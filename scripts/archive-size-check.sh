#!/usr/bin/env bash
# Archive size preflight decision for run-generate-archive.yml (#1514).
#
# Picks the total bytes/files for DATASET_ID@VERSION from, in priority
# order, three PRE-RESOLVED sources -- each one is computed by its own
# workflow step, so this script owns only the merge-and-decide logic, not
# any of the three fetches:
#
#   1. MANIFEST_BYTES/MANIFEST_FILES -- the version manifest (data.nemar.org),
#      the only source before #1514. The data plane serves only PUBLISHED,
#      PUBLIC versions (loadPublishedDataset), so this is empty for a private
#      dataset, an anonymous deposit before release, or a version whose
#      dataset_versions row hasn't landed yet. That gap is the nm000284
#      incident: a build was dispatched in exactly that window and the size
#      guard fell through with nothing to fall back on.
#   2. PAYLOAD_TOTAL_BYTES/PAYLOAD_TOTAL_FILES -- the repository_dispatch
#      client_payload's total_bytes/total_files, sent by the Worker's
#      dispatcher when it already has the row's declared size. Empty when
#      the dispatch came from an older backend, or from run-version-doi.yml's
#      own dispatch (which has no such numbers to send).
#   3. DERIVED_BYTES/DERIVED_FILES -- computed by a separate, best-effort
#      workflow step that clones the dataset repo (a plain `git clone` never
#      fetches git-annex content -- it lives out-of-band, referenced by
#      symlinks/pointer files -- so this is cheap even for a 500 GB dataset)
#      and runs the SAME manifest emitter the publish pipeline uses
#      (scripts/emit_manifest.py), reusing tested code rather than a second
#      hand-rolled annex-key parser.
#
# Falling through all three is ADR 0012's fail-open: unknown means build (the
# wall-clock cap on the archive job remains the backstop).
#
# Then applies ARCHIVE_MAX_BYTES / ARCHIVE_MAX_FILES -- kept in lockstep with
# backend/src/services/archive-policy.ts -- and, if over policy, POSTs the
# skip callback exactly as before #1514.
#
# Inputs (env): DATASET_ID, VERSION (required); MANIFEST_BYTES,
# MANIFEST_FILES; PAYLOAD_TOTAL_BYTES, PAYLOAD_TOTAL_FILES; DERIVED_BYTES,
# DERIVED_FILES; CALLBACK_TOKEN, CALLBACK_URL. Every *_BYTES/*_FILES input is
# optional and, when present, must be a plain non-negative integer string --
# anything else is treated the same as absent (with a warning).
#
# Output: writes `skip=true|false` to $GITHUB_OUTPUT when set, and echoes it
# too so a local/test invocation (no GITHUB_OUTPUT) still shows the answer.
set -euo pipefail

MAX_BYTES=$((100 * 1024 * 1024 * 1024))
MAX_FILES=200000

set_output() {
  echo "[output] $1=$2"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1=$2" >> "$GITHUB_OUTPUT"
  fi
}

# A plain non-negative integer string (no leading/trailing junk, no sign),
# bounded at 18 digits (safely under bash's 64-bit signed integer range).
# The bound matters, not just the digit-only check: bash's `-gt` on an
# operand it can't represent as an integer prints "integer expected" to
# stderr and the COMPARISON EVALUATES FALSE rather than aborting the script
# (verified: `[ 99999999999999999999999999 -gt 100 ]` exits 1 with that
# warning, not a hard error) -- so an unbounded digit string here would make
# an absurdly large value silently compare as "under the limit" a few lines
# down, the opposite of what a size GATE must do with a value it cannot
# trust. Treated the same as any other malformed input: falls through to the
# next tier instead of being trusted.
is_uint() {
  case "$1" in
    '' | *[!0-9]*) return 1 ;;
  esac
  # A digit-only length check, not a glob-count of "?" placeholders: the
  # latter is exactly the kind of off-by-one this bound exists to avoid
  # getting wrong in the first place.
  [ "${#1}" -le 18 ]
}

# Thousands-separated form of a plain non-negative integer string, matching
# JS's `Number.prototype.toLocaleString()` (used for the equivalent reason in
# backend/src/services/archive-policy.ts): "214000" -> "214,000". Built with
# plain awk string slicing rather than `printf "%'d"`, which depends on the
# runner's locale (grouping character, or no grouping at all in "C"/"POSIX").
thousands() {
  awk -v n="$1" 'BEGIN {
    len = length(n);
    rem = len % 3;
    if (rem == 0) rem = 3;
    out = substr(n, 1, rem);
    for (i = rem + 1; i <= len; i += 3) {
      out = out "," substr(n, i, 3);
    }
    print out;
  }'
}

if [ -z "${DATASET_ID:-}" ] || [ -z "${VERSION:-}" ]; then
  echo "::error::Missing dataset_id/version"
  exit 1
fi

BYTES=""
FILES=""
SOURCE=""

# --- Tier 1: the version manifest (already resolved by the caller) ---------
if is_uint "${MANIFEST_BYTES:-}"; then
  BYTES="$MANIFEST_BYTES"
  if is_uint "${MANIFEST_FILES:-}"; then
    FILES="$MANIFEST_FILES"
  fi
  SOURCE="manifest"
fi

# --- Tier 2: the dispatch payload -------------------------------------------
if [ -z "$BYTES" ]; then
  if is_uint "${PAYLOAD_TOTAL_BYTES:-}"; then
    BYTES="$PAYLOAD_TOTAL_BYTES"
    if is_uint "${PAYLOAD_TOTAL_FILES:-}"; then
      FILES="$PAYLOAD_TOTAL_FILES"
    fi
    SOURCE="dispatch payload"
  elif [ -n "${PAYLOAD_TOTAL_BYTES:-}" ]; then
    echo "::warning::client_payload.total_bytes is not a plain integer (${PAYLOAD_TOTAL_BYTES}); ignoring"
  fi
fi

# --- Tier 3: annex-key derivation (already resolved by the caller) ---------
if [ -z "$BYTES" ] && is_uint "${DERIVED_BYTES:-}"; then
  BYTES="$DERIVED_BYTES"
  if is_uint "${DERIVED_FILES:-}"; then
    FILES="$DERIVED_FILES"
  fi
  SOURCE="annex-key derivation"
fi

# --- Tier 4: fail open -------------------------------------------------------
if [ -z "$BYTES" ]; then
  echo "::error::no size source available (manifest, dispatch payload, and annex-key derivation all failed or were unavailable); proceeding to build (size guard skipped)"
  set_output skip false
  exit 0
fi

echo "total (source: ${SOURCE}): ${BYTES} bytes, ${FILES:-unknown} files (limits ${MAX_BYTES} bytes / ${MAX_FILES} files)"

REASON=""
if [ "$BYTES" -gt "$MAX_BYTES" ]; then
  GB=$(awk "BEGIN{printf \"%.1f\", ${BYTES}/1073741824}")
  REASON="dataset ${GB} GB exceeds 100.0 GB archive limit; use direct download"
elif is_uint "${FILES:-}" && [ "$FILES" -gt "$MAX_FILES" ]; then
  REASON="dataset $(thousands "$FILES") files exceeds $(thousands "$MAX_FILES") archive limit; use direct download"
fi

if [ -z "$REASON" ]; then
  echo "Under archive limits (source: ${SOURCE}); building zip."
  set_output skip false
  exit 0
fi

echo "::notice::Skipping archive build: ${REASON}"
set_output skip true

if [ -z "${CALLBACK_TOKEN:-}" ]; then
  echo "::warning::NEMAR_WEBHOOK_TOKEN unset; cannot record skipped state"
  exit 0
fi

jq -nc --arg id "$DATASET_ID" --arg v "$VERSION" --arg r "$REASON" \
  '{dataset_id:$id,status:"skipped",version:$v,reason:$r}' > /tmp/archive-preflight-skip.json
rm -f /tmp/archive-preflight-skip-resp.json
HTTP=$(curl -sS -o /tmp/archive-preflight-skip-resp.json -w "%{http_code}" --connect-timeout 10 --max-time 30 \
  -X POST "$CALLBACK_URL" -H "Content-Type: application/json" \
  -H "X-Webhook-Token: $CALLBACK_TOKEN" -d @/tmp/archive-preflight-skip.json) || HTTP=0
echo "skip callback HTTP ${HTTP}: $(cat /tmp/archive-preflight-skip-resp.json 2>/dev/null || true)"
if [ "$HTTP" -lt 200 ] || [ "$HTTP" -ge 300 ]; then
  # FAIL the job (don't exit 0): if we skip the build but never record
  # archive_skip_reason, the dataset is a silent dead-end (NULL status +
  # NULL reason = "missing archive" forever). A red preflight is visible
  # and re-runnable; the gated archive job is skipped on a failed dep, so
  # there's no doomed build. Re-run is idempotent (re-skips, re-POSTs).
  echo "::error::skip callback failed (HTTP ${HTTP}); failing so archive_skip_reason is not silently dropped"
  exit 1
fi
