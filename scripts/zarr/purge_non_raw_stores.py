#!/usr/bin/env python3
"""Purge already-published non-raw Zarr stores (nemarOrg/nemar-cli#1095,
tracked by nemarOrg/nemar-cli#1097).

PR #98 made `generate_zarr.py` raw-only: `derivatives/`, `sourcedata/`, and
`code/` are no longer walked for NEW recordings. It deliberately left the
stores that earlier, tree-walking runs had already published under those
trees alone -- `compute_clean_orphans` in that file explicitly protects an
already-published store under an excluded tree from `--clean`'s own
orphan-removal, precisely so that raw-only scope change alone could never
mass-delete the ~4,721 stores (12% of all live stores at last count) that
predate it. Deleting them is separate, explicitly-authorized follow-up work,
and this script is that follow-up.

This is a standalone script, not a `generate_zarr.py` entry point (a
concurrent edit to that file cannot conflict with it), and it imports rather
than re-derives the raw/non-raw distinction: `is_excluded_from_discovery`,
`safe_store_prefix`, `_rm_recursive`, `s3_read_json`, `aws_cp`, and the S3
listing helpers all come from `generate_zarr.py` so the two files cannot drift
apart on what counts as "excluded" or how a store's S3 prefix is built.

Safety design
-------------
* Dry-run by default. Nothing is deleted or rewritten without `--execute`.
* Targets are DERIVED from the published `index.json`, never from a path
  pattern alone (`select_purge_candidates`), then CONFIRMED against S3 --
  object count and total bytes -- before anything is deleted (`stat_prefix`).
  An index entry with zero matching S3 objects is reported as
  `already_absent`, not silently skipped or silently deleted twice: it
  usually just means an earlier, partial `--execute` run already removed it
  (this tool is idempotent and safe to re-run), but it could also mean the
  index is stale, so it is always visible in the report either way.
  This required a fix to hold: `aws s3 ls` exits 1 for a prefix matching zero
  keys, so every already-absent store used to raise and be filed as an error
  instead, which also meant its index entry could never be dropped. See
  `interpret_s3_ls_result`.
* Only ever `<dataset_id>/zarr/...`. Every computed delete target is
  re-validated, a second and independent time, immediately before the delete
  call itself (`assert_within_zarr_prefix`) -- `safe_store_prefix` already
  rejects an unsafe `zarr` rel-path (path traversal, an absolute path, an
  empty segment) when the prefix is first built in `prepare_targets`; the
  second check exists so a future refactor that calls `_rm_recursive` on a
  prefix built a different way still trips a guard before it can escape this
  dataset's own zarr tree.
* A store is only ~/`sub-*/` (raw) or excluded (derivatives/sourcedata/code,
  or a reserved BIDS calibration filename) by construction of
  `is_excluded_from_discovery` -- imported unmodified from `generate_zarr.py`
  -- so a raw store's `zarr` rel-path can never satisfy `select_purge_
  candidates`. A hand-edited or corrupted index entry whose `path` and `zarr`
  disagree on that question is routed to `anomalies` and never purged.
* `index.json` is rewritten (`rewrite_index`) to drop purged store entries and
  any `failures` entries for the same paths, preserving every other top-level
  field and every remaining entry's content and relative order untouched.
* `--from-index-snapshot DIR` reads the store list from a SAVED `index.json`
  rather than the live one, for stores stranded in S3 after a converter rebuild
  stopped listing them (the raw-only change in #98 makes a rebuilt index omit
  them, which puts them beyond the reach of every index-derived target). It
  widens where the store list is read from, NOT what may be deleted: selection
  still runs `is_excluded_from_discovery`, so a raw path is unselectable from
  any document; `safe_store_prefix` and `assert_within_zarr_prefix` still pin
  each delete inside the dataset's own `zarr/` tree; and every target is still
  confirmed against S3 before deletion. The snapshot is validated as a
  `nemar-zarr-index` for that exact `dataset_id` (`load_snapshot_index`), and
  the index rewrite is computed from the LIVE document and skipped when it does
  not list the purged stores (`plan_snapshot_index_rewrite`) -- publishing the
  snapshot itself would restore the very entries being cleaned up.
* The ordering (delete before index rewrite, always exactly once) and
  error-isolation decisions (an errored target's index entry always
  survives; an already-absent one is folded in without a second delete
  attempt) are pure, data-in/data-out functions -- `plan_dataset_operations`,
  `decide_target_action`, `summarize_target_outcomes` -- not implicit facts
  about `purge_dataset`'s control flow. `purge_dataset` itself is a thin
  walk over the plan those return.
* Every pure function above -- selection, the escape guard, index rewriting,
  S3-listing-output parsing, and the plan/decision/summary trio -- is unit
  tested directly, with no mocking of business logic, in
  `test_purge_non_raw_stores.py`. The actual S3 list/delete/read/write calls
  themselves (`stat_prefix`'s and `_execute_target_step`'s subprocess/S3
  calls, `discover_excluded_stores`, `write_index`, `list_dataset_ids`) are
  thin I/O wrappers around that tested logic and are NOT exercised by the
  automated test suite here -- see the PR description for exactly what that
  leaves unverified.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_zarr import (  # type: ignore[import-not-found]  (sibling module via sys.path)
    _AWS_OP_TIMEOUT,
    _AWS_TIMEOUTS,
    EXCLUDED_TREES,
    _aws_env,
    _rm_recursive,
    _s3_child_prefixes,
    aws_cp,
    is_excluded_from_discovery,
    s3_read_json,
    safe_store_prefix,
)

DEFAULT_BUCKET = "nemar"
AUDIT_FORMAT = "nemar-zarr-purge-audit"
AUDIT_FORMAT_VERSION = 1

# The `format` marker every `index.json` this tool understands carries. Checked
# when loading a snapshot so `--from-index-snapshot` cannot be pointed at some
# other JSON file that happens to have a `stores` list.
INDEX_FORMAT = "nemar-zarr-index"


# --- Pure: candidate selection -------------------------------------------


def select_purge_candidates(index: dict) -> tuple[list[dict], list[dict]]:
    """Split `index["stores"]` into `(candidates, anomalies)`.

    A candidate is a store entry whose `zarr` rel-path the imported
    `is_excluded_from_discovery` predicate says is excluded (under
    derivatives/sourcedata/code, or a reserved BIDS calibration filename --
    neither can be a raw recording), AND whose `path` field (the original
    BIDS-relative source path) AGREES with `zarr` on that question. Both a
    disagreement (a `path` that looks like an ordinary `sub-*/` recording
    paired with a `zarr` that looks excluded) and a MISSING or non-string
    `path` are routed to `anomalies` instead of ever being purged -- a
    stripped `path` is itself exactly the shape a corrupted or hand-edited
    entry would have, so its absence must count as a disagreement, not as
    consent.

    An entry missing a well-formed string `zarr` is also an anomaly (a
    well-formed index always carries one -- it is the dict key the entry is
    stored under in `generate_zarr.merge_index`). Nothing in `anomalies` is
    ever selected for deletion by anything downstream.
    """
    stores = index.get("stores", [])
    candidates: list[dict] = []
    anomalies: list[dict] = []
    if not isinstance(stores, list):
        return candidates, anomalies
    for entry in stores:
        if not isinstance(entry, dict):
            anomalies.append({"entry": entry, "reason": "store entry is not an object"})
            continue
        zarr = entry.get("zarr")
        if not isinstance(zarr, str) or not zarr:
            anomalies.append({"entry": entry, "reason": "missing or empty 'zarr'"})
            continue
        if not is_excluded_from_discovery(zarr):
            continue  # ordinary raw store -- never a candidate, never reported
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            anomalies.append({"entry": entry, "reason": "missing or non-string 'path'"})
            continue
        if not is_excluded_from_discovery(path):
            anomalies.append(
                {"entry": entry, "reason": "'zarr' looks excluded but 'path' does not agree"}
            )
            continue
        candidates.append(entry)
    return candidates, anomalies


def assert_within_zarr_prefix(prefix: str, *, bucket: str, dataset_id: str) -> None:
    """Refuse, loudly, a delete target that is not strictly inside this
    dataset's own `<id>/zarr/` tree.

    Deliberately redundant with `safe_store_prefix`'s own validation: this
    runs again, on the literal string about to be handed to `_rm_recursive`,
    immediately before every delete call. Raises `AssertionError` rather than
    returning a bool so a bug here aborts loudly instead of being mistakenly
    treated as "not a candidate" and silently skipped.
    """
    required = f"s3://{bucket}/{dataset_id}/zarr/"
    if prefix == required or not prefix.startswith(required):
        raise AssertionError(
            f"refusing to delete {prefix!r}: does not resolve to a store strictly "
            f"inside {required!r}"
        )


def prepare_targets(
    bucket: str, dataset_id: str, candidates: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Compute and guard the S3 delete-prefix for each candidate.

    Returns `(targets, rejected)`. A target dict carries the original `entry`
    plus `rel_store` and `key_prefix` (the full `s3://...` URL). `rejected`
    holds any candidate whose `zarr` value `safe_store_prefix` or
    `assert_within_zarr_prefix` refuses -- a path-traversal value, an
    absolute path, an empty segment, or (defensively) anything else that
    resolves outside the dataset's own zarr tree. Nothing in `rejected` is
    ever handed to a delete call; this is the guard's actual unit-testable
    surface for a malicious or malformed index value.

    Also de-duplicates by `rel_store`: a well-formed index can never have two
    `stores` entries sharing one `zarr` value (`generate_zarr.merge_index`
    uses it as the dict key), so two candidates naming the same `rel_store`
    only happens for a hand-edited/corrupted index, and both name the
    identical S3 location regardless. The first occurrence becomes the (one)
    target; every later one is routed to `rejected` rather than a second
    target, so this dataset's stat/delete work -- and its audit trail -- is
    never duplicated for what is physically one store. `rewrite_index` still
    drops every `stores` entry sharing that `zarr` value once it is purged,
    independent of how many targets were built for it.
    """
    targets: list[dict] = []
    rejected: list[dict] = []
    seen_rel_stores: set[str] = set()
    for entry in candidates:
        rel_store = entry["zarr"]
        if rel_store in seen_rel_stores:
            rejected.append(
                {
                    "entry": entry,
                    "reason": f"duplicate 'zarr' value {rel_store!r} across multiple store entries",
                }
            )
            continue
        try:
            prefix = safe_store_prefix(bucket, dataset_id, rel_store)
            assert_within_zarr_prefix(prefix, bucket=bucket, dataset_id=dataset_id)
        except (ValueError, AssertionError) as exc:
            rejected.append({"entry": entry, "reason": str(exc)})
            continue
        seen_rel_stores.add(rel_store)
        targets.append({"entry": entry, "rel_store": rel_store, "key_prefix": prefix})
    return targets, rejected


def rewrite_index(index: dict, purged_rels: set[str]) -> dict:
    """Drop purged store/failure entries from `index`. Pure; returns a new dict.

    Preserves every top-level field verbatim except `stores`/`store_count`
    and `failures`/`failure_count`, which are recomputed from the filtered
    lists -- any field this function does not know about (a future schema
    addition) survives untouched because it starts from a shallow copy of
    `index` rather than rebuilding the document field by field. Remaining
    `stores`/`failures` entries keep their original content and relative
    order (filtered, never re-sorted or otherwise modified).

    Idempotent: calling this twice with the same `purged_rels` on its own
    output is a no-op the second time, since the matching entries are already
    gone.
    """
    out = dict(index)

    # `"key" in index` (not `.get(..., [])`) so a document that never had a
    # `stores`/`failures` key does not gain an empty one -- "preserve every
    # other field" must not itself add a field that was not there.
    if "stores" in index and isinstance(index["stores"], list):
        kept_stores = [
            e for e in index["stores"] if not (isinstance(e, dict) and e.get("zarr") in purged_rels)
        ]
        out["stores"] = kept_stores
        out["store_count"] = len(kept_stores)

    if "failures" in index and isinstance(index["failures"], list):
        kept_failures = [
            f
            for f in index["failures"]
            if not (isinstance(f, dict) and f.get("zarr") in purged_rels)
        ]
        out["failures"] = kept_failures
        out["failure_count"] = len(kept_failures)

    return out


def plan_snapshot_index_rewrite(live_index: dict | None, purged_rels: set[str]) -> dict | None:
    """Decide whether a snapshot-mode purge should rewrite the LIVE index.

    Returns the rewritten live document, or `None` when no write is warranted.

    This exists because snapshot mode reads its candidates from a SAVED index
    while the authoritative document on S3 has usually moved on. Feeding the
    snapshot itself to `write_index` would publish a stale document over a
    fresher one -- for the stranded stores this mode was built for, that means
    resurrecting hundreds of non-raw entries a converter rebuild had already
    dropped, i.e. undoing the exact cleanup being performed. So the rewrite is
    always computed from the live document, never from the snapshot, and is
    skipped entirely when the live document does not mention any purged store
    (the normal stranded case: the bytes were orphaned in S3 precisely because
    the index stopped listing them).
    """
    if not live_index or not purged_rels:
        return None
    listed: set = set()
    for key in ("stores", "failures"):
        entries = live_index.get(key)
        if isinstance(entries, list):
            listed |= {e.get("zarr") for e in entries if isinstance(e, dict)}
    if not (listed & purged_rels):
        return None
    return rewrite_index(live_index, purged_rels)


def load_snapshot_index(snapshot_dir: str, dataset_id: str) -> dict:
    """Load `<snapshot_dir>/<dataset_id>.json` as this dataset's index.

    Raises `FileNotFoundError` when there is no snapshot for the dataset,
    `TypeError` when the file is not a JSON object, and `ValueError` when it is
    not a `nemar-zarr-index` document for THIS dataset. The caller treats all
    three the same (a per-dataset `error` row, batch continues); they are
    distinct so a direct caller can tell "wrong shape" from "wrong dataset".
    These guards matter more here than for an S3 read: an S3 index key
    is derived from the dataset id and cannot belong to another dataset, while
    a local path is whatever the operator typed. Checking `format` and
    `dataset_id` means a mistyped directory, a half-written file, or a snapshot
    of the wrong dataset fails loudly instead of authorizing deletes computed
    from someone else's store list.

    Note what this does NOT need to guard: a snapshot cannot authorize deleting
    a raw store. Candidate selection runs `is_excluded_from_discovery` over
    whatever index it is given, so a raw `sub-*/` path is never selected no
    matter which document it came from, and `safe_store_prefix` plus
    `assert_within_zarr_prefix` still pin every delete inside this dataset's own
    `zarr/` tree. Snapshot mode changes where the store list is read from, not
    what is permitted to be deleted.
    """
    path = Path(snapshot_dir) / f"{dataset_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no index snapshot at {path}")
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise TypeError(f"{path}: not a JSON object")
    fmt = doc.get("format")
    if fmt != INDEX_FORMAT:
        raise ValueError(f"{path}: format is {fmt!r}, expected {INDEX_FORMAT!r}")
    got = doc.get("dataset_id")
    if got != dataset_id:
        raise ValueError(f"{path}: dataset_id is {got!r}, expected {dataset_id!r}")
    return doc


def snapshot_dataset_ids(snapshot_dir: str) -> list[str]:
    """Dataset ids that have a snapshot in `snapshot_dir`, sorted.

    Used so `--all` in snapshot mode is bounded by the snapshot directory
    rather than by a bucket listing: the set of datasets with a saved index is
    exactly the set this mode can act on, and enumerating the bucket instead
    would report every other dataset as a missing snapshot.
    """
    d = Path(snapshot_dir)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if p.is_file())


# --- Pure: per-dataset orchestration decisions ---------------------------
#
# These three functions own every decision `purge_dataset` makes about
# ordering, error isolation, and what is safe to fold into an index rewrite.
# They take and return plain data (a list of targets, a list of already-
# finished per-target outcomes), so a test can pin the ordering and
# error-isolation properties directly, without any S3 access, and a future
# refactor that inverted the delete/rewrite order, dropped the `execute`
# gate, or let an errored target's entry get dropped from the index would
# fail a test here rather than only being caught by a careful reader.


def plan_dataset_operations(targets: list[dict]) -> list[dict]:
    """The ordered sequence of steps `purge_dataset` runs for one dataset.

    One `{"op": "stat_then_maybe_delete", ...}` step per target, in exactly
    the order `targets` were given (this tool never reorders or parallelizes
    across stores), followed by exactly one trailing
    `{"op": "rewrite_index"}` step. Returning the plan as data -- rather than
    letting "stat/delete every target, then rewrite the index" be an
    unstated fact about where a function call sits in `purge_dataset`'s
    body -- is what makes the delete-before-index-rewrite ordering something
    a test can assert on directly.
    """
    steps = [
        {
            "op": "stat_then_maybe_delete",
            "rel_store": t["rel_store"],
            "key_prefix": t["key_prefix"],
            "entry": t["entry"],
        }
        for t in targets
    ]
    steps.append({"op": "rewrite_index"})
    return steps


def decide_target_action(object_count: int, *, execute: bool) -> str:
    """The one decision of whether a target already confirmed to hold
    `object_count` objects on S3 is ever handed to a delete call.

    Returns `"already_absent"` (0 objects -- never delete, regardless of
    `execute`), `"would_purge"` (objects present, dry run), or `"delete"`
    (objects present, `--execute`). `_execute_target_step` is the only
    caller, and only calls `_rm_recursive` when this returns `"delete"`.
    """
    if object_count == 0:
        return "already_absent"
    return "delete" if execute else "would_purge"


def summarize_target_outcomes(outcomes: list[dict]) -> dict:
    """Pure aggregation over a dataset's finished per-target outcomes.

    Each outcome is a dict with at least `rel_store` and `state`, `state`
    one of `"purged"`, `"already_absent"`, `"would_purge"`, `"stat_error"`,
    or `"delete_error"`.

    This is the one place that decides which `zarr` rel-paths are safe to
    fold into an `index.json` rewrite: `purged_rels` is exactly the
    `"purged"` and `"already_absent"` states -- NEVER `"stat_error"` or
    `"delete_error"`. That is the per-target error-isolation property: one
    store's failed stat or failed delete can never cause the index to claim
    a store that might still physically exist on S3 is gone; its entry
    always survives to be retried on the next run. Every returned list
    preserves `outcomes`' input order (this tool never reorders stores).
    """
    purged_rels = {o["rel_store"] for o in outcomes if o["state"] in ("purged", "already_absent")}
    purged = [o for o in outcomes if o["state"] == "purged"]
    return {
        "purged_rels": purged_rels,
        "purged": purged,
        "already_absent": [o for o in outcomes if o["state"] == "already_absent"],
        "would_purge": [o for o in outcomes if o["state"] == "would_purge"],
        "delete_errors": [o for o in outcomes if o["state"] in ("stat_error", "delete_error")],
        "bytes_freed": sum(o.get("bytes", 0) for o in purged),
        "objects_freed": sum(o.get("object_count", 0) for o in purged),
    }


# --- Pure: S3 listing-output parsing -------------------------------------

_TOTAL_OBJECTS_RE = re.compile(r"^Total Objects:\s*(\d+)\s*$", re.MULTILINE)
_TOTAL_SIZE_RE = re.compile(r"^\s*Total Size:\s*(\d+)\s*$", re.MULTILINE)


def parse_s3_ls_summary(output: str) -> tuple[int, int]:
    """Parse `aws s3 ls --recursive --summarize`'s trailing summary lines into
    `(object_count, total_bytes)`.

    Both default to 0 when the summary lines are absent. Absence is always read
    as "empty," never as "unknown," which is the conservative direction for this
    tool: it only ever makes a store look like it needs no deletion, never the
    reverse.

    Note what `aws s3 ls` actually does for a prefix matching zero keys, since
    an earlier version of this docstring had it wrong: it DOES print a
    well-formed `Total Objects: 0` / `Total Size: 0` summary, and it exits 1.
    Deciding whether a non-zero exit is a real failure or just "no keys" is
    `interpret_s3_ls_result`'s job, not this function's.
    """
    objects_match = _TOTAL_OBJECTS_RE.search(output)
    size_match = _TOTAL_SIZE_RE.search(output)
    count = int(objects_match.group(1)) if objects_match else 0
    total_bytes = int(size_match.group(1)) if size_match else 0
    return count, total_bytes


def interpret_s3_ls_result(returncode: int, stdout: str) -> tuple[int, int] | None:
    """`(object_count, total_bytes)` when `aws s3 ls`'s output can be trusted,
    `None` when the call genuinely failed and the counts are unknown.

    `aws s3 ls --recursive --summarize` **exits 1 for a prefix that matches zero
    keys**, while still printing a well-formed `Total Objects: 0` summary and
    writing nothing to stderr. That is a real answer -- the store is already
    gone -- not a failure.

    Treating every non-zero exit as an error made the `already_absent` outcome
    unreachable in practice, which was not merely a reporting wart. An errored
    target is excluded from `purged_rels` by design (never drop the index entry
    of a store we are unsure about), so a store deleted by a run whose index
    rewrite did not complete would be re-statted on the next run, raise, be
    classed as an error, and keep its index entry -- permanently, across any
    number of re-runs. The published index would go on advertising stores that
    return 404. Observed on `nm000172`: 23 stores deleted, index rewrite lost to
    an expired session, and every subsequent run reported 23 "delete errors"
    while being unable to ever finish the job.

    The discriminator is the summary itself: a credential, permission, or
    network failure produces no `Total Objects` line, so it still returns None
    and still raises upstream.
    """
    if returncode == 0 or _TOTAL_OBJECTS_RE.search(stdout):
        return parse_s3_ls_summary(stdout)
    return None


# --- I/O: S3 + index.json -------------------------------------------------
#
# Thin wrappers around the pure logic above. Not covered by the automated
# test suite (see the PR description); the pure functions they call are.


def stat_prefix(bucket: str, key_prefix: str, *, timeout: int = _AWS_OP_TIMEOUT) -> tuple[int, int]:
    """`(object_count, total_bytes)` actually present on S3 under `key_prefix`
    (a full `s3://bucket/...` URL), read fresh right before any delete
    decision -- this is the "confirm against S3" step; nothing is ever
    deleted on the strength of the index alone.
    """
    res = subprocess.run(
        ["aws", "s3", "ls", key_prefix, "--recursive", "--summarize", *_AWS_TIMEOUTS],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_aws_env(),
        check=False,
    )
    counts = interpret_s3_ls_result(res.returncode, res.stdout)
    if counts is None:
        raise RuntimeError(f"aws s3 ls {key_prefix} exited {res.returncode}: {res.stderr.strip()}")
    return counts


def _execute_target_step(bucket: str, dataset_id: str, step: dict, *, execute: bool) -> dict:
    """Run one `plan_dataset_operations` `"stat_then_maybe_delete"` step.

    Thin I/O wrapper: `stat_prefix` and `_rm_recursive` are real S3 calls, so
    this function itself is not covered by the automated test suite. The
    decisions it defers to -- whether an object count means "already gone"
    versus "delete this" (`decide_target_action`) and what a finished batch
    of these outcomes means for the index (`summarize_target_outcomes`) --
    are pure and are.

    Returns one outcome dict for `summarize_target_outcomes`, carrying
    `rel_store`, `path`, `key_prefix`, `state`, `object_count`, `bytes`, and
    (on a `stat_error`/`delete_error` state) `error`.
    """
    rel_store = step["rel_store"]
    entry = step["entry"]
    key_prefix = step["key_prefix"]
    base = {"rel_store": rel_store, "path": entry.get("path"), "key_prefix": key_prefix}

    try:
        count, total_bytes = stat_prefix(bucket, key_prefix)
    except Exception as exc:  # noqa: BLE001 - reported as this target's outcome
        return {**base, "state": "stat_error", "object_count": 0, "bytes": 0, "error": str(exc)}

    action = decide_target_action(count, execute=execute)
    if action in ("already_absent", "would_purge"):
        return {**base, "state": action, "object_count": count, "bytes": total_bytes}

    # action == "delete"
    try:
        # Redundant, deliberate re-check immediately before the delete call
        # itself (requirement: a guard right before each delete).
        assert_within_zarr_prefix(key_prefix, bucket=bucket, dataset_id=dataset_id)
        _rm_recursive(key_prefix)
    except Exception as exc:  # noqa: BLE001 - reported as this target's outcome
        return {**base, "state": "delete_error", "object_count": count, "bytes": total_bytes, "error": str(exc)}
    return {**base, "state": "purged", "object_count": count, "bytes": total_bytes}


def discover_excluded_stores(bucket: str, dataset_id: str) -> set[str]:
    """Every `.zarr` store directory that actually exists on S3 under an
    excluded tree for this dataset, regardless of what `index.json` says.

    Bounded to `derivatives/`, `sourcedata/`, `code/` under `<id>/zarr/` --
    never the dataset's raw `sub-*/` tree, which can be far larger. Used only
    to report index/S3 drift (the "vice versa" side of requirement 2 --
    something purge-eligible that exists on S3 but the index never listed);
    never itself a source of delete targets. Descends via delimited listing
    (one LIST per directory level) rather than a flat recursive listing, so
    it costs one call per directory rather than one per object.
    """
    found: set[str] = set()
    zarr_root = f"s3://{bucket}/{dataset_id}/zarr/"
    for tree in EXCLUDED_TREES:
        stack = [f"{zarr_root}{tree}/"]
        while stack:
            url = stack.pop()
            for child in _s3_child_prefixes(url):
                if child.endswith(".zarr/"):
                    found.add(child[len(zarr_root) : -1])
                else:
                    stack.append(child)
    return found


def write_index(bucket: str, dataset_id: str, index: dict) -> None:
    """Write the rewritten `index.json` back to S3.

    `index.json`'s destination is a single S3 object, so a single `aws s3 cp`
    PUT is already atomic there: a reader sees the previous full body or the
    new full body, never a partial one. The "write to a sibling temp path,
    then swap" half of the atomic-write pattern `generate_zarr.py`'s
    `fix_source_file_attr` uses for a local file applies here to the LOCAL
    staging file this function builds before that PUT -- the full document is
    written out completely before anything is uploaded, so a crash mid-dump
    never touches the object this uploads. Mirrors `generate_zarr.py main()`'s
    own `index.json` write for this exact file.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(index, fh, separators=(",", ":"))
        tmp_path = fh.name
    try:
        aws_cp(
            tmp_path,
            f"s3://{bucket}/{dataset_id}/zarr/index.json",
            extra=["--content-type", "application/json", "--cache-control", "public, max-age=60"],
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def write_audit_log(path: str, report: dict) -> None:
    """Write the audit report to `path` atomically: temp file, then
    `os.replace` -- the direct local-file application of the same pattern
    `write_index`/`fix_source_file_attr` use, so an interrupted run never
    leaves a truncated audit record on disk.

    Uses `tempfile.NamedTemporaryFile` (a unique name), not a deterministic
    `f"{path}.tmp"` sibling: two concurrent runs sharing an explicit
    `--audit-log` path would otherwise collide on the same temp file. The
    temp file is created in `path`'s own directory so `os.replace` is a
    same-filesystem rename (atomic), not a cross-filesystem copy.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    with tempfile.NamedTemporaryFile(
        "w", prefix=".purge-audit-", suffix=".json.tmp", dir=directory,
        delete=False, encoding="utf-8",
    ) as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        tmp_path = fh.name
    os.replace(tmp_path, path)


def list_dataset_ids(bucket: str) -> list[str]:
    """Every top-level `<id>/` prefix in the bucket, excluding `staging/`
    (the PR staging area -- see AGENTS.md's S3 bucket structure -- which is
    never a dataset).
    """
    ids = []
    for child in _s3_child_prefixes(f"s3://{bucket}/"):
        rel = child[len(f"s3://{bucket}/") :].rstrip("/")
        if rel and rel != "staging":
            ids.append(rel)
    return sorted(ids)


# --- Orchestration ---------------------------------------------------------


def purge_dataset(
    bucket: str,
    dataset_id: str,
    *,
    execute: bool,
    check_extra: bool = True,
    snapshot_dir: str | None = None,
) -> dict:
    """Run the full purge pipeline for one dataset.

    Returns a JSON-serializable result dict, which also doubles as this
    dataset's audit-log record. Never raises for an ordinary per-store
    problem (a candidate missing from S3, a rejected candidate, one store's
    stat/delete call failing) -- those are all captured in the result so a
    bulk run keeps going. Only re-raises when `index.json` itself cannot be
    read for a reason other than "does not exist"; the CLI's bulk loop
    catches that per dataset too, so one dataset's failure never aborts the
    whole run (requirement: a per-dataset outcome table, not an abort).

    The actual ordering and error-isolation decisions -- delete-before-
    index-rewrite, never dropping an errored target's index entry, never
    double-counting an already-absent one -- are NOT made here. They live in
    `plan_dataset_operations` and `summarize_target_outcomes`, both pure and
    directly unit tested; this function only walks the plan those return and
    performs the I/O each step calls for.
    """
    result: dict = {
        "dataset_id": dataset_id,
        "bucket": bucket,
        "execute": execute,
        "status": "ok",
        "index_found": False,
        "candidates": 0,
        "anomalies": [],
        "rejected": [],
        "purged": [],
        "already_absent": [],
        "delete_errors": [],
        "extra_on_s3_not_in_index": [],
        "index_rewritten": False,
        "bytes_freed": 0,
        "objects_freed": 0,
        "index_source": "snapshot" if snapshot_dir else "s3",
    }
    index_key = f"{dataset_id}/zarr/index.json"
    if snapshot_dir:
        try:
            index = load_snapshot_index(snapshot_dir, dataset_id)
        except FileNotFoundError:
            result["status"] = "no_snapshot"
            return result
        except Exception as exc:  # noqa: BLE001 - reported, not fatal to the batch
            result["status"] = "error"
            result["error"] = f"failed to load index snapshot: {exc}"
            return result
    else:
        try:
            index = s3_read_json(bucket, index_key)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal to the batch
            result["status"] = "error"
            result["error"] = f"failed to read index.json: {exc}"
            return result
        if index is None:
            result["status"] = "no_index"
            return result
    result["index_found"] = True

    candidates, selection_anomalies = select_purge_candidates(index)
    targets, rejected = prepare_targets(bucket, dataset_id, candidates)
    result["candidates"] = len(candidates)
    result["anomalies"] = selection_anomalies
    result["rejected"] = rejected

    outcomes: list[dict] = []
    summary: dict = summarize_target_outcomes(outcomes)  # baseline if targets is empty
    for step in plan_dataset_operations(targets):
        if step["op"] == "stat_then_maybe_delete":
            outcomes.append(_execute_target_step(bucket, dataset_id, step, execute=execute))
        else:  # "rewrite_index" -- plan_dataset_operations always puts exactly
            # one of these, always last, so `outcomes` is complete here.
            summary = summarize_target_outcomes(outcomes)

    def _record(o: dict) -> dict:
        return {
            "dataset_id": dataset_id,
            "path": o.get("path"),
            "zarr": o["rel_store"],
            "key_prefix": o["key_prefix"],
            "object_count": o.get("object_count", 0),
            "bytes": o.get("bytes", 0),
        }

    result["purged"] = [_record(o) for o in summary["purged"] + summary["would_purge"]]
    result["already_absent"] = [_record(o) for o in summary["already_absent"]]
    result["delete_errors"] = [
        {
            "zarr": o["rel_store"],
            "path": o.get("path"),
            "stage": "stat" if o["state"] == "stat_error" else "delete",
            "error": o.get("error"),
        }
        for o in summary["delete_errors"]
    ]
    result["bytes_freed"] = summary["bytes_freed"]
    result["objects_freed"] = summary["objects_freed"]

    if check_extra:
        try:
            on_s3 = discover_excluded_stores(bucket, dataset_id)
            indexed_rels = {c["zarr"] for c in candidates}
            result["extra_on_s3_not_in_index"] = sorted(on_s3 - indexed_rels)
        except Exception as exc:  # noqa: BLE001 - best-effort reconciliation only
            result["extra_on_s3_check_error"] = str(exc)

    purged_rels = summary["purged_rels"]
    if execute and purged_rels:
        if snapshot_dir:
            # Compute the rewrite from the LIVE document, never the snapshot --
            # see plan_snapshot_index_rewrite for why publishing the snapshot
            # would undo the cleanup it is performing.
            try:
                live_index = s3_read_json(bucket, index_key)
            except Exception as exc:  # noqa: BLE001 - surfaced; data is already deleted
                result["status"] = "error"
                result["error"] = f"purge succeeded but live index read failed: {exc}"
                return result
            new_index = plan_snapshot_index_rewrite(live_index, purged_rels)
            if new_index is None:
                result["index_rewrite_skipped"] = (
                    "no live index.json to rewrite"
                    if not live_index
                    else "live index does not list the purged stores"
                )
                return result
        else:
            new_index = rewrite_index(index, purged_rels)
        try:
            write_index(bucket, dataset_id, new_index)
            result["index_rewritten"] = True
        except Exception as exc:  # noqa: BLE001 - surfaced; data is already deleted
            result["status"] = "error"
            result["error"] = f"purge succeeded but index rewrite failed: {exc}"

    return result


# --- CLI --------------------------------------------------------------------


def dataset_has_issue(result: dict) -> bool:
    """True if this dataset's result needs a human's attention: a hard
    error, any delete error, any rejected or anomalous candidate, any store
    found on S3 under an excluded tree that `index.json` never listed, or a
    failed drift-reconciliation check.

    This is the SINGLE source of truth both the bulk exit code and every
    per-dataset print statement use -- computing it independently in two
    places (an exit-code expression here, a print statement there) is
    exactly how the two silently drift apart, which is what happened before:
    the exit code omitted `extra_on_s3_not_in_index`/`extra_on_s3_check_error`
    even though the printed summary and the PR's own rollout guidance both
    depend on an operator noticing them. Scripting `--execute` off `$?` is
    the obvious way to gate a rollout on this tool, so the exit code has to
    agree with what gets printed.

    `no_snapshot` counts as an issue; `no_index` deliberately does not. The
    asymmetry is the point. A dataset with no `index.json` has nothing to purge
    and was correctly a no-op. A dataset with no SNAPSHOT was explicitly named
    as a target and then silently skipped -- a typo in the directory, a file
    never exported, the wrong path -- and it must not be reported as clean:
    zero purged and zero errors is the same output a genuinely clean dataset
    produces, so an operator gating `--execute` on `$?` would get a green light
    while a named target went untouched.
    """
    return bool(
        result.get("status") in ("error", "no_snapshot")
        or result.get("delete_errors")
        or result.get("rejected")
        or result.get("anomalies")
        or result.get("extra_on_s3_not_in_index")
        or result.get("extra_on_s3_check_error")
    )


def _default_audit_path() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"zarr-purge-audit-{ts}.json"


def _print_dataset_summary(result: dict) -> None:
    status = result.get("status")
    dataset_id = result.get("dataset_id")
    if status == "no_index":
        print(f"[purge] {dataset_id}: no zarr index.json -- nothing to do", flush=True)
        return
    if status == "no_snapshot":
        # Must NOT fall through to the counts line: zero purged / zero errors is
        # indistinguishable from a dataset that was genuinely checked and clean,
        # and this one was never checked at all.
        print(
            f"[purge] {dataset_id}: SKIPPED -- no index snapshot found for this "
            f"dataset; nothing was examined or deleted",
            flush=True,
        )
        return
    if status == "error":
        print(f"[purge] {dataset_id}: ERROR -- {result.get('error')}", flush=True)
        return
    verb = "purged" if result.get("execute") else "would purge"
    print(
        f"[purge] {dataset_id}: {len(result.get('purged', []))} store(s) {verb}, "
        f"{len(result.get('already_absent', []))} already absent, "
        f"{len(result.get('anomalies', [])) + len(result.get('rejected', []))} anomaly/rejected, "
        f"{len(result.get('delete_errors', []))} delete error(s), "
        f"{len(result.get('extra_on_s3_not_in_index', []))} store(s) on S3 not in index",
        flush=True,
    )
    if result.get("extra_on_s3_check_error"):
        # The S3-vs-index drift check itself failed -- distinct from finding
        # drift. This landed only in the audit JSON before; an operator
        # watching a long --all run on stdout alone would never see it.
        print(
            f"[purge] {dataset_id}: WARNING -- S3-vs-index drift reconciliation "
            f"failed: {result['extra_on_s3_check_error']}",
            flush=True,
        )


def _print_outcome_table(results: list[dict]) -> None:
    print("\n[purge] per-dataset outcome:", flush=True)
    header = (
        f"{'dataset':<14} {'status':<10} {'purged':>7} {'absent':>7} "
        f"{'flagged':>8} {'errors':>7} {'drift':>6} {'bytes_freed':>14}"
    )
    print(header, flush=True)
    print("-" * len(header), flush=True)
    for r in results:
        flagged = len(r.get("anomalies", [])) + len(r.get("rejected", []))
        drift = "ERR" if r.get("extra_on_s3_check_error") else str(len(r.get("extra_on_s3_not_in_index", [])))
        print(
            f"{r.get('dataset_id', ''):<14} {r.get('status', ''):<10} "
            f"{len(r.get('purged', [])):>7} {len(r.get('already_absent', [])):>7} "
            f"{flagged:>8} {len(r.get('delete_errors', [])):>7} {drift:>6} {r.get('bytes_freed', 0):>14}",
            flush=True,
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Delete already-published non-raw (derivatives/sourcedata/code) "
        "Zarr stores and rewrite index.json to match. Dry-run unless --execute."
    )
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--dataset", action="append", dest="datasets", metavar="ID",
        help="dataset id to purge; repeatable",
    )
    target.add_argument(
        "--all", action="store_true", help="purge every dataset found in the bucket"
    )
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument(
        "--execute", action="store_true",
        help="actually delete stores and rewrite index.json; omit for a dry run",
    )
    ap.add_argument(
        "--skip-extra-check", action="store_true",
        help="skip the S3-vs-index reconciliation listing (faster, less thorough)",
    )
    ap.add_argument("--audit-log", default=None, help="path for the JSON audit record")
    ap.add_argument(
        "--from-index-snapshot", default=None, metavar="DIR",
        help="read each dataset's store list from DIR/<dataset_id>.json (a saved "
        "index.json) instead of the live one. For stores stranded in S3 after a "
        "converter rebuild dropped them from the published index: the live index "
        "no longer lists them, so nothing else can target them. The rewrite is "
        "still computed from the LIVE index, never from the snapshot. With --all, "
        "the dataset set is the snapshots present in DIR.",
    )
    args = ap.parse_args(argv)

    snapshot_dir = args.from_index_snapshot
    if snapshot_dir and not Path(snapshot_dir).is_dir():
        print(f"[purge] --from-index-snapshot: no such directory: {snapshot_dir}", flush=True)
        return 2

    if args.datasets:
        dataset_ids = args.datasets
    elif snapshot_dir:
        dataset_ids = snapshot_dataset_ids(snapshot_dir)
    else:
        dataset_ids = list_dataset_ids(args.bucket)
    if args.all and not dataset_ids:
        where = snapshot_dir if snapshot_dir else f"s3://{args.bucket}/"
        print(f"[purge] no datasets found under {where}", flush=True)
    if snapshot_dir:
        print(
            f"[purge] snapshot mode: store lists read from {snapshot_dir} "
            f"({len(dataset_ids)} dataset(s)); index rewrites still computed from S3",
            flush=True,
        )

    audit_path = args.audit_log or _default_audit_path()
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    per_dataset: list[dict] = []
    for dataset_id in dataset_ids:
        print(
            f"[purge] {dataset_id}: starting ({'EXECUTE' if args.execute else 'dry-run'})",
            flush=True,
        )
        try:
            result = purge_dataset(
                args.bucket, dataset_id,
                execute=args.execute,
                check_extra=not args.skip_extra_check,
                snapshot_dir=snapshot_dir,
            )
        except Exception as exc:  # noqa: BLE001 - one dataset must never abort the batch
            result = {
                "dataset_id": dataset_id, "bucket": args.bucket, "execute": args.execute,
                "status": "error", "error": f"unhandled exception: {exc}",
            }
        per_dataset.append(result)
        _print_dataset_summary(result)

    report = {
        "format": AUDIT_FORMAT,
        "format_version": AUDIT_FORMAT_VERSION,
        "generated_utc": generated,
        "execute": args.execute,
        "bucket": args.bucket,
        "index_source": "snapshot" if snapshot_dir else "s3",
        "snapshot_dir": snapshot_dir,
        "datasets": per_dataset,
    }
    write_audit_log(audit_path, report)
    print(f"\n[purge] audit log written to {audit_path}", flush=True)
    _print_outcome_table(per_dataset)

    had_issue = any(dataset_has_issue(r) for r in per_dataset)
    if not args.execute:
        print(
            "\n[purge] DRY RUN -- nothing was deleted or rewritten. "
            "Re-run with --execute to apply.",
            flush=True,
        )
    return 1 if had_issue else 0


if __name__ == "__main__":
    sys.exit(main())
