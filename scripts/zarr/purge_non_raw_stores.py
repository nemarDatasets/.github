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
* The pure selection/guard/rewrite/parsing logic is unit tested directly, with
  no mocking of business logic, in `test_purge_non_raw_stores.py`. The actual
  S3 list/delete/read/write calls are thin wrappers around that logic and are
  NOT exercised by the automated test suite here -- see the PR description
  for exactly what that leaves unverified.
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


# --- Pure: candidate selection -------------------------------------------


def select_purge_candidates(index: dict) -> tuple[list[dict], list[dict]]:
    """Split `index["stores"]` into `(candidates, anomalies)`.

    A candidate is a store entry whose `zarr` rel-path the imported
    `is_excluded_from_discovery` predicate says is excluded (under
    derivatives/sourcedata/code, or a reserved BIDS calibration filename --
    neither can be a raw recording). An entry only reaches `candidates` when
    its `path` field (the original BIDS-relative source path), if present,
    AGREES with `zarr` on that question -- a disagreement (a `path` that
    looks like an ordinary `sub-*/` recording paired with a `zarr` that looks
    excluded) is exactly the shape a corrupted or hand-edited index entry
    would have, and is routed to `anomalies` instead of ever being purged.

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
        if isinstance(path, str) and path and not is_excluded_from_discovery(path):
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
    """
    targets: list[dict] = []
    rejected: list[dict] = []
    for entry in candidates:
        rel_store = entry["zarr"]
        try:
            prefix = safe_store_prefix(bucket, dataset_id, rel_store)
            assert_within_zarr_prefix(prefix, bucket=bucket, dataset_id=dataset_id)
        except (ValueError, AssertionError) as exc:
            rejected.append({"entry": entry, "reason": str(exc)})
            continue
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


# --- Pure: S3 listing-output parsing -------------------------------------

_TOTAL_OBJECTS_RE = re.compile(r"^Total Objects:\s*(\d+)\s*$", re.MULTILINE)
_TOTAL_SIZE_RE = re.compile(r"^\s*Total Size:\s*(\d+)\s*$", re.MULTILINE)


def parse_s3_ls_summary(output: str) -> tuple[int, int]:
    """Parse `aws s3 ls --recursive --summarize`'s trailing summary lines into
    `(object_count, total_bytes)`.

    Both default to 0 when the prefix holds no objects at all -- `aws s3 ls`
    prints nothing whatsoever (not even the summary lines) for a prefix that
    matches zero keys -- or when the summary lines are absent for any other
    reason. Absence is always read as "empty," never as "unknown," which is
    the conservative direction for this tool: it only ever makes a store look
    like it needs no deletion, never the reverse.
    """
    objects_match = _TOTAL_OBJECTS_RE.search(output)
    size_match = _TOTAL_SIZE_RE.search(output)
    count = int(objects_match.group(1)) if objects_match else 0
    total_bytes = int(size_match.group(1)) if size_match else 0
    return count, total_bytes


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
    if res.returncode != 0:
        raise RuntimeError(f"aws s3 ls {key_prefix} exited {res.returncode}: {res.stderr.strip()}")
    return parse_s3_ls_summary(res.stdout)


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
    """
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
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
    }
    index_key = f"{dataset_id}/zarr/index.json"
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

    purged_rels: set[str] = set()
    for target in targets:
        rel_store = target["rel_store"]
        entry = target["entry"]
        try:
            count, total_bytes = stat_prefix(bucket, target["key_prefix"])
        except Exception as exc:  # noqa: BLE001 - collected, other targets still run
            result["delete_errors"].append(
                {"zarr": rel_store, "path": entry.get("path"), "stage": "stat", "error": str(exc)}
            )
            continue

        record = {
            "dataset_id": dataset_id,
            "path": entry.get("path"),
            "zarr": rel_store,
            "key_prefix": target["key_prefix"],
            "object_count": count,
            "bytes": total_bytes,
        }

        if count == 0:
            # Confirmed gone already -- most likely a prior, partial --execute
            # run. Never call _rm_recursive on it; just fold it into the set
            # the index rewrite drops, so a re-run converges instead of
            # erroring or double-counting.
            result["already_absent"].append(record)
            purged_rels.add(rel_store)
            continue

        if not execute:
            result["purged"].append(record)  # "would purge" under dry-run
            continue

        try:
            # Redundant, deliberate re-check immediately before the delete
            # call itself (requirement: a guard right before each delete).
            assert_within_zarr_prefix(target["key_prefix"], bucket=bucket, dataset_id=dataset_id)
            _rm_recursive(target["key_prefix"])
        except Exception as exc:  # noqa: BLE001 - collected, other targets still run
            result["delete_errors"].append(
                {"zarr": rel_store, "path": entry.get("path"), "stage": "delete", "error": str(exc)}
            )
            continue

        result["purged"].append(record)
        result["bytes_freed"] += total_bytes
        result["objects_freed"] += count
        purged_rels.add(rel_store)

    if check_extra:
        try:
            on_s3 = discover_excluded_stores(bucket, dataset_id)
            indexed_rels = {c["zarr"] for c in candidates}
            result["extra_on_s3_not_in_index"] = sorted(on_s3 - indexed_rels)
        except Exception as exc:  # noqa: BLE001 - best-effort reconciliation only
            result["extra_on_s3_check_error"] = str(exc)

    if execute and purged_rels:
        new_index = rewrite_index(index, purged_rels)
        try:
            write_index(bucket, dataset_id, new_index)
            result["index_rewritten"] = True
        except Exception as exc:  # noqa: BLE001 - surfaced; data is already deleted
            result["status"] = "error"
            result["error"] = f"purge succeeded but index rewrite failed: {exc}"

    return result


# --- CLI --------------------------------------------------------------------


def _default_audit_path() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"zarr-purge-audit-{ts}.json"


def _print_dataset_summary(result: dict) -> None:
    status = result.get("status")
    dataset_id = result.get("dataset_id")
    if status == "no_index":
        print(f"[purge] {dataset_id}: no zarr index.json -- nothing to do", flush=True)
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


def _print_outcome_table(results: list[dict]) -> None:
    print("\n[purge] per-dataset outcome:", flush=True)
    header = (
        f"{'dataset':<14} {'status':<10} {'purged':>7} {'absent':>7} "
        f"{'flagged':>8} {'errors':>7} {'bytes_freed':>14}"
    )
    print(header, flush=True)
    print("-" * len(header), flush=True)
    for r in results:
        flagged = len(r.get("anomalies", [])) + len(r.get("rejected", []))
        print(
            f"{r.get('dataset_id', ''):<14} {r.get('status', ''):<10} "
            f"{len(r.get('purged', [])):>7} {len(r.get('already_absent', [])):>7} "
            f"{flagged:>8} {len(r.get('delete_errors', [])):>7} {r.get('bytes_freed', 0):>14}",
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
    args = ap.parse_args(argv)

    dataset_ids = args.datasets if args.datasets else list_dataset_ids(args.bucket)
    if args.all and not dataset_ids:
        print(f"[purge] no datasets found under s3://{args.bucket}/", flush=True)

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
        "datasets": per_dataset,
    }
    write_audit_log(audit_path, report)
    print(f"\n[purge] audit log written to {audit_path}", flush=True)
    _print_outcome_table(per_dataset)

    had_issue = any(
        r.get("status") == "error" or r.get("delete_errors") or r.get("rejected") or r.get("anomalies")
        for r in per_dataset
    )
    if not args.execute:
        print(
            "\n[purge] DRY RUN -- nothing was deleted or rewritten. "
            "Re-run with --execute to apply.",
            flush=True,
        )
    return 1 if had_issue else 0


if __name__ == "__main__":
    sys.exit(main())
