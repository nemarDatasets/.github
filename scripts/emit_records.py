#!/usr/bin/env python3
"""
NEMAR Tier-1 record emitter (neuroschema v0.3.0 `record` docs).

Sibling to scripts/emit_manifest.py. Where emit_manifest.py emits one
VersionManifest + summary for the whole dataset version, this script emits
a JSON ARRAY of neuroschema `record` documents -- one per primary signal
file -- to <out-dir>/records.json. Each element validates through the
neuroschema root envelope (doc_type 'record' -> core/record.schema.json).

This is the "Tier-1" emitter: it derives every field from git plumbing
alone -- no annex download, no biosigIO, no MNE, no pandas. Recording
duration / sample counts come from the BIDS sidecar (`RecordingDuration`,
`SamplingFrequency`) resolved via the inheritance principle. When the
sidecar does not declare `RecordingDuration`, `recording_duration` and
`ntimes` are emitted as null. The Tier-2 follow-up (a separate PR) will
backfill those nulls from the already-converted Zarr stores / biosigIO;
see the TIER-2 TODO hook in build_record().

Like emit_manifest.py it walks the git tree at the tag, but it only needs
each primary file's PATH, so it enumerates by name (`git ls-tree -r
--name-only`) -- which lists annexed files (locked symlinks, unlocked
pointer blobs) and plain git blobs alike, so no annex-pointer parsing is
needed here. Sidecars and channels.tsv are git-tracked text read via
`git cat-file blob <tag>:<path>`, which works in the workflow's
`--no-checkout` clone (no working tree).

The script does NOT clone; the workflow's checkout step is responsible
for that. The script does NOT touch the GitHub API. STDLIB ONLY. The
neuroschema validator (a third-party-jsonschema dependency) is a SEPARATE
workflow step, never imported here.

Usage:
    emit_records.py --dataset-id nm099999 --version 1.0.0 \\
        --repo-dir /tmp/repo --out-dir /tmp/out
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- primary file classification -----------------------------------------

# Primary recording containers a record doc is emitted for. EXTENDS
# generate_zarr.py PRIMARY_EXTS (.set,.edf,.bdf,.vhdr,.fif) with .snirf;
# every extension here MUST have its suffix in MODALITY below or the record
# is skipped (modality is required) -- .snirf's `nirs` suffix is mapped.
PRIMARY_EXTS = (".set", ".edf", ".bdf", ".vhdr", ".fif", ".snirf")

# Suffix -> record `modality` value. modality is REQUIRED and NOT inherited;
# `modality` is free-text in the schema (type string, no enum) but its
# documented examples are EEG/MEG/iEEG/EMG (record.schema.json:32), so we use
# those canonical spellings -- note "iEEG", NOT generate_zarr.py's biosigIO
# group name "IEEG". `nirs` -> "NIRS" so .snirf recordings are emitted, not
# silently dropped.
MODALITY = {"eeg": "EEG", "meg": "MEG", "ieeg": "iEEG", "emg": "EMG", "nirs": "NIRS"}

# BIDS filename entity short codes -> the neuroschema bidsEntities long keys.
# bidsEntities.schema.json is additionalProperties:false and allows ONLY
# these six keys; every other BIDS entity (ce, rec, dir, mod, echo, part,
# proc, split, desc, hemi, recording, ...) is DROPPED -- emitting it would
# fail validation.
ENTITY_KEY_MAP = {
    "sub": "subject",
    "ses": "session",
    "task": "task",
    "run": "run",
    "acq": "acquisition",
    "space": "space",
}

# Datatype directory segments that count as a BIDS datatype, used to fill
# the record `datatype` field from the path. Mirrors emit_manifest.py's
# BIDS_MODALITIES set.
BIDS_DATATYPES = (
    "eeg",
    "emg",
    "meg",
    "func",
    "anat",
    "dwi",
    "fmap",
    "beh",
    "ieeg",
    "nirs",
    "pet",
    "perf",
    "motion",
)


def run(cmd: list[str], cwd: str | None = None) -> str:
    """Run a subprocess and return stdout as text. Raises on non-zero exit."""
    return subprocess.check_output(cmd, cwd=cwd, text=True)


# --- path / BIDS helpers (mirror generate_zarr.py) -----------------------


def lower_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def is_primary(path: str) -> bool:
    return lower_ext(path) in PRIMARY_EXTS


def filename_stem(path: str) -> str:
    """`sub-01/eeg/sub-01_task-x_eeg.vhdr` -> `sub-01_task-x_eeg`."""
    return os.path.splitext(os.path.basename(path))[0]


def _bids_entities(stem: str) -> dict[str, str]:
    """Entity key->value pairs from a BIDS stem (`sub-01_task-x_run-2_eeg` ->
    {sub: 01, task: x, run: 2}); the trailing suffix token (no dash) is ignored.
    """
    ents: dict[str, str] = {}
    for tok in stem.split("_"):
        if "-" in tok:
            k, v = tok.split("-", 1)
            ents[k] = v
    return ents


def suffix_of(stem: str) -> str:
    """Trailing BIDS suffix token of a stem (no dash). `sub-01_task-x_eeg`
    -> `eeg`. A bare stem with no underscore returns itself."""
    return stem.rsplit("_", 1)[-1].lower() if "_" in stem else stem.lower()


def detect_datatype(path: str) -> str | None:
    """The BIDS datatype directory segment from the path, or None.

    Scans every path segment except the filename for a known datatype dir
    (mirrors emit_manifest.derive_modalities). For a raw recording the
    datatype is the immediate parent (`sub-01/eeg/...edf` -> 'eeg').
    """
    for seg in path.split("/")[:-1]:
        if seg in BIDS_DATATYPES:
            return seg
    return None


def _read_repo_text(repo_dir: str, head: str, path: str) -> str | None:
    """Read a git-tracked text file at `head`. Uses the working tree when
    present (local mode), else falls back to `git cat-file` -- the workflow
    clones `--no-checkout`, so there is no working tree there. None if
    unreadable. Reused verbatim from generate_zarr.py."""
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


# --- BIDS inheritance resolver -------------------------------------------


def resolve_chain(primary_path: str, head_files: set[str], needle: str) -> list[str]:
    """Resolve a BIDS sidecar inheritance chain for a recording.

    Generalises generate_zarr.py:power_line_frequency_for + electrode
    `_resolve_sidecar`: among files whose basename ends with `needle`
    (e.g. `_eeg.json`, `_channels.tsv`) OR whose basename is exactly the
    bare form without the leading underscore (e.g. a root-level
    `channels.tsv`), keep only those sitting in the recording's directory
    or a strict ancestor whose parsed entities are a SUBSET of the
    recording's. Return the applicable paths sorted least-specific-first
    (ascending (depth, entity-count)), so the LAST element is the most
    specific and overrides.
    """
    stem = filename_stem(primary_path)
    rec_dir = os.path.dirname(primary_path)
    rec_ents = _bids_entities(stem)
    bare = needle.lstrip("_")  # "channels.tsv" from "_channels.tsv"
    candidates: list[tuple[int, int, str]] = []
    for f in head_files:
        bname = os.path.basename(f)
        if not (f.endswith(needle) or bname == bare):
            continue
        cdir = os.path.dirname(f)
        # Applicable only if it sits in the recording's dir or an ancestor.
        if cdir and rec_dir != cdir and not rec_dir.startswith(cdir + "/"):
            continue
        cents = _bids_entities(filename_stem(f))
        # ...and its entities must be a subset of the recording's.
        if any(rec_ents.get(k) != v for k, v in cents.items()):
            continue
        depth = cdir.count("/") + (1 if cdir else 0)
        candidates.append((depth, len(cents), f))
    candidates.sort()  # least specific first
    return [f for _, _, f in candidates]


def merged_sidecar(
    repo_dir: str, head: str, primary_path: str, head_files: set[str], needle: str
) -> dict:
    """Resolve the `_<suffix>.json` sidecar chain by inheritance and deep-merge
    least->most specific into one flat dict (most specific overrides). Returns
    {} when no applicable sidecar resolves. BIDS sidecars are flat JSON, so a
    shallow dict.update per candidate is the correct override semantics."""
    merged: dict = {}
    for f in resolve_chain(primary_path, head_files, needle):
        text = _read_repo_text(repo_dir, head, f)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict):
            merged.update(data)  # later (more specific) overrides
    return merged


def parse_channels_tsv(text: str) -> tuple[int | None, dict[str, int] | None]:
    """Parse a BIDS channels.tsv -> (nchans, channel_type_counts).

    nchans = number of non-empty data rows (authoritative over any sidecar
    *ChannelCount). channel_type_counts tallies the (uppercased) 'type'
    column over data rows, SKIPPING missing / '' / 'n/a' cells while still
    counting those rows toward nchans. Returns channel_type_counts=None when
    the 'type' column is absent or no row carried a usable type.
    """
    lines = text.splitlines()
    if not lines:
        return None, None
    header = [c.strip().lower() for c in lines[0].split("\t")]
    type_i = header.index("type") if "type" in header else None
    nchans = 0
    counts: dict[str, int] = {}
    for row in lines[1:]:
        if not row.strip():
            continue
        nchans += 1
        if type_i is None:
            continue
        cols = row.split("\t")
        if len(cols) <= type_i:
            continue
        val = cols[type_i].strip()
        if not val or val.lower() == "n/a":
            continue
        key = val.upper()
        counts[key] = counts.get(key, 0) + 1
    channel_type_counts = counts if (type_i is not None and counts) else None
    # A header-only channels.tsv (zero data rows) carries no channel info ->
    # return None for nchans so build_record's *ChannelCount sidecar fallback
    # can fire, instead of asserting an authoritative-but-wrong nchans=0.
    return (nchans if nchans > 0 else None), channel_type_counts


# --- record assembly -----------------------------------------------------


def build_record(
    *,
    dataset_id: str,
    relpath: str,
    repo_dir: str,
    head: str,
    head_files: set[str],
    digested_at: str,
) -> dict | None:
    """Assemble one neuroschema v0.3.0 `record` doc for a primary file.

    Returns None (with a ::warning::) when the BIDS suffix is not a known
    modality, since `modality` is a REQUIRED field and we will not emit an
    invalid record.
    """
    stem = filename_stem(relpath)
    suffix = suffix_of(stem)
    modality = MODALITY.get(suffix)
    if modality is None:
        print(
            f"::warning::[emit_records] skipping {relpath}: unknown BIDS suffix "
            f"{suffix!r} has no modality mapping (modality is required)",
            file=sys.stderr,
            flush=True,
        )
        return None

    raw_ents = _bids_entities(stem)
    entities = {
        ENTITY_KEY_MAP[k]: v for k, v in raw_ents.items() if k in ENTITY_KEY_MAP
    }

    # Sidecar (`_<suffix>.json`) resolved + merged by BIDS inheritance.
    sidecar = merged_sidecar(repo_dir, head, relpath, head_files, f"_{suffix}.json")
    sf = sidecar.get("SamplingFrequency")
    if not (isinstance(sf, (int, float)) and not isinstance(sf, bool)):
        sf = None
    dur = sidecar.get("RecordingDuration")
    if not (isinstance(dur, (int, float)) and not isinstance(dur, bool)):
        # TIER-2 TODO: when the sidecar does not declare RecordingDuration,
        # backfill it (and ntimes) from the already-converted Zarr store /
        # biosigIO in the Tier-2 follow-up PR. Tier-1 emits null. Do NOT add
        # a `duration_source` field -- record/provenance are
        # additionalProperties:false and reject any extra key.
        dur = None

    # channels.tsv (authoritative for nchans + channel_type_counts), resolved
    # by the SAME inheritance chain (most-specific-wins).
    nchans: int | None = None
    channel_type_counts: dict[str, int] | None = None
    chain = resolve_chain(relpath, head_files, "_channels.tsv")
    ch_text: str | None = None
    if chain:
        ch_text = _read_repo_text(repo_dir, head, chain[-1])  # most specific
    if ch_text:
        nchans, channel_type_counts = parse_channels_tsv(ch_text)

    # Fall back to the sidecar *ChannelCount fields when channels.tsv is
    # absent (or yielded no rows).
    if nchans is None:
        cc = sum(
            v
            for k, v in sidecar.items()
            if k.endswith("ChannelCount")
            and isinstance(v, (int, float))
            and not isinstance(v, bool)
        )
        nchans = int(cc) if cc else None

    # ntimes = round(SamplingFrequency * RecordingDuration) when BOTH present,
    # else null (the recording_duration-null branch propagates here).
    ntimes = round(sf * dur) if (sf is not None and dur is not None) else None

    record: dict = {
        "schema_version": "0.3.0",
        "doc_type": "record",
        "dataset": dataset_id,
        "bids_relpath": relpath,
        "modality": modality,
        "datatype": detect_datatype(relpath),
        "suffix": suffix,
        "file_extension": os.path.splitext(relpath)[1],
        "entities": entities,
        "signal_summary": {
            "nchans": nchans,
            "ntimes": ntimes,
            "recording_duration": dur,
            "channel_type_counts": channel_type_counts,
        },
        "provenance": {"digested_at": digested_at},
    }
    # signal_properties is optional; only emit it when we actually have a
    # SamplingFrequency. sampling_frequency lives ONLY here (signal_summary
    # rejects it).
    if sf is not None:
        record["signal_properties"] = {"sampling_frequency": sf}

    return record


def build_records(*, dataset_id: str, version: str, repo_dir: str) -> list[dict]:
    """Walk the git tree at v<version> and emit one record doc per primary file."""
    bare_version = version.lstrip("v")
    tag = f"v{bare_version}"

    # The full name-list at the tag, used both as the primary enumeration and
    # as the inheritance resolver's `head_files` corpus (sidecars/channels.tsv).
    names = run(["git", "-C", repo_dir, "ls-tree", "-r", "--name-only", tag])
    head_files: set[str] = set()
    for line in names.splitlines():
        path = line.strip()
        if not path:
            continue
        # emit_manifest.py's isInternal() policy, plus skip derived/source
        # trees so only raw recordings become records.
        if path.startswith(".git/") or path.startswith(".github/"):
            continue
        if (
            path.startswith("derivatives/")
            or "/derivatives/" in path
            or path.startswith("sourcedata/")
            or "/sourcedata/" in path
        ):
            continue
        head_files.add(path)

    primaries = sorted(p for p in head_files if is_primary(p))

    # Match JS Date#toISOString(): millisecond precision, trailing Z.
    now = datetime.now(timezone.utc)
    digested_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    records: list[dict] = []
    for relpath in primaries:
        rec = build_record(
            dataset_id=dataset_id,
            relpath=relpath,
            repo_dir=repo_dir,
            head=tag,
            head_files=head_files,
            digested_at=digested_at,
        )
        if rec is not None:
            records.append(rec)
    return records


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Emit a JSON array of neuroschema record docs from a cloned dataset repo.",
    )
    p.add_argument("--dataset-id", required=True)
    p.add_argument("--version", required=True, help="X.Y.Z or vX.Y.Z; tag is v<X.Y.Z>")
    p.add_argument("--repo-dir", required=True, help="path to already-cloned dataset repo")
    p.add_argument("--out-dir", required=True, help="directory to write records.json")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    repo_dir = Path(args.repo_dir)
    if not (repo_dir / ".git").exists() and not (repo_dir / "HEAD").exists():
        # Tolerate both worktrees and bare clones. .git is the common case.
        print(f"[emit_records] error: --repo-dir {repo_dir} is not a git repo", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    records = build_records(
        dataset_id=args.dataset_id,
        version=args.version,
        repo_dir=str(repo_dir),
    )

    records_path = out_dir / "records.json"
    records_path.write_text(json.dumps(records, indent=2))

    bare_version = args.version.lstrip("v")
    with_duration = sum(
        1 for r in records if r["signal_summary"]["recording_duration"] is not None
    )
    print(
        f"[emit_records] dataset={args.dataset_id} version={bare_version} "
        f"records={len(records)} (with_recording_duration={with_duration}, "
        f"null_duration={len(records) - with_duration})",
        flush=True,
    )
    print(f"[emit_records] wrote {records_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
