#!/usr/bin/env python3
"""Backfill the `power_line_frequency` attr into already-converted Zarr stores.

The converter (generate_zarr.py) embeds BIDS PowerLineFrequency in every store it
writes, so NEW/changed datasets get it automatically. Datasets converted before
that landed keep attr-less stores until they happen to re-convert. This one-off
patches them cheaply: it resolves PowerLineFrequency per recording and rewrites
only each store's small root `zarr.json` on S3 (no re-conversion, no data move, no
viewer downtime), then optionally purges the CDN so the viewer sees it at once.

This is the first piece of the admin zarr backfill (epic #684, Task E). It runs
where the driver runs (Hallu / Actions): git + aws + uv/python + nemar CLI.

Per dataset:
  nemar dataset download <id> --no-data -o <tmp>   # working tree w/ json sidecars
  read s3://<bucket>/<id>/zarr/index.json          # the list of stores
  for each store entry: resolve PowerLineFrequency for its recording, and if the
    store's root zarr.json lacks/differs that value, patch + re-upload it (and the
    index entry); collect the changed URLs and purge the CDN.

Usage:
  python3 patch_power_line_frequency.py --dataset on007139 [--dataset nm000132] --apply
  python3 patch_power_line_frequency.py --all --apply          # every converted dataset
  (omit --apply for a dry run; nothing is written.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_zarr import (  # type: ignore[import-not-found]  # noqa: E402  (sibling via sys.path)
    event_descriptions_for,
    git_ls_files,
    power_line_frequency_for,
)

DATASET_ID_RE = re.compile(r"^(nm|on)[0-9]{6}$")


# --- pure helpers (unit-tested) ----------------------------------------------


def store_meta_key(dataset_id: str, zarr_rel: str) -> str:
    """S3 key of a store's root group metadata (`<id>/zarr/<rel>.zarr/zarr.json`)."""
    return f"{dataset_id}/zarr/{zarr_rel.strip('/')}/zarr.json"


def events_meta_key(dataset_id: str, zarr_rel: str) -> str:
    """S3 key of a store's events group metadata (`<id>/zarr/<rel>.zarr/events/zarr.json`)."""
    return f"{dataset_id}/zarr/{zarr_rel.strip('/')}/events/zarr.json"


def current_plf(doc: dict) -> object:
    """The `power_line_frequency` currently in a zarr.json doc (None if unset)."""
    attrs = doc.get("attributes")
    return attrs.get("power_line_frequency") if isinstance(attrs, dict) else None


def set_plf(doc: dict, plf: float) -> dict:
    """Set `power_line_frequency` in a zarr.json doc's attributes (mutates + returns)."""
    doc.setdefault("attributes", {})["power_line_frequency"] = plf
    return doc


def current_value_descriptions(doc: dict) -> dict | None:
    """The `value_descriptions` currently in a zarr.json doc (None if unset)."""
    attrs = doc.get("attributes")
    vd = attrs.get("value_descriptions") if isinstance(attrs, dict) else None
    return vd if isinstance(vd, dict) else None


def set_value_descriptions(doc: dict, descs: dict[str, str]) -> dict:
    """Set `value_descriptions` in a zarr.json doc's attributes (mutates + returns)."""
    doc.setdefault("attributes", {})["value_descriptions"] = descs
    return doc


def plan_patches(index_stores: list[dict], resolve) -> list[tuple[str, str, float]]:
    """For each store entry, resolve its recording's PowerLineFrequency. Returns
    (zarr_rel, recording_path, plf) for the entries that declare one. `resolve` is
    a callable path -> float|None. The caller still compares against the live
    zarr.json before writing (so a re-run is a no-op)."""
    out: list[tuple[str, str, float]] = []
    for e in index_stores:
        zarr_rel = e.get("zarr")
        path = e.get("path")
        if not isinstance(zarr_rel, str) or not isinstance(path, str):
            continue
        plf = resolve(path)
        if plf is not None:
            out.append((zarr_rel, path, plf))
    return out


# --- I/O ---------------------------------------------------------------------


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def s3_get_text(bucket: str, key: str) -> str | None:
    try:
        return _run(["aws", "s3", "cp", f"s3://{bucket}/{key}", "-", "--only-show-errors"]).stdout
    except subprocess.CalledProcessError:
        return None


def s3_put_text(bucket: str, key: str, text: str, content_type: str) -> None:
    subprocess.run(
        [
            "aws", "s3", "cp", "-", f"s3://{bucket}/{key}",
            "--content-type", content_type,
            "--cache-control", "public, max-age=86400",
            "--only-show-errors",
        ],
        check=True,
        input=text,
        text=True,
    )


def fetch_done_datasets(api_base: str) -> list[str]:
    """Public nm/on datasets that have a zarr index.json on S3 are converted. We
    page the API for public datasets; the caller filters to those with an index."""
    base, out, offset = api_base.rstrip("/"), [], 0
    while True:
        req = urllib.request.Request(
            f"{base}/datasets?limit=200&offset={offset}",
            headers={"User-Agent": "nemar-zarr-patch/1.0"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - trusted NEMAR API
            payload = json.loads(resp.read().decode("utf-8"))
        rows = payload.get("datasets", []) or []
        for d in rows:
            did = str(d.get("dataset_id", ""))
            if d.get("visibility") == "public" and DATASET_ID_RE.match(did) and did != "nm099999":
                out.append(did)
        offset += len(rows)
        if not rows or offset >= int(payload.get("total_count", 0) or 0):
            break
    return out


def purge_cdn(urls: list[str]) -> bool:
    """Best-effort Cloudflare cache purge of the patched URLs. Needs CF_PURGE_TOKEN
    + CF_ZONE_ID in the env; otherwise prints the URLs for a manual purge."""
    token, zone = os.environ.get("CF_PURGE_TOKEN"), os.environ.get("CF_ZONE_ID")
    if not (token and zone):
        print(f"  (no CF_PURGE_TOKEN/CF_ZONE_ID; purge these {len(urls)} URLs manually)")
        return False
    body = json.dumps({"files": urls}).encode()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - Cloudflare API
            ok = json.loads(resp.read().decode("utf-8")).get("success", False)
        print(f"  CDN purge: {'ok' if ok else 'FAILED'} ({len(urls)} urls)")
        return bool(ok)
    except OSError as exc:
        print(f"  CDN purge error: {exc}")
        return False


def patch_dataset(
    dataset_id: str, bucket: str, purge_base: str, apply: bool, tmp_root: str
) -> dict:
    """Patch one dataset's stores. Returns a small summary dict."""
    index_text = s3_get_text(bucket, f"{dataset_id}/zarr/index.json")
    if index_text is None:
        return {"dataset": dataset_id, "skipped": "no zarr index (not converted)"}
    index = json.loads(index_text)
    stores = index.get("stores") if isinstance(index.get("stores"), list) else []
    if not stores:
        return {"dataset": dataset_id, "skipped": "empty index"}

    repo = os.path.join(tmp_root, dataset_id)
    subprocess.run(
        ["nemar", "dataset", "download", dataset_id, "--no-data", "-o", repo],
        check=True,
        capture_output=True,
        text=True,
    )
    head_files = set(git_ls_files(repo, "HEAD"))

    def resolve(path: str) -> float | None:
        return power_line_frequency_for(repo, path, head_files, "HEAD")

    def resolve_descs(path: str) -> dict[str, str]:
        return event_descriptions_for(repo, path, head_files, "HEAD")

    planned = plan_patches(stores, resolve)
    patched, urls, by_zarr = 0, [], {}
    for zarr_rel, _, plf in planned:
        by_zarr[zarr_rel] = plf
        key = store_meta_key(dataset_id, zarr_rel)
        doc_text = s3_get_text(bucket, key)
        if doc_text is None:
            continue
        doc = json.loads(doc_text)
        if current_plf(doc) == plf:
            continue  # already patched -> idempotent skip
        set_plf(doc, plf)
        if apply:
            s3_put_text(bucket, key, json.dumps(doc), "application/json")
        urls.append(f"{purge_base.rstrip('/')}/{key}")
        patched += 1

    # Patch event descriptions into the events group zarr.json for each store.
    desc_patched = 0
    for e in stores:
        zarr_rel = e.get("zarr")
        path = e.get("path")
        if not isinstance(zarr_rel, str) or not isinstance(path, str):
            continue
        descs = resolve_descs(path)
        if not descs:
            continue
        ekey = events_meta_key(dataset_id, zarr_rel)
        edoc_text = s3_get_text(bucket, ekey)
        if edoc_text is None:
            continue  # no events group in this store -> skip
        edoc = json.loads(edoc_text)
        if current_value_descriptions(edoc) == descs:
            continue  # already equal -> idempotent skip
        set_value_descriptions(edoc, descs)
        if apply:
            s3_put_text(bucket, ekey, json.dumps(edoc), "application/json")
        urls.append(f"{purge_base.rstrip('/')}/{ekey}")
        desc_patched += 1

    # Keep the index entries consistent so a later read carries the value too.
    if patched and by_zarr:
        changed_index = False
        for e in stores:
            plf = by_zarr.get(e.get("zarr"))
            if plf is not None and e.get("power_line_frequency") != plf:
                e["power_line_frequency"] = plf
                changed_index = True
        if changed_index and apply:
            s3_put_text(bucket, f"{dataset_id}/zarr/index.json", json.dumps(index), "application/json")
            urls.append(f"{purge_base.rstrip('/')}/{dataset_id}/zarr/index.json")

    if apply and urls:
        purge_cdn(urls)
    return {
        "dataset": dataset_id,
        "stores": len(stores),
        "declared_plf": len(planned),
        "patched": patched,
        "desc_patched": desc_patched,
        "apply": apply,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill power_line_frequency into converted zarr stores")
    ap.add_argument("--bucket", default=os.environ.get("ZARR_BUCKET", "nemar"))
    ap.add_argument("--api-base", default=os.environ.get("NEMAR_API_BASE", "https://api.nemar.org"))
    ap.add_argument("--purge-base", default=os.environ.get("ZARR_CACHE_BASE_URL", "https://zarr.nemar.org"))
    ap.add_argument("--dataset", action="append", default=[], help="dataset id (repeatable)")
    ap.add_argument("--all", action="store_true", help="every public converted dataset")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    datasets = list(args.dataset)
    if args.all:
        datasets += [d for d in fetch_done_datasets(args.api_base) if d not in datasets]
    if not datasets:
        ap.error("pass --dataset <id> (repeatable) or --all")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] patching {len(datasets)} dataset(s) on bucket {args.bucket}")
    totals = {"datasets": 0, "plf": 0, "desc": 0}
    with tempfile.TemporaryDirectory() as tmp:
        for did in datasets:
            try:
                res = patch_dataset(did, args.bucket, args.purge_base, args.apply, tmp)
            except subprocess.CalledProcessError as exc:
                print(f"  {did}: ERROR {(exc.stderr or exc).__str__()[:200]}")
                continue
            print(f"  {res}")
            if "patched" in res:
                totals["datasets"] += 1
                totals["plf"] += res.get("patched", 0)
                totals["desc"] += res.get("desc_patched", 0)
    print(
        f"[{mode}] done: {totals['plf']} PLF + {totals['desc']} description patch(es) "
        f"across {totals['datasets']} dataset(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
