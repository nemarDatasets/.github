#!/usr/bin/env python3
"""Tier-2: fill records.json `recording_duration` via a biosigIO full read.

Best-effort enrichment that runs AFTER emit_records.py + neuroschema validation
and BEFORE the S3 upload, patching records.json in place. For each record whose
`signal_summary.recording_duration` is null (the BIDS sidecar lacked
`RecordingDuration` -- the Tier-1 git-walk could not resolve it), download the
primary recording from S3 (git-annex object) and open it with biosigIO to read
the duration. Mirrors the idempotent best-effort shape of
scripts/zarr/patch_power_line_frequency.py.

PRINCIPLE (load-bearing): biosigIO is the single format authority. This script
does NO format-specific parsing -- it resolves the file (git-annex/S3, stable
plumbing, not format logic) and hands the bytes to biosigIO. Support for new or
odd formats (CTF .ds, KIT, .snirf) is added by a biosigIO *importer*, never by a
NEMAR-side parser. Recordings biosigIO cannot read stay recording_duration:null
(graceful degradation).

Idempotent + best-effort: any per-file failure (unreadable, biosigIO ImportError,
unsupported format) emits a ::warning:: and leaves the duration null; the run
never fails. Re-running re-fills the same nulls deterministically.

Usage:
    patch_duration.py --records-path /tmp/out/records.json \\
        --dataset-id nm099999 --version 1.0.0 --repo-dir /tmp/repo
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Reuse the canonical NEMAR path -> annex-key -> S3 download from the Zarr
# converter. generate_zarr.py's top-level imports are stdlib (biosigio/zarr are
# lazy, inside functions) and it has a __main__ guard, so importing it is cheap
# and side-effect-free. materialize_recording also pulls same-stem siblings
# (e.g. a BrainVision .vhdr's .eeg/.vmrk), which biosigIO needs to open them.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "zarr"))
import generate_zarr  # noqa: E402


def list_head_files(repo_dir: str, tag: str) -> set[str]:
    """All tracked paths at the tag (the corpus materialize_recording resolves
    siblings against)."""
    out = generate_zarr._run(["git", "-C", repo_dir, "ls-tree", "-r", "--name-only", tag])
    return {line.strip() for line in out.splitlines() if line.strip()}


def read_duration(
    *,
    repo_dir: str,
    bucket: str,
    dataset_id: str,
    relpath: str,
    head_files: set[str],
    tag: str,
) -> tuple[float, int]:
    """biosigIO full read of one recording -> (recording_duration_s, n_samples).

    Downloads the primary (+ same-stem siblings) into a temp dir via the Zarr
    converter's materialize_recording, then hands the local path to biosigIO.
    Raises on any failure (the caller turns that into a warning + null). NO
    format-specific parsing -- biosigIO owns all format knowledge.
    """
    from biosigio import Recording  # lazy: only imported when a gap needs filling

    work = tempfile.mkdtemp(prefix="rec-dur-")
    try:
        primary_local, _events, _key = generate_zarr.materialize_recording(
            repo_dir, bucket, dataset_id, relpath, head_files, tag, work
        )
        rec = Recording.from_file(primary_local)
        return float(rec.get_duration()), int(rec.get_n_samples())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def patch_records(
    records: list[dict],
    *,
    repo_dir: str,
    bucket: str,
    dataset_id: str,
    version: str,
) -> tuple[int, int]:
    """Fill null recording_duration (and null ntimes) in place via biosigIO.

    Returns (filled, gaps). A record is a "gap" when its signal_summary exists
    and recording_duration is None. Already-resolved durations are never
    touched (idempotent). ntimes is only set when it too is null, so a Tier-1
    ntimes is preserved.
    """
    bare = version.lstrip("v")
    tag = f"v{bare}"
    gaps = [
        r
        for r in records
        if isinstance(r.get("signal_summary"), dict)
        and r["signal_summary"].get("recording_duration") is None
    ]
    if not gaps:
        print("[patch_duration] no null-duration records; nothing to do", flush=True)
        return 0, 0

    head_files = list_head_files(repo_dir, tag)
    filled = 0
    for rec in gaps:
        relpath = rec.get("bids_relpath")
        if not isinstance(relpath, str) or not relpath:
            print(
                "::warning::[patch_duration] record missing bids_relpath; skipping",
                file=sys.stderr,
                flush=True,
            )
            continue
        try:
            dur, nsamp = read_duration(
                repo_dir=repo_dir,
                bucket=bucket,
                dataset_id=dataset_id,
                relpath=relpath,
                head_files=head_files,
                tag=tag,
            )
        except Exception as e:  # noqa: BLE001 - best-effort by design
            print(
                f"::warning::[patch_duration] {relpath}: biosigIO read failed, leaving "
                f"recording_duration null ({type(e).__name__}: {e})",
                file=sys.stderr,
                flush=True,
            )
            continue
        ss = rec["signal_summary"]
        ss["recording_duration"] = dur
        if ss.get("ntimes") is None:
            ss["ntimes"] = nsamp
        filled += 1

    return filled, len(gaps)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Tier-2: fill records.json recording_duration via a biosigIO full read.",
    )
    p.add_argument("--records-path", required=True, help="path to records.json (patched in place)")
    p.add_argument("--dataset-id", required=True)
    p.add_argument("--version", required=True, help="X.Y.Z or vX.Y.Z; tag is v<X.Y.Z>")
    p.add_argument("--repo-dir", required=True, help="path to the already-cloned dataset repo")
    p.add_argument(
        "--bucket",
        default=os.environ.get("S3_BUCKET", "nemar"),
        help="S3 bucket holding <id>/objects/<annexKey> (default: $S3_BUCKET or 'nemar')",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records_path = Path(args.records_path)
    records = json.loads(records_path.read_text())
    if not isinstance(records, list):
        print("::error::[patch_duration] records.json is not a JSON array", file=sys.stderr)
        return 2

    filled, gaps = patch_records(
        records,
        repo_dir=args.repo_dir,
        bucket=args.bucket,
        dataset_id=args.dataset_id,
        version=args.version,
    )
    records_path.write_text(json.dumps(records, indent=2))

    print(
        f"[patch_duration] dataset={args.dataset_id} version={args.version.lstrip('v')} "
        f"gaps={gaps} filled={filled} still_null={gaps - filled}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
