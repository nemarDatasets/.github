#!/usr/bin/env bash
# Selection logic for the "Clean up older archive versions (#1518)" step in
# run-generate-archive.yml. Extracted so it can be exercised by
# scripts/test_archive_cleanup_plan.py without touching S3 -- the workflow
# step still makes every AWS call itself (list-object-versions,
# delete-objects); this script only decides, from an already-fetched
# listing, what to delete, and separately classifies whether an AWS error
# means "IAM permission missing" (warn and move on) or a real failure
# (fail the job).
#
# Subcommands:
#
#   plan <listing.json>
#     DATASET_ID, VERSION (env, required). <listing.json> is a real
#     `aws s3api list-object-versions --output json` document (or an
#     equivalent fixture): its `Versions`/`DeleteMarkers` arrays, each
#     entry `{Key, VersionId, IsLatest}`.
#
#     Parses the semantic version out of each key's file name -- the new
#     shape `<id>_v<ver>.zip` or the pre-#1491 legacy shape `v<ver>.zip` --
#     and classifies every object version against $VERSION with a NUMERIC
#     (not lexical) comparison:
#       - strictly lower than $VERSION: always deleted, whatever its name
#         or current/noncurrent state (a superseded build).
#       - exactly $VERSION: every entry is deleted EXCEPT the one current,
#         new-name Version entry (kind=new, IsLatest=true, not a delete
#         marker) -- the single object this run is allowed to keep. Covers
#         noncurrent versions and delete markers of both names for this
#         exact version, and the legacy-name object for this exact version.
#       - strictly higher than $VERSION: NEVER touched -- protects a
#         genuinely newer archive (new- or legacy-named, current or
#         noncurrent, or even a delete marker that is the current state of
#         a higher version) from an out-of-order or retried older build.
#       - a key whose file name does not parse as either shape (including
#         a `-rc.1`/`+build5` suffix, which is valid semver but not the
#         bare X.Y.Z this bucket's archives use, or a nested path, or a
#         non-.zip file): reported (one ::warning:: line to stderr per
#         key), never deleted.
#
#     Prints, to STDOUT, a JSON array of `{Key, VersionId}` to delete (may
#     be empty). Exit codes:
#       0  a plan was computed (the array may be empty).
#       1  refused: a key outside `<id>/archives/` was found in the
#          listing (scope guard), or the listing did not contain exactly
#          one current object named `<id>_v<version>.zip` (abort guard --
#          this also catches the case where the "current" state of that
#          key is actually a delete marker, not a real object). Either way
#          nothing should be deleted; an ::error:: line explains why.
#
#   is-access-denied
#     Reads error text on stdin (e.g. the captured stderr/stdout of a
#     failed `aws` invocation). Exit 0 if it contains "AccessDenied"
#     (case-insensitive), 1 otherwise. For a whole-call AWS CLI failure
#     (e.g. list-object-versions denied outright).
#
#   delete-errors-are-access-denied <delete-result.json>
#     Reads an `aws s3api delete-objects --output json` response. Exit 0
#     if it reports at least one error AND every error's Code is
#     AccessDenied -- S3's actual shape for a missing s3:DeleteObjectVersion
#     permission: the call itself succeeds (HTTP 200), and each object's
#     deletion is individually denied in the Errors[] array, never a
#     whole-call failure. Exit 1 if there are no errors, or any error is
#     NOT AccessDenied (a real failure that must fail the job).
set -euo pipefail

PLAN_JQ_PROGRAM='
def isSemver:
  test("^[0-9]+\\.[0-9]+\\.[0-9]+$");
def toParts:
  split(".") | map(tonumber);
def semverCmp($a; $b):
  ($a | toParts) as $pa | ($b | toParts) as $pb |
  if $pa[0] != $pb[0] then ($pa[0] - $pb[0])
  elif $pa[1] != $pb[1] then ($pa[1] - $pb[1])
  else ($pa[2] - $pb[2])
  end;
def parseArchiveKey:
  (.Key[($prefix | length):]) as $fn |
  ($id + "_v") as $newPrefix |
  if ($fn | startswith($newPrefix)) and ($fn | endswith(".zip")) then
    ($fn[($newPrefix | length):-4]) as $v |
    if ($v | isSemver) then {kind:"new", version:$v} else {kind:"unparsed", version:null} end
  elif ($fn | startswith("v")) and ($fn | endswith(".zip")) then
    ($fn[1:-4]) as $v |
    if ($v | isSemver) then {kind:"legacy", version:$v} else {kind:"unparsed", version:null} end
  else
    {kind:"unparsed", version:null}
  end;
( [(.Versions // [])[] | . + {isMarker:false}]
  + [(.DeleteMarkers // [])[] | . + {isMarker:true}]
) | map(. + parseArchiveKey) | map(
  if .kind == "unparsed" then . + {action:"report"}
  elif (semverCmp(.version; $dispatched) < 0) then . + {action:"delete"}
  elif (semverCmp(.version; $dispatched) > 0) then . + {action:"keep-newer"}
  elif (.kind == "new") and (.IsLatest == true) and (.isMarker == false) then
    . + {action:"keep-current"}
  else . + {action:"delete"}
  end
)
'

cmd_plan() {
  local listing_file="$1"
  : "${DATASET_ID:?DATASET_ID is required}"
  : "${VERSION:?VERSION is required}"
  local prefix="${DATASET_ID}/archives/"
  local plan_file="${TMPDIR:-/tmp}/archive-cleanup-plan.$$.json"

  local out_of_scope
  out_of_scope=$(jq --arg p "$prefix" \
    '[(.Versions // []), (.DeleteMarkers // [])] | flatten | map(select(.Key | startswith($p) | not)) | length' \
    "$listing_file")
  if [ "$out_of_scope" != "0" ]; then
    echo "::error::list-object-versions returned $out_of_scope key(s) outside $prefix; refusing to delete anything" >&2
    return 1
  fi

  jq -c \
    --arg id "$DATASET_ID" \
    --arg prefix "$prefix" \
    --arg dispatched "$VERSION" \
    "$PLAN_JQ_PROGRAM" \
    "$listing_file" > "$plan_file"

  jq -r '.[] | select(.action == "report") | "  " + .Key + " (versionId " + .VersionId + ")"' "$plan_file" \
    | while IFS= read -r line; do echo "::warning::unparseable archive key under ${prefix}, left untouched: ${line}" >&2; done

  local keep_count new_name
  keep_count=$(jq '[.[] | select(.action == "keep-current")] | length' "$plan_file")
  new_name="${DATASET_ID}_v${VERSION}.zip"
  if [ "$keep_count" -ne 1 ]; then
    echo "::error::expected exactly 1 current object named ${new_name} under ${prefix}, found ${keep_count}; refusing to delete anything until this is understood (a fresh upload should always leave exactly one)." >&2
    rm -f "$plan_file"
    return 1
  fi

  jq -c '[.[] | select(.action == "delete") | {Key, VersionId}]' "$plan_file"
  rm -f "$plan_file"
  return 0
}

cmd_is_access_denied() {
  local text
  text=$(cat)
  echo "$text" | grep -qi "AccessDenied"
}

cmd_delete_errors_are_access_denied() {
  local result_file="$1"
  local err_count access_denied_count
  err_count=$(jq '.Errors // [] | length' "$result_file")
  if [ "$err_count" = "0" ]; then
    return 1
  fi
  access_denied_count=$(jq '[.Errors[] | select(.Code == "AccessDenied")] | length' "$result_file")
  [ "$access_denied_count" = "$err_count" ]
}

main() {
  local sub="${1:-}"
  shift || true
  case "$sub" in
    plan) cmd_plan "$@" ;;
    is-access-denied) cmd_is_access_denied "$@" ;;
    delete-errors-are-access-denied) cmd_delete_errors_are_access_denied "$@" ;;
    *)
      echo "Usage: $0 {plan <listing.json> | is-access-denied | delete-errors-are-access-denied <result.json>}" >&2
      exit 2
      ;;
  esac
}

main "$@"
