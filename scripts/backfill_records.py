#!/usr/bin/env python3
"""Dispatch the generate-records workflow across NEMAR datasets (epic #615 P6).

One-time, idempotent backfill that fires the central `generate-records.yml`
workflow (via `workflow_dispatch`) for each public `nm`/`on` dataset, so every
dataset gets a records.json snapshot at
`s3://nemar/<id>/version/v<X.Y.Z>-records.json`, served at
`data.nemar.org/<id>/<version>/records.json`. Dataset enumeration mirrors
`scripts/zarr/patch_power_line_frequency.py` (paginated GET /datasets).

DRY-RUN by default; pass `--apply` to actually dispatch. Idempotent: the
records.json emit is a pure function of the immutable tag, so re-dispatching
overwrites the same S3 key deterministically -- safe to re-run.

Prerequisites:
  * The records.json data-plane route is live on prod (uploaded snapshots are
    inert until then, but harmless -- they just sit in S3).
  * `gh` is authenticated with workflow:write on nemarDatasets/.github.

Usage:
  backfill_records.py --all                          # dry-run: latest version of every public nm/on dataset
  backfill_records.py --all --apply                  # dispatch
  backfill_records.py --dataset nm000132 --apply      # one dataset (repeatable)
  backfill_records.py --all --all-versions --apply    # every published version, not just latest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

DATASET_ID_RE = re.compile(r"^(nm|on)\d+$")
WORKFLOW = "generate-records.yml"
DEFAULT_REPO = "nemarDatasets/.github"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "nemar-records-backfill"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - trusted NEMAR API
        return json.loads(resp.read().decode())


def fetch_public_datasets(api_base: str, *, page: int = 200) -> list[dict]:
    """All dataset rows from the paginated GET /datasets API."""
    base = api_base.rstrip("/")
    out: list[dict] = []
    offset = 0
    while True:
        d = _get_json(f"{base}/datasets?limit={page}&offset={offset}")
        rows = d.get("datasets", [])
        out.extend(rows)
        offset += page
        total = int(d.get("total_count", len(out)))
        if not rows or offset >= total:
            break
    return out


def select_datasets(rows: list[dict], *, exclude: set[str]) -> list[tuple[str, str]]:
    """(dataset_id, latest_version) for PUBLIC `nm`/`on` datasets, excluding
    `exclude` and any non-nm/on id (e.g. `xx` sandbox), private row, or row with
    no published version. Pure + unit-testable (no network). latest_version is
    normalised to bare (no leading `v`)."""
    picks: list[tuple[str, str]] = []
    for r in rows:
        ds = r.get("dataset_id") or ""
        if r.get("visibility") != "public":
            continue
        if not DATASET_ID_RE.match(ds) or ds in exclude:
            continue
        ver = str(r.get("latest_version") or "").lstrip("v")
        if not ver:
            continue
        picks.append((ds, ver))
    return sorted(picks)


def versions_for(api_base: str, dataset_id: str) -> list[str]:
    """All published versions (bare) for a dataset, from the data-plane index
    GET /data/<id>."""
    base = api_base.rstrip("/")
    d = _get_json(f"{base}/data/{dataset_id}")
    return [
        str(v.get("version", "")).lstrip("v")
        for v in d.get("versions", [])
        if v.get("version")
    ]


def build_plan(
    selected: list[tuple[str, str]],
    *,
    all_versions: bool,
    versions_lookup,
) -> list[tuple[str, str]]:
    """Expand (dataset, latest) pairs into the (dataset, version) dispatch plan.
    `versions_lookup(dataset_id) -> list[str]` is injected so the planning logic
    is testable without the network."""
    plan: list[tuple[str, str]] = []
    for ds, latest in selected:
        if all_versions:
            for v in versions_lookup(ds):
                plan.append((ds, v))
        elif latest:
            plan.append((ds, latest))
    return plan


def dispatch(repo: str, dataset_id: str, version: str) -> None:
    subprocess.run(
        [
            "gh", "workflow", "run", WORKFLOW, "--repo", repo,
            "-f", f"dataset_id={dataset_id}", "-f", f"version={version}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Dispatch generate-records across NEMAR datasets (epic #615 P6).",
    )
    ap.add_argument("--api-base", default=os.environ.get("NEMAR_API_BASE", "https://api.nemar.org"))
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--dataset", action="append", default=[], help="dataset id (repeatable)")
    ap.add_argument("--all", action="store_true", help="every public nm/on dataset")
    ap.add_argument(
        "--all-versions", action="store_true", help="every published version (default: latest only)"
    )
    ap.add_argument(
        "--exclude",
        action="append",
        default=["nm099999"],
        help="dataset id to skip (repeatable; default: nm099999 test dataset)",
    )
    ap.add_argument("--sleep", type=float, default=0.5, help="seconds between dispatches (pacing)")
    ap.add_argument("--apply", action="store_true", help="actually dispatch (default: dry-run)")
    args = ap.parse_args(argv)
    if not args.all and not args.dataset:
        ap.error("pass --dataset <id> (repeatable) or --all")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    exclude = set(args.exclude)

    rows = fetch_public_datasets(args.api_base)
    if args.all:
        selected = select_datasets(rows, exclude=exclude)
    else:
        by_id = {r.get("dataset_id"): r for r in rows}
        selected = []
        for ds in args.dataset:
            r = by_id.get(ds)
            if not r or r.get("visibility") != "public":
                print(f"::warning::{ds} not found / not public; skipping", file=sys.stderr, flush=True)
                continue
            selected.append((ds, str(r.get("latest_version") or "").lstrip("v")))

    plan = build_plan(
        selected,
        all_versions=args.all_versions,
        versions_lookup=lambda ds: versions_for(args.api_base, ds),
    )

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"[backfill_records] {len(plan)} dispatch(es) planned ({mode}; "
        f"datasets={len(selected)}, all_versions={args.all_versions}, repo={args.repo})",
        flush=True,
    )
    dispatched = 0
    for ds, ver in plan:
        if not args.apply:
            print(f"  would dispatch: {ds}  version={ver}")
            continue
        try:
            dispatch(args.repo, ds, ver)
            dispatched += 1
        except subprocess.CalledProcessError as e:
            print(f"::warning::dispatch failed {ds}@{ver}: {e}", file=sys.stderr, flush=True)
            continue
        time.sleep(args.sleep)
    if args.apply:
        print(f"[backfill_records] dispatched {dispatched}/{len(plan)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
