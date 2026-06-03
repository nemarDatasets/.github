#!/usr/bin/env python3
"""NEMAR Zarr serving-copy generator (epic nemarOrg/nemar-cli#684, Stream B).

Runs in nemarDatasets/.github :: run-generate-zarr.yml. Converts the BIDS
recordings that changed since the last conversion into per-recording biosigIO
Zarr v3 serving stores, uploads them to ``s3://<bucket>/<id>/zarr/...``
(LATEST-ONLY: overwrite in place, delete on source removal), maintains
``s3://<bucket>/<id>/zarr/index.json``, and writes a callback body the workflow
POSTs to ``/webhooks/zarr-ready``.

The conversion itself is biosigIO (``Recording.from_file -> bids.apply_events_tsv
-> rec.to_zarr``); this driver owns the BIDS-tree orchestration: change
detection, annex-content materialisation, S3 sync, and the index.

Design notes
------------
* The dataset repo is cloned by the workflow (full history, ``--no-checkout``);
  this script reads the tree with git plumbing (``ls-tree``/``cat-file``/``diff``)
  exactly like ``emit_manifest.py``, and pulls annex *content* from
  ``s3://<bucket>/<id>/objects/<key>`` with authenticated ``aws s3 cp`` (works for
  private datasets, unlike the archive workflow's public-HTTP fetch).
* Incremental: the prior ``index.json`` records the commit it was built from;
  we ``git diff <prior>..HEAD`` and convert only the affected recordings, mapping
  a changed companion (``.fdt``/``.eeg``/``.vmrk``) or ``*_events.tsv`` back to its
  sibling recording. ``--full`` (or a missing/!ancestor prior) converts everything.
* The pure helpers (path classification, worklist, index merge) carry the logic
  and are unit-tested in ``test_generate_zarr.py``; the I/O lives in ``main``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone

# --- Path classification -------------------------------------------------

# Primary recording containers biosigIO reads directly. A change to one of
# these (or its companion / events sidecar) rebuilds exactly one `.zarr` store.
PRIMARY_EXTS = (".set", ".edf", ".bdf", ".vhdr", ".fif")
# Companions that share a recording's filename stem and carry its samples or
# markers; a change confined to one still rebuilds the recording's store.
COMPANION_EXTS = (".fdt", ".eeg", ".vmrk")

INDEX_FORMAT = "nemar-zarr-index"
INDEX_FORMAT_VERSION = 1

# Per-modality canonical rate caps (Hz) passed to to_zarr. Keys are biosigIO's
# uppercase modality names; the defaults already match, set explicitly so the
# NEMAR caps are visible/auditable here rather than implied by the library.
MODALITY_RATES = {"EEG": 250, "MEG": 250, "IEEG": 1000, "EMG": 1000}

# The serving group + rate are driven by the recording's BIDS datatype SUFFIX, not
# by per-channel type guessing. A `*_eeg.set` is EEG (250 Hz cap) even when a few
# EOG/REF/trigger channels ride along; biosigIO's EEGLAB importer can only see an
# empty chanlocs `type` and would otherwise fall back to OTHER -> MISC, yielding a
# `misc_1024hz` group (no cap) instead of the intended `eeg_250hz`. We force every
# channel's modality from the suffix so the whole recording lands in one coherent
# group at the modality's MODALITY_RATES cap.
_SUFFIX_MODALITY = {"eeg": "EEG", "meg": "MEG", "ieeg": "IEEG", "emg": "EMG"}


def bids_suffix_modality(path: str) -> str | None:
    """Modality from a recording's BIDS suffix (`sub-01_task-rest_eeg.set` -> EEG),
    or None when the trailing `_<suffix>` is not a known datatype. The rate cap
    then follows from MODALITY_RATES (EEG/MEG 250 Hz, IEEG/EMG 1000 Hz)."""
    stem = os.path.basename(path).rsplit(".", 1)[0]
    suffix = stem.rsplit("_", 1)[-1].lower() if "_" in stem else ""
    return _SUFFIX_MODALITY.get(suffix)

ANNEX_TARGET_RE = re.compile(r"\.git/annex/objects/[A-Za-z0-9]+/[A-Za-z0-9]+/([^/]+)/\1$")
ANNEX_POINTER_CONTENT_RE = re.compile(r"^/annex/objects/(.+)$")


def lower_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def is_primary(path: str) -> bool:
    return lower_ext(path) in PRIMARY_EXTS


def is_events_tsv(path: str) -> bool:
    return path.endswith("_events.tsv")


def filename_stem(path: str) -> str:
    """`sub-01/eeg/sub-01_task-x_eeg.vhdr` -> `sub-01_task-x_eeg`."""
    return os.path.splitext(os.path.basename(path))[0]


def entities_base(stem: str) -> str:
    """Drop the trailing BIDS suffix: `sub-01_task-x_eeg` -> `sub-01_task-x`."""
    return stem.rsplit("_", 1)[0] if "_" in stem else stem


def store_rel_for(primary_path: str) -> str:
    """`sub-01/eeg/sub-01_task-x_eeg.set` -> `sub-01/eeg/sub-01_task-x_eeg.zarr`.

    Strips the data extension and appends `.zarr`; the BIDS suffix (`_eeg`,
    `_emg`, ...) is preserved, so the rule is uniform across all primary exts.
    """
    root, _ = os.path.splitext(primary_path)
    return root + ".zarr"


def events_sibling_for(primary_path: str) -> str:
    """BIDS events sidecar path for a recording (suffix `_events`, ext `.tsv`).

    `sub-01/eeg/sub-01_task-x_eeg.set` -> `sub-01/eeg/sub-01_task-x_events.tsv`.
    """
    d = os.path.dirname(primary_path)
    base = entities_base(filename_stem(primary_path))
    name = f"{base}_events.tsv"
    return f"{d}/{name}" if d else name


def _bids_entities(stem: str) -> dict[str, str]:
    """Entity key->value pairs from a BIDS stem (`sub-01_task-x_run-2_eeg` ->
    {sub: 01, task: x, run: 2}); the trailing suffix token (no dash) is ignored."""
    ents: dict[str, str] = {}
    for tok in stem.split("_"):
        if "-" in tok:
            k, v = tok.split("-", 1)
            ents[k] = v
    return ents


def _read_repo_text(repo_dir: str, head: str, path: str) -> str | None:
    """Read a git-tracked text file at `head`. Uses the working tree when present
    (local/Hallu mode), else falls back to `git cat-file` -- the workflow clones
    `--no-checkout`, so there is no working tree there. None if unreadable."""
    try:
        with open(os.path.join(repo_dir, path), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        pass
    try:
        return subprocess.check_output(
            ["git", "-C", repo_dir, "cat-file", "blob", f"{head}:{path}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError):
        return None


def power_line_frequency_for(
    repo_dir: str, primary_path: str, head_files: set[str], head: str
) -> float | None:
    """BIDS PowerLineFrequency (Hz) for a recording, resolved via the inheritance
    principle: among the `_<suffix>.json` sidecars sitting in the recording's
    directory or an ancestor whose entities are a subset of the recording's, the
    most specific one that declares PowerLineFrequency wins. Returns None when none
    declare it (so the viewer leaves the notch off).

    Sidecars are git-tracked text (not annexed); they are read at `head` from the
    working tree when present and `git cat-file` otherwise, so this works in both
    the no-checkout workflow clone and the local/Hallu working tree -- one grep of
    the head file list, then a couple of small reads, no annex download.
    """
    stem = filename_stem(primary_path)
    suffix = stem.rsplit("_", 1)[-1].lower() if "_" in stem else ""
    if not suffix:
        return None
    rec_dir = os.path.dirname(primary_path)
    rec_ents = _bids_entities(stem)
    needle = f"_{suffix}.json"
    candidates: list[tuple[int, int, str]] = []
    for f in head_files:
        if not f.endswith(needle):
            continue
        cdir = os.path.dirname(f)
        # Applicable only if the sidecar is in the recording's dir or an ancestor.
        if cdir and rec_dir != cdir and not rec_dir.startswith(cdir + "/"):
            continue
        cents = _bids_entities(filename_stem(f))
        # ...and its entities must be a subset of the recording's.
        if any(rec_ents.get(k) != v for k, v in cents.items()):
            continue
        depth = cdir.count("/") + (1 if cdir else 0)
        candidates.append((depth, len(cents), f))
    candidates.sort()  # least specific first; the most specific value overrides
    plf: float | None = None
    for _, _, f in candidates:
        text = _read_repo_text(repo_dir, head, f)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        v = data.get("PowerLineFrequency")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            plf = float(v)
    return plf


def affected_primaries(changed_path: str, primaries_by_dir: dict[str, list[str]]) -> set[str]:
    """Primary recordings a changed path rebuilds, restricted to those at HEAD.

    A primary maps to itself; a companion (`.fdt`/`.eeg`/`.vmrk`) maps to the
    same-stem primary in its directory; a `*_events.tsv` maps to every primary
    in its directory sharing the events entities-base (handles a single events
    file shared across modality recordings in the same folder).
    """
    d = os.path.dirname(changed_path)
    here = primaries_by_dir.get(d, [])
    if is_primary(changed_path):
        return {changed_path} if changed_path in here else set()
    ext = lower_ext(changed_path)
    if ext in COMPANION_EXTS:
        stem = filename_stem(changed_path)
        return {p for p in here if filename_stem(p) == stem}
    if is_events_tsv(changed_path):
        ev_stem = filename_stem(changed_path)  # `sub-01_task-x_events`
        ev_base = ev_stem[: -len("_events")] if ev_stem.endswith("_events") else entities_base(ev_stem)
        return {p for p in here if entities_base(filename_stem(p)) == ev_base}
    return set()


def compute_worklist(
    head_files: list[str],
    diff_entries: list[tuple[str, str]],
    full: bool,
) -> tuple[list[str], list[str]]:
    """Return (convert, remove): primary source paths to (re)build, and store
    rel-paths (`*.zarr`) to delete.

    `diff_entries` is a list of (status, path) from `git diff --no-renames
    --name-status` (so a rename is a D + an A). `full` ignores the diff and
    converts every primary at HEAD.
    """
    head_set = set(head_files)
    primaries = [p for p in head_files if is_primary(p)]
    by_dir: dict[str, list[str]] = {}
    for p in primaries:
        by_dir.setdefault(os.path.dirname(p), []).append(p)

    if full:
        return sorted(primaries), []

    convert: set[str] = set()
    remove: set[str] = set()
    for status, path in diff_entries:
        if status == "D":
            if is_primary(path):
                # The recording itself is gone -> drop its store. (If a same-name
                # primary somehow still exists at HEAD it lands in convert below.)
                if path not in head_set:
                    remove.add(store_rel_for(path))
            else:
                # A companion/events removal still rebuilds any sibling recording
                # that remains (e.g. events.tsv deleted -> regenerate without events).
                convert |= affected_primaries(path, by_dir)
        else:  # "A", "M", "T", ...
            convert |= affected_primaries(path, by_dir)

    convert &= head_set  # never convert something not present at HEAD
    convert_stores = {store_rel_for(p) for p in convert}
    remove -= convert_stores  # a rebuilt store must not also be deleted
    return sorted(convert), sorted(remove)


def merge_index(
    prior: dict | None,
    dataset_id: str,
    head_commit: str,
    converted: list[dict],
    removed_store_rels: list[str],
    updated_utc: str,
) -> dict:
    """Fold this run's results into the prior index. Pure.

    `converted` is a list of store entries (each carries a `zarr` rel-path key);
    `removed_store_rels` are `*.zarr` rels to drop. Entries for unchanged stores
    are carried over from `prior` verbatim.
    """
    stores: dict[str, dict] = {}
    if prior and isinstance(prior.get("stores"), list):
        for entry in prior["stores"]:
            if isinstance(entry, dict) and isinstance(entry.get("zarr"), str):
                stores[entry["zarr"]] = entry
    for rel in removed_store_rels:
        stores.pop(rel, None)
    for entry in converted:
        stores[entry["zarr"]] = entry
    ordered = [stores[k] for k in sorted(stores)]
    return {
        "dataset_id": dataset_id,
        "format": INDEX_FORMAT,
        "format_version": INDEX_FORMAT_VERSION,
        "source_commit": head_commit,
        "updated_utc": updated_utc,
        "store_count": len(ordered),
        "stores": ordered,
    }


def parse_annex_key(blob_text: str) -> str | None:
    """Annex key from a locked-mode symlink target or an unlocked pointer blob."""
    t = blob_text.strip()
    m = ANNEX_TARGET_RE.search(t)
    if m:
        return m.group(1)
    m = ANNEX_POINTER_CONTENT_RE.match(t)
    return m.group(1) if m else None


# --- I/O (git, S3, conversion) ------------------------------------------


def _run(cmd: list[str], cwd: str | None = None) -> str:
    return subprocess.check_output(cmd, cwd=cwd, text=True)


def git_ls_files(repo_dir: str, ref: str) -> list[str]:
    out = _run(["git", "-C", repo_dir, "ls-tree", "-r", "--name-only", ref])
    return [line for line in out.splitlines() if line]


def git_diff_name_status(repo_dir: str, base: str, head: str) -> list[tuple[str, str]]:
    out = _run(
        ["git", "-C", repo_dir, "diff", "--no-renames", "--name-status", f"{base}..{head}"]
    )
    entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            entries.append((parts[0].strip()[:1], parts[-1].strip()))
    return entries


def is_ancestor(repo_dir: str, maybe_ancestor: str, head: str) -> bool:
    """True iff `maybe_ancestor` is an ancestor of `head`.

    `merge-base --is-ancestor` exits 0 (yes), 1 (no), or other (git error, e.g.
    an unknown commit after a history rewrite). A git error is treated as "not
    an ancestor" (so the run falls back to a full rebuild, which is correct for
    a rewritten prior commit) but is logged so it isn't mistaken for a clean no.
    """
    res = subprocess.run(
        ["git", "-C", repo_dir, "merge-base", "--is-ancestor", maybe_ancestor, head],
        capture_output=True,
        text=True,
    )
    if res.returncode not in (0, 1):
        print(
            f"::warning::merge-base --is-ancestor {maybe_ancestor[:8]}..{head[:8]} "
            f"exited {res.returncode}: {res.stderr.strip()}; treating as non-ancestor",
            flush=True,
        )
    return res.returncode == 0


def safe_store_prefix(bucket: str, dataset_id: str, rel_store: str) -> str:
    """Build the S3 prefix for a store, validating `rel_store` first.

    This prefix feeds `aws s3 sync --delete` and `aws s3 rm --recursive`, so an
    empty or path-traversal value could wipe an unintended prefix (e.g. the
    whole `<id>/zarr/`). Reject anything that isn't a clean `*.zarr` rel-path.
    """
    if not rel_store or not rel_store.endswith(".zarr"):
        raise ValueError(f"unsafe store rel-path {rel_store!r}: empty or not a .zarr")
    parts = rel_store.split("/")
    if rel_store.startswith("/") or "" in parts or ".." in parts:
        raise ValueError(f"unsafe store rel-path {rel_store!r}: traversal or empty segment")
    return f"s3://{bucket}/{dataset_id}/zarr/{rel_store}/"


def validate_store(store_local: str) -> None:
    """Raise if biosigIO produced an empty/partial store.

    Guards the `aws s3 sync --delete` below: syncing an empty local directory to
    a populated destination would DELETE a previously-valid store. A biosigIO
    Zarr v3 store always has a root `zarr.json`.
    """
    if not os.path.isdir(store_local) or not os.path.exists(os.path.join(store_local, "zarr.json")):
        raise RuntimeError(f"biosigIO wrote no zarr.json at {store_local}; store is empty/partial")


def aws_cp(src: str, dst: str, *, extra: list[str] | None = None) -> None:
    # --only-show-errors drops the per-file transfer progress meter; with JOBS
    # workers each streaming a ~100 MB blob, that meter otherwise floods the cron
    # log to the point of uselessness.
    subprocess.run(["aws", "s3", "cp", src, dst, "--only-show-errors", *(extra or [])], check=True)


def s3_read_json(bucket: str, key: str) -> dict | None:
    """Read a JSON object from S3.

    Returns None ONLY for a genuine 404 (NoSuchKey) -- the legitimate first-run
    case. Any other non-zero exit (credentials, network, wrong bucket) RAISES:
    silently treating it as "no prior index" would send the run full AND drop
    every prior store from the rewritten index. A corrupt body raises for the
    same reason (absent != corrupt).
    """
    res = subprocess.run(
        ["aws", "s3", "cp", f"s3://{bucket}/{key}", "-"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        err = res.stderr.lower()
        if "nosuchkey" in err or "404" in err or "not found" in err:
            return None
        raise RuntimeError(
            f"s3_read_json: aws s3 cp s3://{bucket}/{key} exited {res.returncode}: "
            f"{res.stderr.strip()}"
        )
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"corrupt JSON at s3://{bucket}/{key}: {exc}") from exc


def materialize_recording(
    repo_dir: str,
    bucket: str,
    dataset_id: str,
    primary_path: str,
    head_files: set[str],
    head: str,
    work_dir: str,
) -> tuple[str, str | None, str | None]:
    """Reconstruct a recording's file set into `work_dir`.

    Downloads the primary + every same-stem companion (annex content via
    authenticated `aws s3 cp`, in-git blobs written directly) and the BIDS
    `_events.tsv` sidecar if present. Returns (primary_local_path,
    events_local_path|None, primary_annex_key|None).
    """
    d = os.path.dirname(primary_path)
    stem = filename_stem(primary_path)
    siblings = [
        p
        for p in head_files
        if os.path.dirname(p) == d and filename_stem(p) == stem
    ]
    events_path = events_sibling_for(primary_path)
    wanted = list(dict.fromkeys([primary_path, *siblings]))
    if events_path in head_files:
        wanted.append(events_path)

    primary_key: str | None = None
    for path in wanted:
        local = os.path.join(work_dir, os.path.basename(path))
        # mode tells symlink (120000) vs regular blob; sha addresses the blob.
        # Read against the pinned `head` SHA (not the symbolic ref) so this sees
        # the same tree git_ls_files/compute_worklist were built from.
        meta = _run(["git", "-C", repo_dir, "ls-tree", head, "--", path]).strip()
        if not meta:
            if path == primary_path:
                raise RuntimeError(
                    f"primary {path!r} in the worklist but absent from ls-tree {head[:8]} "
                    "(possible pack corruption or path-encoding issue)"
                )
            print(f"::warning::companion {path!r} absent from ls-tree {head[:8]}; skipping", flush=True)
            continue
        mode, _, rest = meta.split(" ", 2)
        sha = rest.split("\t", 1)[0].strip()
        blob = subprocess.check_output(["git", "-C", repo_dir, "cat-file", "blob", sha])
        key = None
        if mode == "120000":
            key = parse_annex_key(blob.decode("utf-8", "replace"))
        elif len(blob) < 1024:
            key = parse_annex_key(blob.decode("utf-8", "replace"))
        if key:
            aws_cp(f"s3://{bucket}/{dataset_id}/objects/{key}", local)
            if path == primary_path:
                primary_key = key
        else:
            with open(local, "wb") as fh:
                fh.write(blob)
    return (
        os.path.join(work_dir, os.path.basename(primary_path)),
        os.path.join(work_dir, os.path.basename(events_path))
        if events_path in head_files
        else None,
        primary_key,
    )


def store_metadata(store_path: str) -> dict:
    """Read the small per-store summary the viewer/index needs from the written
    store's attrs (biosigIO contract: root `channel_groups`, group `rate`/
    `n_channels`/`n_samples`/`modality`). Best-effort: returns {} on any error.
    """
    try:
        import zarr  # type: ignore

        root = zarr.open_group(store_path, mode="r")
        ra = dict(root.attrs)
        groups = []
        modalities: set[str] = set()
        for gname in ra.get("channel_groups", []):
            ga = dict(root[gname].attrs)
            rate = ga.get("rate")
            nsamp = ga.get("n_samples")
            mod = ga.get("modality")
            if mod:
                modalities.add(str(mod).lower())
            groups.append(
                {
                    "name": gname,
                    "modality": mod,
                    "rate": rate,
                    "n_channels": ga.get("n_channels"),
                    "n_samples": nsamp,
                    "duration_s": (nsamp / rate) if rate and nsamp else None,
                }
            )
        # Count event descriptions when the events group exists and carries them.
        event_description_count: int | None = None
        if "events" in root:
            vd = dict(root["events"].attrs).get("value_descriptions")
            if isinstance(vd, dict):
                event_description_count = len(vd)
        result: dict = {
            "modalities": sorted(modalities),
            "groups": groups,
            "power_line_frequency": ra.get("power_line_frequency"),
        }
        if event_description_count is not None:
            result["event_description_count"] = event_description_count
        return result
    except Exception as exc:  # noqa: BLE001 - best-effort metadata, never fatal
        print(f"::warning::store_metadata failed for {store_path}: {exc}", flush=True)
        return {}


def materialize_local(
    repo_dir: str, primary_path: str, head_files: set[str]
) -> tuple[str, str | None, str | None]:
    """Local-mode materialisation (e.g. Hallu after `nemar dataset download`).

    The dataset working tree already holds the annex content (the data files are
    symlinks resolving to local annex objects), so biosigIO reads the
    working-tree paths directly and companions resolve beside the primary --
    no S3 download. Returns (primary_local, events_local|None, annex_key|None);
    the key is read from the symlink target for index provenance, best-effort.
    """
    primary_local = os.path.join(repo_dir, primary_path)
    events_rel = events_sibling_for(primary_path)
    events_local = os.path.join(repo_dir, events_rel) if events_rel in head_files else None
    primary_key: str | None = None
    try:
        if os.path.islink(primary_local):
            primary_key = parse_annex_key(os.readlink(primary_local))
    except OSError:
        primary_key = None
    return primary_local, events_local, primary_key


def embed_attr(meta_path: str, key: str, value: object) -> None:
    """Write a key into the `attributes` dict of an arbitrary Zarr v3 group zarr.json.

    Reads `meta_path`, sets `attributes[key] = value`, and writes back in place.
    Preserves all other fields. Use `embed_root_attr` for the store-root shorthand.
    """
    with open(meta_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc.setdefault("attributes", {})[key] = value
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def embed_root_attr(store_path: str, key: str, value: object) -> None:
    """Write a scalar into the Zarr v3 root group's attributes (its `zarr.json`)
    after biosigIO has written the store. Carries a display hint the converter knows
    from BIDS context but biosigIO does not (PowerLineFrequency), so the viewer reads
    it straight from the store with no extra fetch."""
    embed_attr(os.path.join(store_path, "zarr.json"), key, value)


def electrode_positions_for(
    repo_dir: str, primary_path: str, head_files: set[str], head: str
) -> dict | None:
    """BIDS electrode positions for a recording, resolved via the inheritance
    principle: among the `_electrodes.tsv` sidecars in the recording's directory
    or an ancestor whose entities are a subset of the recording's, the most
    specific one wins. A sibling `_coordsystem.json` is resolved the same way.

    The TSV is parsed by its header row to find the `name`/`x`/`y`/`z` columns
    (robust to extra columns like type/impedance and to column-order variation).
    Rows where any of x/y/z is missing, non-numeric, or "n/a" are skipped.

    Returns ``{"positions": {label: [x, y, z]}, "coordinate_system": str,
    "coordinate_units": str}`` or None when no `_electrodes.tsv` resolves or it
    contains no valid rows.
    """
    stem = filename_stem(primary_path)
    rec_dir = os.path.dirname(primary_path)
    rec_ents = _bids_entities(stem)

    def _resolve_sidecar(needle: str) -> str | None:
        """Return the most-specific applicable sidecar path, or None.

        `needle` is an entity-prefixed suffix like ``_electrodes.tsv``.
        A file matches when its basename ends with `needle` (e.g.
        ``sub-01_task-rest_electrodes.tsv``) or its basename is exactly
        the bare form without the leading underscore (e.g. ``electrodes.tsv``
        at the dataset root). Both forms carry empty entities, so the entity
        subset check still applies correctly.
        """
        bare = needle.lstrip("_")  # "electrodes.tsv" from "_electrodes.tsv"
        candidates: list[tuple[int, int, str]] = []
        for f in head_files:
            bname = os.path.basename(f)
            if not (f.endswith(needle) or bname == bare):
                continue
            cdir = os.path.dirname(f)
            if cdir and rec_dir != cdir and not rec_dir.startswith(cdir + "/"):
                continue
            cents = _bids_entities(filename_stem(f))
            if any(rec_ents.get(k) != v for k, v in cents.items()):
                continue
            depth = cdir.count("/") + (1 if cdir else 0)
            candidates.append((depth, len(cents), f))
        if not candidates:
            return None
        candidates.sort()
        # most specific = last after ascending sort
        return candidates[-1][2]

    elec_path = _resolve_sidecar("_electrodes.tsv")
    if elec_path is None:
        return None
    elec_text = _read_repo_text(repo_dir, head, elec_path)
    if not elec_text:
        return None

    # Parse the TSV by its header to locate name/x/y/z columns.
    lines = elec_text.splitlines()
    if not lines:
        return None
    header = [col.strip().lower() for col in lines[0].split("\t")]
    try:
        name_i = header.index("name")
        x_i = header.index("x")
        y_i = header.index("y")
        z_i = header.index("z")
    except ValueError:
        return None  # required columns absent

    positions: dict[str, list[float]] = {}
    for row_line in lines[1:]:
        if not row_line.strip():
            continue
        cols = row_line.split("\t")
        if len(cols) <= max(name_i, x_i, y_i, z_i):
            continue
        label = cols[name_i].strip()
        if not label:
            continue
        try:
            xv = cols[x_i].strip()
            yv = cols[y_i].strip()
            zv = cols[z_i].strip()
            if xv.lower() == "n/a" or yv.lower() == "n/a" or zv.lower() == "n/a":
                continue
            positions[label] = [float(xv), float(yv), float(zv)]
        except (ValueError, IndexError):
            continue

    if not positions:
        return None

    # Resolve the sibling coordsystem.json for coordinate metadata.
    coord_system = ""
    coord_units = ""
    cs_path = _resolve_sidecar("_coordsystem.json")
    if cs_path is not None:
        cs_text = _read_repo_text(repo_dir, head, cs_path)
        if cs_text:
            try:
                cs_data = json.loads(cs_text)
                if isinstance(cs_data, dict):
                    sys_val = cs_data.get("EEGCoordinateSystem") or cs_data.get(
                        "iEEGCoordinateSystem"
                    ) or cs_data.get("MEGCoordinateSystem") or ""
                    units_val = cs_data.get("EEGCoordinateUnits") or cs_data.get(
                        "iEEGCoordinateUnits"
                    ) or cs_data.get("MEGCoordinateUnits") or ""
                    coord_system = str(sys_val) if sys_val else ""
                    coord_units = str(units_val) if units_val else ""
            except ValueError:
                pass

    return {
        "positions": positions,
        "coordinate_system": coord_system,
        "coordinate_units": coord_units,
    }


def event_descriptions_for(
    repo_dir: str, primary_path: str, head_files: set[str], head: str
) -> dict[str, str]:
    """BIDS event-code descriptions for a recording, resolved via the inheritance
    principle: among the `_events.json` sidecars sitting in the recording's
    directory or an ancestor whose entities are a subset of the recording's, the
    most specific one wins (overrides less specific). Returns a flat mapping of
    event code -> description string (empty dict when none apply or no Levels are
    declared).

    Each applicable `_events.json` sidecar is parsed as a BIDS column-metadata
    object. For every top-level value that is a dict containing a ``"Levels"`` dict,
    its ``{str: str}`` entries are merged (most-specific sidecar wins). This supports
    multiple columns declaring Levels (e.g. ``value``, ``trial_type``).

    Sidecars are small JSON files tracked in git (not annexed); read via the working
    tree when present and ``git cat-file`` otherwise, matching the no-checkout
    workflow clone behaviour.
    """
    stem = filename_stem(primary_path)
    rec_dir = os.path.dirname(primary_path)
    rec_ents = _bids_entities(stem)
    needle = "_events.json"
    candidates: list[tuple[int, int, str]] = []
    for f in head_files:
        if not f.endswith(needle):
            continue
        cdir = os.path.dirname(f)
        # Applicable only if the sidecar is in the recording's dir or an ancestor.
        if cdir and rec_dir != cdir and not rec_dir.startswith(cdir + "/"):
            continue
        cents = _bids_entities(filename_stem(f))
        # ...and its entities must be a subset of the recording's.
        if any(rec_ents.get(k) != v for k, v in cents.items()):
            continue
        depth = cdir.count("/") + (1 if cdir else 0)
        candidates.append((depth, len(cents), f))
    candidates.sort()  # least specific first; the most specific value overrides
    result: dict[str, str] = {}
    for _, _, f in candidates:
        text = _read_repo_text(repo_dir, head, f)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        for col_meta in data.values():
            if not isinstance(col_meta, dict):
                continue
            levels = col_meta.get("Levels")
            if not isinstance(levels, dict):
                continue
            for code, desc in levels.items():
                if isinstance(code, str) and code and isinstance(desc, str) and desc:
                    result[code] = desc
    return result


def convert_recording(
    primary_local: str,
    events_local: str | None,
    store_path: str,
    power_line_frequency: float | None = None,
    value_descriptions: dict[str, str] | None = None,
    electrode_positions: dict | None = None,
) -> None:
    from biosigio import Recording, bids  # type: ignore[import-not-found]  # lazy: runtime-only dep

    rec = Recording.from_file(primary_local)
    if events_local and os.path.exists(events_local):
        bids.apply_events_tsv(rec, events_local)
    # Suffix-driven modality: group + resample the whole recording by its BIDS
    # datatype (an _eeg file -> eeg_250hz), regardless of what the importer guessed
    # per channel. Without this, EEGLAB's empty chanlocs type -> MISC -> misc_1024hz.
    modality = bids_suffix_modality(primary_local)
    if modality:
        for label in rec.channels:
            rec.channels[label]["modality"] = modality
    rec.to_zarr(store_path, dtype="int16", modality_rates=MODALITY_RATES)
    if power_line_frequency is not None:
        embed_root_attr(store_path, "power_line_frequency", power_line_frequency)
    if value_descriptions:
        events_meta = os.path.join(store_path, "events", "zarr.json")
        if os.path.exists(events_meta):
            embed_attr(events_meta, "value_descriptions", value_descriptions)
    if electrode_positions is not None:
        embed_root_attr(store_path, "electrode_positions", electrode_positions["positions"])
        embed_root_attr(store_path, "electrode_coordinate_system", electrode_positions["coordinate_system"])
        embed_root_attr(store_path, "electrode_coordinate_units", electrode_positions["coordinate_units"])


# --- Parallel conversion ------------------------------------------------------
# Recordings are independent (distinct S3 store prefixes), so they convert in a
# ProcessPoolExecutor: each worker streams its own annex blob, converts, validates,
# and `aws s3 sync`s its store, then returns the index entry. Conversion is
# CPU-bound (resample + zstd), so processes (not threads) give real parallelism.
# The shared context (repo, bucket, head, the head file set) is pickled once per
# worker via the initializer, not once per task.

_CTX: dict = {}


def _init_worker(ctx: dict) -> None:
    _CTX.clear()
    _CTX.update(ctx)


def convert_one(primary: str) -> dict:
    """Convert + upload one recording in a pool worker. Returns
    {"ok": True, "primary", "entry"} or {"ok": False, "primary", "error"}.
    Self-contained and picklable; reads shared inputs from the worker `_CTX`."""
    c = _CTX
    rel_store = store_rel_for(primary)
    work = os.path.join(c["tmp"], "work", primary.replace("/", "_"))
    store_local = os.path.join(c["tmp"], "stores", rel_store)
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.dirname(store_local), exist_ok=True)
    try:
        if c["local"]:
            primary_local, events_local, primary_key = materialize_local(
                c["repo"], primary, c["head_files"]
            )
        else:
            primary_local, events_local, primary_key = materialize_recording(
                c["repo"], c["bucket"], c["dataset_id"], primary, c["head_files"], c["head"], work
            )
        plf = power_line_frequency_for(c["repo"], primary, c["head_files"], c["head"])
        descs = event_descriptions_for(c["repo"], primary, c["head_files"], c["head"])
        elec = electrode_positions_for(c["repo"], primary, c["head_files"], c["head"])
        convert_recording(primary_local, events_local, store_local, plf, descs or None, elec)
        # Guard the --delete sync: an empty/partial store would otherwise wipe a
        # previously-valid one. zarr.json => v3 root.
        validate_store(store_local)
        meta = store_metadata(store_local)
        if not meta.get("groups"):
            raise RuntimeError(f"store has no channel groups: {store_local}")
        # Latest-only: --delete drops stale chunk objects a smaller new store no
        # longer needs. Long origin TTL; the callback purges zarr.json/index.json.
        subprocess.run(
            [
                "aws", "s3", "sync", store_local,
                safe_store_prefix(c["bucket"], c["dataset_id"], rel_store),
                "--delete", "--only-show-errors",
                "--cache-control", "public, max-age=86400",
            ],
            check=True,
        )
        entry = {
            "path": primary,
            "zarr": rel_store,
            "source_key": primary_key,
            "updated_utc": c["updated"],
            **meta,
        }
        return {"ok": True, "primary": primary, "entry": entry}
    except Exception as exc:  # noqa: BLE001 - isolate one bad recording
        return {"ok": False, "primary": primary, "error": str(exc)}
    finally:
        # Parallel workers share the NVMe scratch; reclaim each recording's copy
        # right after upload so N concurrent stores don't accumulate on disk.
        for d in (store_local, work):
            shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate NEMAR Zarr serving copies")
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--repo-dir", required=True, help="cloned dataset repo (full history)")
    ap.add_argument("--bucket", default="nemar")
    ap.add_argument("--region", default="us-east-2")
    ap.add_argument("--full", action="store_true", help="convert every recording")
    ap.add_argument(
        "--clean",
        action="store_true",
        help="wipe s3://<bucket>/<id>/zarr/ first, then full-rebuild the whole "
        "dataset. The serving copy must mirror the current dataset exactly, so a "
        "trigger remakes it wholesale rather than incrementally (no orphaned "
        "stores from removed/renamed recordings, no stale groups from a regroup, "
        "no merged index). Implies --full.",
    )
    ap.add_argument(
        "--local",
        action="store_true",
        help="read recordings from the local working tree (annex content present, "
        "e.g. on Hallu after `nemar dataset download`) instead of downloading the "
        "annex blobs from S3",
    )
    ap.add_argument("--callback-out", required=True, help="write the zarr-ready body here")
    ap.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="convert this many recordings in parallel (ProcessPoolExecutor). "
        "Default 1 (serial). The Hallu cron raises it; cap to keep N concurrent "
        "multi-GB recordings within local scratch + RAM.",
    )
    args = ap.parse_args()

    dataset_id = args.dataset_id
    bucket = args.bucket
    repo = args.repo_dir
    head = _run(["git", "-C", repo, "rev-parse", "HEAD"]).strip()
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --clean rebuilds from scratch: ignore any prior index (no merge, no diff) so
    # the run is unconditionally full and the index is rewritten fresh below.
    if args.clean:
        prior, prior_commit, full = None, None, True
    else:
        prior = s3_read_json(bucket, f"{dataset_id}/zarr/index.json")
        prior_commit = (prior or {}).get("source_commit")
        full = args.full or not prior_commit or not is_ancestor(repo, prior_commit, head)

    head_files = git_ls_files(repo, head)
    if full:
        diff: list[tuple[str, str]] = []
    else:
        assert prior_commit  # full is False only when prior_commit is a real ancestor SHA
        diff = git_diff_name_status(repo, prior_commit, head)
    convert, remove = compute_worklist(head_files, diff, full)

    # Wipe the whole serving prefix before rebuilding. Guarded on a non-empty
    # worklist so a transient "no recordings" read can never nuke a good copy;
    # the convert loop then re-uploads every store into the emptied prefix.
    if args.clean and convert:
        print(f"[zarr] --clean: wiping s3://{bucket}/{dataset_id}/zarr/ before full rebuild", flush=True)
        subprocess.run(
            ["aws", "s3", "rm", f"s3://{bucket}/{dataset_id}/zarr/",
             "--recursive", "--only-show-errors"],
            check=True,
        )
    print(
        f"[zarr] {dataset_id} head={head[:8]} prior={(prior_commit or 'none')[:8]} "
        f"full={full} convert={len(convert)} remove={len(remove)}",
        flush=True,
    )

    head_set = set(head_files)
    converted_entries: list[dict] = []
    failures: list[str] = []

    n = len(convert)

    def record(r: dict, i: int) -> None:
        # Log each recording as it finishes (live progress over a long backfill),
        # not all at once at the end.
        if r["ok"]:
            converted_entries.append(r["entry"])
            print(f"[zarr] [{i}/{n}] converted {r['primary']} -> {r['entry']['zarr']}", flush=True)
        else:
            failures.append(r["primary"])
            print(f"::warning::[{i}/{n}] conversion failed for {r['primary']}: {r['error']}", flush=True)

    jobs = max(1, args.jobs)
    with tempfile.TemporaryDirectory() as tmp:
        ctx = {
            "repo": repo, "bucket": bucket, "dataset_id": dataset_id, "head": head,
            "head_files": head_set, "local": args.local, "tmp": tmp, "updated": updated,
        }
        if jobs == 1 or n <= 1:
            _init_worker(ctx)
            for i, p in enumerate(convert, 1):
                record(convert_one(p), i)
        else:
            with ProcessPoolExecutor(
                max_workers=jobs, initializer=_init_worker, initargs=(ctx,)
            ) as ex:
                futs = {ex.submit(convert_one, p): p for p in convert}
                for i, fut in enumerate(as_completed(futs), 1):
                    try:
                        r = fut.result()
                    except Exception as exc:  # worker process died (OOM/segfault)
                        r = {"ok": False, "primary": futs[fut], "error": f"worker crashed: {exc}"}
                    record(r, i)

    for rel_store in remove:
        subprocess.run(
            ["aws", "s3", "rm", safe_store_prefix(bucket, dataset_id, rel_store),
             "--recursive", "--only-show-errors"],
            check=True,
        )
        print(f"[zarr] removed store {rel_store}", flush=True)

    # Hard fail: every attempted conversion errored and nothing was removed. Do
    # NOT advance the checkpoint or rewrite the index (that would strand the
    # failed recordings); return non-zero so the workflow's failure callback
    # flips zarr_status to 'failed' and the prior index is left intact.
    if convert and not converted_entries and not remove:
        print(f"::error::all {len(convert)} conversion(s) failed; index left untouched", flush=True)
        return 1

    # Advance source_commit to HEAD only on a fully clean run. With any failure,
    # keep the prior commit ("" when there is no prior -> next run goes full) so
    # the failed recordings are re-diffed and retried on the next run rather than
    # being skipped by an advanced checkpoint.
    index_commit = head if not failures else (prior_commit or "")
    index = merge_index(prior, dataset_id, index_commit, converted_entries, remove, updated)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(index, fh, separators=(",", ":"))
        index_local = fh.name
    aws_cp(
        index_local,
        f"s3://{bucket}/{dataset_id}/zarr/index.json",
        extra=["--content-type", "application/json", "--cache-control", "public, max-age=60"],
    )
    etag = _run(
        ["aws", "s3api", "head-object", "--bucket", bucket, "--key",
         f"{dataset_id}/zarr/index.json", "--query", "ETag", "--output", "text"]
    ).strip().strip('"')

    # status stays "ready": the stores that converted + the index are on S3, so
    # the latest-only state is real and worth recording even on a partial run.
    # `errors`/`failed` carry the per-recording skips; the workflow flags the run
    # red on errors>0 AFTER posting this, so the callback always fires.
    callback = {
        "dataset_id": dataset_id,
        "status": "ready",
        "store_count": index["store_count"],
        "index_etag": etag,
        "commit": head,
        "converted": [e["zarr"] for e in converted_entries],
        "removed": remove,
        "errors": len(failures),
        "failed": failures,
    }
    with open(args.callback_out, "w") as fh:
        json.dump(callback, fh)

    if failures:
        print(f"::error::{len(failures)} recording(s) failed to convert: {failures}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
