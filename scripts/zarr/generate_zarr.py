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
* BIDS raw only: ``derivatives/``, ``sourcedata/``, and ``code/`` never hold a BIDS
  raw recording, so ``is_excluded_from_discovery`` excludes them (and BIDS-reserved
  MEG calibration filenames) from every discovery path -- ``is_primary``, the
  directory-recording derivation, and the diff-based companion/events routing. This
  scope matches nemarOrg/nemar-cli ADR 0027 (the backend dispatch-gate side of the
  same decision). Excluding a tree from *future* conversion must not be read as
  "gone from HEAD": ``compute_clean_orphans`` explicitly protects already-published
  stores under an excluded tree from ``--clean``'s orphan-removal so this scope
  change alone never deletes a store; that cleanup is separate, explicitly-authorized
  follow-up work (nemarOrg/nemar-cli#1095 / nemarOrg/nemar-cli#1097).
* Directory-keyed recordings: CTF ``.ds``, MEF3 ``.mefd``, and 4D/BTi are each a
  DIRECTORY of files git tracks individually, never the directory itself.
  ``.ds``/``.mefd`` are derived from an extension on a path component
  (``dir_recording_of`` / ``is_dir_recording`` / ``dir_recordings``); 4D/BTi carries
  no extension at all (BIDS names it a bare ``..._meg/`` directory), so it is
  detected by CONTENT instead -- a ``c,rf*`` processed-data file alongside a sibling
  ``config`` file (``bti_recordings``), the same gate biosigIO's importer uses, so
  the two sides agree on what counts as a recording. Requires biosigio>=1.2.3 (the
  release that added ``.mefd``/4D-BTi import); see ``requirements.txt``.
* The pure helpers (path classification, worklist, index merge) carry the logic
  and are unit-tested in ``test_generate_zarr.py``; the I/O lives in ``main``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from datetime import datetime, timezone

# --- Path classification -------------------------------------------------

# Primary recording containers biosigIO reads directly. A change to one of
# these (or its companion / events sidecar) rebuilds exactly one `.zarr` store.
# KIT/Yokogawa MEG is a single `.con`/`.sqd`/`.kdf` file (its `.mrk`/`.elp`/`.hsp`
# coregistration sidecars are not needed for the signal serving copy).
PRIMARY_EXTS = (".set", ".edf", ".bdf", ".vhdr", ".fif", ".con", ".sqd", ".kdf")
# Companions that share a recording's filename stem and carry its samples or
# markers; a change confined to one still rebuilds the recording's store.
COMPANION_EXTS = (".fdt", ".eeg", ".vmrk")
# CTF MEG is a `.ds` DIRECTORY (`.meg4` data + `.res4`/`.hc`/... headers), and MEF3
# iEEG is a `.mefd` DIRECTORY (`<CHANNEL>.timd/<CHANNEL>-000000.segd/` holding
# `.tdat`/`.tidx`/`.tmet` per channel segment -- a real session can hold well over a
# hundred `.timd` channel dirs, see on006392's 194). Neither is a single file, so
# neither ever appears in `git ls-tree` as one path -- each is derived from the files
# under it and treated as one recording keyed at the directory itself. Both are
# EXTENSION-keyed (the directory's own name ends in `.ds`/`.mefd`), which is what
# lets `dir_recording_of` derive the recording from any member path without
# consulting `head_files`; contrast 4D/BTi below, which is directory-based too but
# carries no extension and needs content-based detection instead.
CTF_DS_EXT = ".ds"
MEFD_EXT = ".mefd"
DIR_RECORDING_EXTS = (CTF_DS_EXT, MEFD_EXT)

# 4D Neuroimaging/BTi MEG: BIDS gives the recording directory NO extension at all
# (`sub-<label>[_ses-<label>]_task-<label>[_run-<index>]_meg/`), so unlike
# `.ds`/`.mefd` it cannot be keyed by a path-component extension -- detection is by
# CONTENT instead (`bti_recordings`, `bti_dir_of`): a directory qualifies only when
# it directly (non-nested) contains a `c,rf*`-prefixed processed-data file AND a
# sibling `config` file. Requiring `c,rf*` is the point: `config` alone is common to
# almost every datalad-tracked dataset (`.datalad/config`) and would false-positive
# on nearly every repo if used by itself. This mirrors biosigIO's own
# `importers.meg._find_bti_pdf` gate exactly, so the converter and biosigIO agree on
# what counts as a BTi recording.
_BTI_PDF_PREFIX = "c,rf"
_BTI_CONFIG_NAME = "config"

# Trees that can never hold a BIDS raw recording, so a file under one is never
# a servable recording no matter its extension. Mirrors `emit_records.py`'s
# `derivatives`/`sourcedata` exclusion shape (this repo), extended to also
# cover `code/` -- see `is_excluded_from_discovery` below and
# nemarOrg/nemar-cli ADR 0027 for the matching backend dispatch-gate scope.
EXCLUDED_TREES = ("derivatives", "sourcedata", "code")

# BIDS-reserved Elekta/Neuromag MEG calibration filenames: fine-calibration
# and crosstalk-correction data, never a recording. `_acq-crosstalk_meg.fif`
# matches PRIMARY_EXTS by extension alone and, read as a recording, raises a
# correctly-failing `ValueError: Could not find measurement data` (confirmed
# in on006012, on006720) -- the right verdict on the wrong question.
_BIDS_CALIBRATION_SUFFIXES = ("_acq-crosstalk_meg.fif", "_acq-calibration_meg.dat")

INDEX_FORMAT = "nemar-zarr-index"
INDEX_FORMAT_VERSION = 1

# Per-modality canonical rate caps (Hz) passed to to_zarr. Keys are biosigIO's
# uppercase modality names; the defaults already match, set explicitly so the
# NEMAR caps are visible/auditable here rather than implied by the library.
MODALITY_RATES = {"EEG": 250, "MEG": 250, "IEEG": 1000, "EMG": 1000}

# Large recordings are converted with biosigIO's STREAMING path (bounded RAM)
# instead of the in-memory `Recording.from_file -> to_zarr`, which loads the whole
# recording at float64 2-3x and OOMs on multi-GB iEEG/MEG (e.g. nm000253's 18 GB
# BrainVision recordings). Gated on (a) size and (b) an MNE-native format, so the
# streamed read matches the in-memory reader for that format exactly (BrainVision/
# FIF both go through MNE either way); EDF/EEGLAB stay on the in-memory path.
# Requires biosigio>=1.1.5. Threshold is env-overridable for the Hallu cron.
# CTF `.ds` is MNE-native too (large MEG), so it streams as well. So is MEF3
# `.mefd`: `mne.io.read_raw_mef` supports `preload=False`, and biosigio's streaming
# exporter opens it lazily the same way as CTF/FIF (`_MneSource`, biosigio>=1.2.3)
# -- worth it, since a MEF3 iEEG session can be multi-gigabyte. 4D/BTi streams too
# (`mne.io.read_raw_bti(preload=False)`, same `_MneSource` lazy path) but is NOT
# extension-keyed, so it cannot join this tuple; `should_stream` below gives it its
# own extension-less branch at this SAME multi-GB threshold, deliberately not the
# lower KIT one (see the KIT comment below for why KIT differs).
STREAM_MIN_BYTES = int(os.environ.get("ZARR_STREAM_MIN_BYTES", str(2 * 1024**3)))
STREAM_EXTS = (".vhdr", ".fif", ".ds", MEFD_EXT)
# KIT/Yokogawa .con/.sqd/.kdf load FULLY in memory (read_raw_kit has no lazy
# path), and a many-channel MEG file expands to ~5x its bytes as float64 + the
# biosigIO DataFrame + the resample copy -- so even a ~600 MB .con OOM-kills a
# pool worker at JOBS-way concurrency (which then breaks the whole pool). Route
# them through the streaming converter above a much lower threshold than the
# multi-GB one: streaming peaks ~3 GB regardless of file size, and small KIT
# files stay on the faster in-memory path. 4D/BTi does NOT belong in this group
# even though it is also MEG and also directory-based: `read_raw_bti` DOES support
# `preload=False` (confirmed via biosigio's streaming exporter, which opens it lazily
# exactly like CTF/FIF/`.mefd`), so it has none of KIT's "no lazy path" problem and
# stays on the multi-GB `STREAM_EXTS`-equivalent threshold instead (see
# `should_stream`).
STREAM_KIT_EXTS = (".con", ".sqd", ".kdf")
STREAM_KIT_MIN_BYTES = int(os.environ.get("ZARR_STREAM_KIT_MIN_BYTES", str(256 * 1024**2)))

# EDF/BDF have no lazy MNE reader parity: the in-memory path reads them via
# pyedflib and MNE rescales EDF to SI volts, so streaming EDF via MNE would NOT
# match a re-run on the in-memory path. biosigIO >= 1.2.0 streams EDF/BDF via
# pyedflib (importer parity), so ONLY then may we route them to the streamer;
# on an older lib EDF stays in-memory (and is #909-skipped when too large).
# Threshold is low (like KIT) because in-memory EDF blows up ~6x and OOMs early.
STREAM_EDF_EXTS = (".edf", ".bdf")
STREAM_EDF_MIN_BYTES = int(os.environ.get("ZARR_STREAM_EDF_MIN_BYTES", str(256 * 1024**2)))


def _biosigio_streams_edf() -> bool:
    """True if the installed biosigIO streams EDF/BDF via pyedflib (>= 1.2.0,
    nemar-cli#944). Older builds read EDF via MNE in stream_to_zarr, which
    disagrees with the in-memory pyedflib units, so EDF must stay in-memory."""
    try:
        from importlib.metadata import version

        major_minor = tuple(int(p) for p in version("biosigio").split(".")[:2])
        return major_minor >= (1, 2)
    except Exception:  # noqa: BLE001 - absent/odd version string: assume no EDF streaming
        return False


# Resolved once at import; a recording-format decision must not re-probe per call.
_EDF_STREAMABLE = _biosigio_streams_edf()


def should_stream(primary_local: str, size_bytes: int) -> bool:
    """Whether a recording converts via the bounded-memory streaming path.

    Large MNE-native recordings (multi-GB iEEG/MEG BrainVision/FIF, CTF `.ds`,
    MEF3 `.mefd`) stream above ``STREAM_MIN_BYTES``; KIT `.con`/`.sqd`/`.kdf` and
    -- when biosigIO >= 1.2.0 -- EDF/BDF stream above the much lower KIT/EDF
    thresholds because their in-memory float64 blow-up OOMs a worker well below
    the multi-GB mark. Everything else (and small KIT/EDF) uses the faster
    in-memory path.

    Called both pre-materialization (``primary_local`` is still the git-relative
    path, e.g. the RAM-admission estimate in ``main``) and post-materialization
    (a real local path, from ``convert_recording``), so every branch here must
    decide from the path STRING alone -- never ``os.path.isdir`` or another
    filesystem check, which would silently misclassify the pre-materialization
    call (nothing exists at that path yet).
    """
    ext = lower_ext(primary_local)
    if ext in STREAM_KIT_EXTS:
        return size_bytes > STREAM_KIT_MIN_BYTES
    if _EDF_STREAMABLE and ext in STREAM_EDF_EXTS:
        return size_bytes > STREAM_EDF_MIN_BYTES
    if ext in STREAM_EXTS:
        return size_bytes > STREAM_MIN_BYTES
    # 4D/BTi: BIDS gives it no extension at all (see `bti_recordings`), so it
    # can't join STREAM_EXTS by extension the way `.ds`/`.mefd` do. Every other
    # primary this converter discovers carries a real extension (PRIMARY_EXTS, or
    # `.ds`/`.mefd` via DIR_RECORDING_EXTS), so an empty extension reaching here is
    # a BTi recording by construction, not an unrelated ext-less path. It streams
    # above the SAME multi-GB threshold as STREAM_EXTS -- deliberately not the
    # lower KIT one -- because `read_raw_bti` genuinely supports `preload=False`
    # (biosigIO's streaming exporter opens it lazily via the same `_MneSource`
    # path as CTF/FIF/`.mefd`); it doesn't have KIT's "no lazy reader" problem.
    if ext == "":
        return size_bytes > STREAM_MIN_BYTES
    return False


# --- Per-recording memory guard (#909) ----------------------------------------
# The in-memory path (`Recording.from_file`) loads a recording at float64 (~4x
# its int16 on-disk bytes) plus a resample copy, so a large EDF/BDF/EEGLAB
# recording -- which has no streaming reader -- OOM-kills its pool worker (and,
# via BrokenProcessPool, its concurrently-running siblings), then reruns as an
# infra failure and burns retries. The streaming path peaks ~STREAM_PEAK_BYTES
# regardless of size. We PROJECT each recording's peak RAM and skip (cleanly,
# with a deterministic reason surfaced in the index) anything that won't fit the
# node's usable RAM -- BEFORE the load, so no OOM ever happens. The ceiling is the
# whole usable node (not usable_RAM / jobs): main() admits recordings so the SUM
# of in-flight peaks stays within it, so raising --jobs adds concurrency without
# shrinking the budget or skipping more recordings.
STREAM_PEAK_BYTES = int(os.environ.get("ZARR_STREAM_PEAK_BYTES", str(4 * 1024**3)))
# float64 blow-up + resample copy for the in-memory path (int16 -> float64 = 4x).
INMEM_MEM_FACTOR = float(os.environ.get("ZARR_INMEM_MEM_FACTOR", "6"))


class RecordingTooLarge(Exception):
    """A recording whose projected peak RAM exceeds this run's per-recording
    budget. Carries `.code` so convert_one surfaces it as a DETERMINISTIC skip
    (recorded in the index, no infra retry) -- exactly like a biosigIO data
    failure -- instead of OOM-crashing the worker. #909"""

    code = "recording_too_large"


class ChannelCountMismatch(Exception):
    """The converted store carries fewer channels than the recording's BIDS
    `_channels.tsv` declares, so publishing it would serve a silently
    unfaithful copy (the failure mode behind nemarDatasets/on002718#1, where
    biosigio#110 truncated 74-channel EEGLAB files to one channel). Typed so
    the gate is a DETERMINISTIC data failure surfaced in the index and the
    unfaithful store is never uploaded.

    Policy: better NO store than a wrong store. On the incremental path the
    prior store survives (the gate runs before this recording's sync). Under
    ``--clean`` (the Hallu bulk path) the dataset prefix is wiped up front, so
    a gated recording ends with no serving copy at all -- intended, since a
    copy that contradicts channels.tsv must not be served, and the prior copy
    was built by the same converter lineage that just failed the check. Being
    deterministic, a gated recording does NOT self-retry: after a converter
    fix, re-run the dataset explicitly (hallu-zarr.sh --dataset <id>)."""

    code = "channel_count_mismatch"


def projected_peak_bytes(primary_local: str, size_bytes: int) -> int:
    """Estimated peak RAM to convert this recording: bounded for the streaming
    path, ~float64 blow-up for the in-memory path. Drives the skip guard (#909)."""
    if should_stream(primary_local, size_bytes):
        return STREAM_PEAK_BYTES
    return int(size_bytes * INMEM_MEM_FACTOR)


def usable_ram_bytes() -> int:
    """Convertible RAM: MemTotal (Linux /proc/meminfo) minus a headroom fraction.
    A conservative fallback keeps the guard active off-Linux / in tests."""
    frac = float(os.environ.get("ZARR_MEM_HEADROOM_FRAC", "0.8"))
    total: int | None = None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024  # kB -> bytes
                    break
    except OSError:
        total = None
    if total is None:
        total = int(os.environ.get("ZARR_NODE_RAM_BYTES", str(32 * 1024**3)))
    return int(total * frac)


def per_recording_ceiling_bytes() -> int:
    """Largest projected peak a SINGLE recording may use: the whole usable node.
    Admission control in ``main`` keeps the SUM of concurrently-converting
    recordings within this, so a recording is #909-skipped only when it can't fit
    the node even alone -- independent of ``--jobs`` (unlike the old RAM/jobs
    split, where raising jobs shrank the budget and skipped more recordings).
    Explicit override still wins. #909"""
    override = os.environ.get("ZARR_REC_MEM_BUDGET_BYTES")
    if override:
        return int(override)
    return usable_ram_bytes()


# Fallback user-facing reasons, keyed by biosigIO error code. The authoritative
# copy lives in biosigio.exceptions.REASONS (single source of truth); we prefer
# that at runtime and use this only if the import is unavailable. Keep the codes
# in sync with biosigio's hierarchy.
_FALLBACK_REASONS = {
    "not_continuous": (
        "This file is a trial-averaged or epoched derivative, not a continuous "
        "recording, so the time-series viewer is not available."
    ),
    "corrupt_or_truncated": (
        "This recording's data file appears truncated or corrupt, so the viewer "
        "could not be generated."
    ),
    "unsupported_format": "This file format is not yet supported by the viewer.",
    "empty_recording": "This recording contains no signal channels to display.",
    "file_read_error": "This recording could not be prepared for viewing.",
    # NEMAR-side (not a biosigIO code): a recording too large to convert on the
    # conversion node within memory limits (#909). The streaming path handles
    # multi-GB BrainVision/FIF/CTF; this is hit by a very large EDF/BDF/EEGLAB
    # recording, which has no streaming reader yet and would load fully in memory.
    "recording_too_large": (
        "This recording is too large to convert to an interactive viewer copy "
        "within the conversion node's memory limits."
    ),
    # NEMAR-side fidelity gate: the converted copy disagreed with the BIDS
    # channels.tsv ground truth, so it was withheld rather than served.
    "channel_count_mismatch": (
        "The converted viewer copy carried fewer channels than this recording's "
        "channels.tsv declares, so it was withheld pending a converter fix."
    ),
}
_GENERIC_REASON = _FALLBACK_REASONS["file_read_error"]


def reason_for_code(code: str | None) -> str:
    """User-facing reason for a biosigIO failure code, preferring biosigIO's own
    REASONS (single source of truth) and falling back to a local copy."""
    if not code:
        return _GENERIC_REASON
    try:
        from biosigio.exceptions import REASONS  # type: ignore[import-not-found]

        if code in REASONS:
            return REASONS[code]
    except Exception:  # noqa: BLE001 - biosigio absent/old: use the local copy
        pass
    return _FALLBACK_REASONS.get(code, _GENERIC_REASON)


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


def in_excluded_tree(path: str) -> bool:
    """True if `path` sits under `derivatives/`, `sourcedata/`, or `code/`,
    matched at the top level or nested, on a full path SEGMENT rather than a
    bare substring -- `mycode/`, `derivatives_old/`, and a task label
    containing "code" must still be discoverable. Same shape as
    `emit_records.py`'s `derivatives`/`sourcedata` exclusion in this repo."""
    return any(
        path.startswith(f"{tree}/") or f"/{tree}/" in path for tree in EXCLUDED_TREES
    )


def is_bids_calibration_file(path: str) -> bool:
    """True for a BIDS-reserved MEG calibration filename (crosstalk or
    fine-calibration correction data), which is never a recording."""
    return path.endswith(_BIDS_CALIBRATION_SUFFIXES)


def is_excluded_from_discovery(path: str) -> bool:
    """True if `path` can never be a servable BIDS raw recording: it sits
    under an excluded tree, or it is a reserved BIDS calibration filename.

    The single predicate every recording-discovery path runs a candidate
    through before treating it as buildable -- `is_primary`, the directory-
    recording derivations (`dir_recordings` for CTF `.ds`/MEF3 `.mefd`,
    `bti_recordings` for 4D/BTi), the diff-based companion/`_events.tsv`
    routing in `compute_worklist`, the stale-failure carry-forward in
    `merge_index`, and the `--clean` orphan-safety filter in
    `compute_clean_orphans`. One predicate used everywhere instead of
    repeating the tree/filename checks inline keeps the raw-only scope
    consistent across every entry point.
    """
    return in_excluded_tree(path) or is_bids_calibration_file(path)


def is_primary(path: str) -> bool:
    return lower_ext(path) in PRIMARY_EXTS and not is_excluded_from_discovery(path)


def is_events_tsv(path: str) -> bool:
    return path.endswith("_events.tsv")


def filename_stem(path: str) -> str:
    """`sub-01/eeg/sub-01_task-x_eeg.vhdr` -> `sub-01_task-x_eeg`."""
    return os.path.splitext(os.path.basename(path))[0]


def entities_base(stem: str) -> str:
    """Drop the trailing BIDS suffix: `sub-01_task-x_eeg` -> `sub-01_task-x`."""
    return stem.rsplit("_", 1)[0] if "_" in stem else stem


# --- BIDS split recordings (multi-file FIF) ------------------------------
#
# MNE writes a recording larger than the FIF 2 GB limit as a chain of files
# `..._split-01_<suffix>.fif`, `..._split-02_<suffix>.fif`, ...; the first file
# holds the header and a pointer to the next, so `read_raw_fif(split-01)` follows
# the chain and returns the WHOLE recording. The other splits are not standalone
# recordings -- reading one in isolation yields only its segment. So a split group
# is ONE logical recording: the lowest-index split is the chain head (the only
# buildable primary), every split must be materialised together for MNE to follow
# the chain, and exactly one store is written (keyed at the head split's path).
_SPLIT_RE = re.compile(r"_split-(\d+)")


def split_index(path: str) -> int | None:
    """Numeric `split-NN` entity of a BIDS split file (`..._split-02_meg.fif` -> 2),
    or None when the path carries no `split-` entity."""
    m = _SPLIT_RE.search(os.path.basename(path))
    return int(m.group(1)) if m else None


def _strip_split(stem: str) -> str:
    """Remove the `_split-NN` entity token from a stem (no-op when absent)."""
    return _SPLIT_RE.sub("", stem, count=1)


def is_split_fif(path: str) -> bool:
    """True for a FIF recording carrying a `split-` entity (the only ext where the
    split chain matters; other formats are single-file)."""
    return lower_ext(path) == ".fif" and split_index(path) is not None


def split_group_key(path: str) -> str:
    """Identity of the logical recording a split file belongs to: its path with the
    `_split-NN` entity removed. `sub-03/meg/sub-03_task-x_split-02_meg.fif` ->
    `sub-03/meg/sub-03_task-x_meg.fif`. A non-split path returns unchanged."""
    d = os.path.dirname(path)
    base = _SPLIT_RE.sub("", os.path.basename(path), count=1)
    return f"{d}/{base}" if d else base


def split_heads_and_members(primaries: list[str]) -> tuple[set[str], dict[str, str]]:
    """Partition primaries into buildable heads + a non-head-split -> head map.

    `heads` is every primary that should build a store: non-split primaries
    verbatim, plus the lowest-index split of each FIF split group. `member_to_head`
    maps each NON-head split to its head, so a change to any split rebuilds the one
    head store. A degenerate group whose `split-01` is absent picks the lowest
    present split as head (best-effort; MNE then reads from there)."""
    groups: dict[str, list[str]] = {}
    heads: set[str] = set()
    for p in primaries:
        if is_split_fif(p):
            groups.setdefault(split_group_key(p), []).append(p)
        else:
            heads.add(p)
    member_to_head: dict[str, str] = {}
    for members in groups.values():
        ordered = sorted(members, key=lambda x: (split_index(x), x))
        head = ordered[0]
        heads.add(head)
        for m in ordered[1:]:
            member_to_head[m] = head
    return heads, member_to_head


def split_members_for(primary_path: str, head_files: set[str]) -> list[str]:
    """Every FIF split that shares `primary_path`'s split group, sorted by index
    (includes the head). `[]` when `primary_path` is not a split file. Used to (a)
    materialise the whole chain and (b) record the member list on the index entry so
    the browser can resolve any split file to the one store."""
    if not is_split_fif(primary_path):
        return []
    gkey = split_group_key(primary_path)
    members = [p for p in head_files if is_split_fif(p) and split_group_key(p) == gkey]
    return sorted(members, key=lambda x: (split_index(x), x))


def store_rel_for(primary_path: str) -> str:
    """`sub-01/eeg/sub-01_task-x_eeg.set` -> `sub-01/eeg/sub-01_task-x_eeg.zarr`.

    Strips the data extension and appends `.zarr`; the BIDS suffix (`_eeg`,
    `_emg`, ...) is preserved, so the rule is uniform across all primary exts and
    over a directory recording (CTF `..._meg.ds` -> `..._meg.zarr`, MEF3
    `..._ieeg.mefd` -> `..._ieeg.zarr`). A 4D/BTi directory carries no extension
    at all, so `os.path.splitext` finds none to strip and this is a plain
    `path + ".zarr"` for it (`..._meg` -> `..._meg.zarr`).
    """
    root, _ = os.path.splitext(primary_path)
    return root + ".zarr"


# --- Extension-keyed directory recordings (CTF `.ds`, MEF3 `.mefd`) -----------
#
# A CTF recording is a directory `..._meg.ds/` holding `.meg4` (data) + `.res4`/
# `.hc`/... headers. A MEF3 recording is a directory `..._ieeg.mefd/` holding one
# `<CHANNEL>.timd/<CHANNEL>-000000.segd/` per channel, each with `.tdat`/`.tidx`/
# `.tmet`. Both are directories git tracks by their inner files, never the
# directory itself, so each is derived from those files and treated as one
# primary keyed at the directory path; biosigIO/MNE reads the directory whole
# (`read_raw_ctf` / `read_raw_mef`). Generalized into one mechanism (rather than
# copy-pasting the CTF logic for MEF3) because the shape is identical: both are
# recognized by an EXTENSION on a path component, so the recording can be derived
# from any member path alone, without consulting `head_files`. Contrast 4D/BTi
# just below, which is directory-based too but has no extension and needs
# content-based detection instead.


def dir_recording_of(path: str) -> str | None:
    """The `.ds`/`.mefd` recording directory a path belongs to, or None.

    `sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.meg4` ->
    `sub-01/meg/sub-01_task-x_meg.ds`; likewise a `.mefd` member resolves to its
    `.mefd` directory regardless of nesting depth (a MEF3 recording nests a
    `<CHANNEL>.timd/<CHANNEL>-000000.segd/*.tdat` several levels below the
    `.mefd` itself). Returns a directory path itself unchanged. Only the FIRST
    matching component counts (neither `.ds` nor `.mefd` dirs are nested)."""
    parts = path.split("/")
    for i, comp in enumerate(parts):
        lower = comp.lower()
        if any(lower.endswith(ext) for ext in DIR_RECORDING_EXTS):
            return "/".join(parts[: i + 1])
    return None


def is_dir_recording(path: str) -> bool:
    """True if `path` is exactly a `.ds`/`.mefd` recording directory (not a file
    inside one)."""
    stripped = path.lower().rstrip("/")
    return any(stripped.endswith(ext) for ext in DIR_RECORDING_EXTS)


def is_mefd(path: str) -> bool:
    """True if `path` is exactly a MEF3 `.mefd` recording directory."""
    return path.lower().rstrip("/").endswith(MEFD_EXT)


def dir_recordings(head_files) -> set[str]:
    """Every CTF `.ds` / MEF3 `.mefd` recording directory present in `head_files`
    (derived from the inner files, since the directory itself is never a tracked
    path).

    Excludes a directory under an excluded tree (derivatives/sourcedata/code): a
    directory recording's identity is directory-derived rather than an extension
    match on the recording's OWN path, so it needs its own
    `is_excluded_from_discovery` check rather than inheriting `is_primary`'s.
    """
    dirs: set[str] = set()
    for f in head_files:
        d = dir_recording_of(f)
        if d is not None and not is_excluded_from_discovery(d):
            dirs.add(d)
    return dirs


# --- Content-keyed directory recordings (4D/BTi) -------------------------------
#
# 4D/BTi is directory-based like CTF/MEF3, but BIDS gives it NO extension at all,
# so it cannot be derived from a path component the way `dir_recording_of` does.
# Detection is instead by CONTENT: a directory qualifies only when it directly
# (non-nested) contains a `c,rf*`-prefixed processed-data file (conventionally
# `c,rfDC`; a hardware-filtered copy such as `c,rfDC,fn50,o` also matches) AND a
# sibling `config` file -- exactly biosigIO's own `importers.meg._find_bti_pdf`
# gate, so this converter and biosigIO agree on what counts as a BTi recording
# (the converter decides what's a recording; biosigIO decides which file it
# reads -- if they disagreed, the index would describe a different file than was
# actually converted). `config` is checked but never sufficient by itself:
# `.datalad/config` exists in virtually every datalad-tracked dataset, and using
# it alone as the detector would treat every repo as a BTi recording.


def is_bti_marker_name(name: str) -> bool:
    """True for a basename that participates in 4D/BTi directory detection: the
    processed-data file (`c,rf*`) or its required `config` sibling. `hs_file`
    (the optional head-shape sidecar) deliberately does NOT count -- its absence
    or removal must not affect whether a directory is a BTi recording."""
    return name == _BTI_CONFIG_NAME or name.startswith(_BTI_PDF_PREFIX)


def bti_recordings(head_files) -> set[str]:
    """Every 4D/BTi recording directory present in `head_files`, detected by
    content rather than extension (see the module note above): a directory
    qualifies when it directly contains both a `c,rf*` file and a sibling
    `config` file. Excludes a directory under an excluded tree
    (derivatives/sourcedata/code), like `dir_recordings`.
    """
    names_by_dir: dict[str, set[str]] = {}
    for f in head_files:
        names_by_dir.setdefault(os.path.dirname(f), set()).add(os.path.basename(f))
    dirs: set[str] = set()
    for d, names in names_by_dir.items():
        if not d or is_excluded_from_discovery(d):
            continue
        if _BTI_CONFIG_NAME in names and any(n.startswith(_BTI_PDF_PREFIX) for n in names):
            dirs.add(d)
    return dirs


def is_bti_dir(path: str) -> bool:
    """True for a path that is a 4D/BTi recording directory keyed the
    extension-less way (see `bti_recordings`).

    A bare extension check, not a content re-check: every OTHER primary this
    converter discovers carries a real extension (`PRIMARY_EXTS`, or `.ds`/
    `.mefd` via `DIR_RECORDING_EXTS`), so this is only ever called on a path
    already known to be a recording (from the worklist), where "no extension"
    unambiguously means "4D/BTi directory."
    """
    return lower_ext(path) == ""


def bti_pdf_choice(basenames) -> tuple[str | None, bool]:
    """Which processed-data file biosigIO's `_find_bti_pdf` will read among the
    `c,rf*`-prefixed candidates in `basenames`, and whether that choice is
    AMBIGUOUS (more than one candidate present, or falling back off the
    canonical name).

    Mirrors biosigIO 1.2.3's precedence exactly: an exact `c,rfDC` always wins;
    otherwise the first candidate in `sorted()` order (filesystem listing order
    is NOT used -- verified to matter in practice, see biosigIO's module note).
    Neither side of this converter picks a file to convert -- biosigIO alone
    reads the directory -- so this exists purely to let this converter's own
    discovery/materialization logging name the SAME file biosigIO is expected
    to read, keeping the two sides verifiably in agreement (see the module note
    above: if they disagreed, the index would describe a different file than
    was actually converted). Returns (None, False) when no `c,rf*` candidate is
    present at all.
    """
    candidates = sorted(n for n in basenames if n.startswith(_BTI_PDF_PREFIX))
    if not candidates:
        return None, False
    fell_back = "c,rfDC" not in candidates
    chosen = candidates[0] if fell_back else "c,rfDC"
    ambiguous = fell_back or len(candidates) > 1
    return chosen, ambiguous


def events_sibling_for(primary_path: str) -> str:
    """BIDS events sidecar path for a recording (suffix `_events`, ext `.tsv`).

    `sub-01/eeg/sub-01_task-x_eeg.set` -> `sub-01/eeg/sub-01_task-x_events.tsv`.

    The `split-NN` entity is dropped (a split FIF recording shares one events file
    without it): `sub-03/meg/sub-03_task-x_split-01_meg.fif` ->
    `sub-03/meg/sub-03_task-x_events.tsv`.
    """
    d = os.path.dirname(primary_path)
    base = _strip_split(entities_base(filename_stem(primary_path)))
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


def store_total_channels(meta: dict) -> int:
    """Total channels a built store serves, summed across its groups (a group
    with a missing/None n_channels counts 0). Compared against
    ``expected_channel_count_for`` by the fidelity gate in ``convert_one``."""
    return sum(int(g.get("n_channels") or 0) for g in meta.get("groups", []))


def expected_channel_count_for(
    repo_dir: str, primary_path: str, head_files: set[str], head: str
) -> int | None:
    """Channel count the recording's BIDS `_channels.tsv` declares, or None when
    no applicable channels.tsv exists (nothing to check against).

    Resolution mirrors ``power_line_frequency_for``: among `_channels.tsv` files
    in the recording's directory or an ancestor whose entities are a subset of
    the recording's, the most specific wins. channels.tsv is git-tracked text
    (never annexed), so this is a head-file-list scan plus one small read.

    This is the ground truth for the post-conversion fidelity gate: a store
    whose total channel count falls short of this number is withheld
    (``ChannelCountMismatch``) instead of served.

    Unlike PLF's per-field JSON inheritance, only the single most specific
    candidate is read (BIDS TSV inheritance is closest-file-wins, not a
    merge). If that read fails or the file has no data rows, this returns
    None and the gate is silently OFF for the recording -- fail-open by
    design: no ground truth, nothing to check against.
    """
    stem = filename_stem(primary_path)
    rec_dir = os.path.dirname(primary_path)
    rec_ents = _bids_entities(stem)
    candidates: list[tuple[int, int, str]] = []
    for f in head_files:
        if not f.endswith("_channels.tsv"):
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
    best = candidates[-1][2]  # most specific
    text = _read_repo_text(repo_dir, head, best)
    if text is None:
        return None
    rows = [line for line in text.splitlines()[1:] if line.strip()]
    return len(rows) or None


def affected_primaries(
    changed_path: str,
    primaries_by_dir: dict[str, list[str]],
    member_to_head: dict[str, str] | None = None,
) -> set[str]:
    """Buildable head primaries a changed path rebuilds, restricted to those at HEAD.

    A head primary maps to itself; a non-head split FIF maps to its group head (via
    `member_to_head`), so editing any split rebuilds the one store; a companion
    (`.fdt`/`.eeg`/`.vmrk`) maps to the same-stem primary in its directory; a
    `*_events.tsv` maps to every primary in its directory sharing the events
    entities-base (the `split-NN` entity is ignored on both sides, since a split
    recording's events file carries no split). `primaries_by_dir` holds only
    buildable heads, so a non-head split is not in `here`.
    """
    d = os.path.dirname(changed_path)
    here = primaries_by_dir.get(d, [])
    if is_primary(changed_path):
        if changed_path in here:
            return {changed_path}
        # A non-head split (not itself buildable) rebuilds its group head.
        head = (member_to_head or {}).get(changed_path)
        return {head} if head in here else set()
    ext = lower_ext(changed_path)
    if ext in COMPANION_EXTS:
        stem = filename_stem(changed_path)
        return {p for p in here if filename_stem(p) == stem}
    if is_events_tsv(changed_path):
        ev_stem = filename_stem(changed_path)  # `sub-01_task-x_events`
        ev_base = ev_stem[: -len("_events")] if ev_stem.endswith("_events") else entities_base(ev_stem)
        ev_base = _strip_split(ev_base)
        return {p for p in here if _strip_split(entities_base(filename_stem(p))) == ev_base}
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
    # Directory-keyed recordings are derived from the files under them, not
    # tracked paths of their own, so they are buildable primaries alongside the
    # file primaries. CTF `.ds`/MEF3 `.mefd` are extension-derived; 4D/BTi is
    # content-derived (see the two sections above).
    dirrec_dirs = dir_recordings(head_files)
    bti_dirs = bti_recordings(head_files)
    # Collapse FIF split groups to their chain head: only the head builds a store,
    # and a change to any split routes to that head (member_to_head).
    heads, member_to_head = split_heads_and_members(primaries)
    by_dir: dict[str, list[str]] = {}
    for p in heads:
        by_dir.setdefault(os.path.dirname(p), []).append(p)
    all_primaries = sorted([*heads, *dirrec_dirs, *bti_dirs])

    if full:
        return all_primaries, []

    convert: set[str] = set()
    remove: set[str] = set()
    # Deleted splits are resolved per split GROUP after the loop: a split file gone
    # from HEAD is no longer in `member_to_head` (which is built from HEAD), so it
    # can't route through it. Group by split_group_key and decide once per group.
    deleted_split_groups: dict[str, list[str]] = {}
    for status, path in diff_entries:
        # A changed/removed path under an excluded tree (derivatives/
        # sourcedata/code) or a BIDS calibration filename never builds or
        # removes a store: it was never a candidate recording, and a
        # deletion there must not be misread as "the recording is gone from
        # HEAD" -- that would delete an already-published store this change
        # is required to leave untouched (see `compute_clean_orphans`).
        if is_excluded_from_discovery(path):
            continue
        # A change anywhere inside a CTF `.ds`/MEF3 `.mefd` is a change to that
        # one recording (extension-derived; see `dir_recording_of`).
        ds = dir_recording_of(path)
        if ds is not None:
            if ds in dirrec_dirs:  # at least one file remains -> rebuild the recording
                convert.add(ds)
            elif status == "D":  # the whole directory is gone -> drop its store
                remove.add(store_rel_for(ds))
            continue
        # A change anywhere inside a 4D/BTi directory is a change to that one
        # recording too, but BTi has no extension to derive it from -- membership
        # is content-based (`bti_dirs`, computed from the current HEAD state), so
        # a still-valid BTi dir means "rebuild." A deletion that drops the
        # directory below the c,rf*/config threshold (the last processed-data or
        # config file going away) is the removal signal instead; `hs_file` (not a
        # marker name) deliberately does NOT trigger removal on its own.
        btidir = os.path.dirname(path)
        if btidir in bti_dirs:
            convert.add(btidir)
            continue
        if status == "D" and is_bti_marker_name(os.path.basename(path)):
            remove.add(store_rel_for(btidir))
            continue
        if status == "D":
            if is_split_fif(path):
                deleted_split_groups.setdefault(split_group_key(path), []).append(path)
            elif is_primary(path):
                # A buildable recording is gone -> drop its store. (If a same-name
                # primary still exists at HEAD it lands in convert below.)
                if path not in head_set:
                    remove.add(store_rel_for(path))
            else:
                # A companion/events removal still rebuilds any sibling recording
                # that remains (e.g. events.tsv deleted -> regenerate without events).
                convert |= affected_primaries(path, by_dir, member_to_head)
        else:  # "A", "M", "T", ...
            convert |= affected_primaries(path, by_dir, member_to_head)

    # Per deleted split group: if any split still exists at HEAD, re-read the chain
    # (rebuild its head); otherwise the whole recording is gone -> drop the store,
    # which was keyed at the group's head (lowest split index seen for the group).
    for gkey, deleted in deleted_split_groups.items():
        head_here = next(
            (h for h in heads if is_split_fif(h) and split_group_key(h) == gkey), None
        )
        # All entries are split FIFs, so split_index is never None here (-1 is an
        # unreachable fallback that only quiets the type checker).
        old_lowest = min(deleted, key=lambda x: (split_index(x) or 0, x))
        if head_here is not None:
            convert.add(head_here)
            # If the deletion reaches below the surviving head, the group's head
            # index shifted up (old head removed): drop its now-orphaned store. The
            # `remove -= convert_stores` guard below protects a rebuilt store.
            if (split_index(old_lowest) or -1) < (split_index(head_here) or -1):
                remove.add(store_rel_for(old_lowest))
        else:
            remove.add(store_rel_for(old_lowest))

    # A directory recording is "present" when it still has qualifying files at HEAD.
    present = head_set | dirrec_dirs | bti_dirs
    convert &= present  # never convert something not present at HEAD
    convert_stores = {store_rel_for(p) for p in convert}
    remove -= convert_stores  # a rebuilt store must not also be deleted
    return sorted(convert), sorted(remove)


def compute_clean_orphans(prior_index: dict | None, convert: list[str]) -> set[str]:
    """Stores a `--clean` run should remove: prior index stores this run does
    not (re)produce, MINUS any store under an excluded tree
    (derivatives/sourcedata/code).

    A store's rel-path missing from `convert` normally means its recording is
    gone from HEAD. But since this converter went raw-only, a derivatives/
    sourcedata/code primary is ALSO absent from `convert` -- deliberately,
    because we stopped attempting it, not because the file disappeared. A
    store rel-path mirrors its primary's directory structure (`store_rel_for`
    only swaps the extension), so `is_excluded_from_discovery` applies to it
    directly. Without this guard, going raw-only would let `--clean`'s own
    orphan-removal delete the ~4,721 already-published non-raw stores on the
    very next run -- exactly the cleanup this change must NOT perform; that
    is separate, explicitly-authorized follow-up work
    (nemarOrg/nemar-cli#1095 / nemarOrg/nemar-cli#1097).
    """
    prior_rels = {
        e["zarr"]
        for e in (prior_index or {}).get("stores", [])
        if isinstance(e, dict) and isinstance(e.get("zarr"), str)
    }
    convert_rels = {store_rel_for(p) for p in convert}
    return {
        rel for rel in prior_rels - convert_rels if not is_excluded_from_discovery(rel)
    }


def merge_index(
    prior: dict | None,
    dataset_id: str,
    head_commit: str,
    converted: list[dict],
    removed_store_rels: list[str],
    updated_utc: str,
    failures: list[dict] | None = None,
) -> dict:
    """Fold this run's results into the prior index. Pure.

    `converted` is a list of store entries (each carries a `zarr` rel-path key);
    `removed_store_rels` are `*.zarr` rels to drop. Entries for unchanged stores
    are carried over from `prior` verbatim.

    `failures` is this run's typed data failures ({path, zarr, code, reason}) --
    recordings that could not be converted for a reason the viewer should show.
    They are merged like stores: prior failures carry over, a path that converted
    (or whose store was removed) this run drops out, and this run's failures
    overlay. A path is never in both `stores` and `failures`.
    """
    failures = failures or []
    new_fail_paths = {f["path"] for f in failures if f.get("path")}

    stores: dict[str, dict] = {}
    if prior and isinstance(prior.get("stores"), list):
        for entry in prior["stores"]:
            if isinstance(entry, dict) and isinstance(entry.get("zarr"), str):
                stores[entry["zarr"]] = entry
    for rel in removed_store_rels:
        stores.pop(rel, None)
    for entry in converted:
        stores[entry["zarr"]] = entry
    # A recording that newly FAILED must not keep a stale store entry.
    stores = {z: e for z, e in stores.items() if e.get("path") not in new_fail_paths}
    ordered = [stores[k] for k in sorted(stores)]

    fails: dict[str, dict] = {}
    if prior and isinstance(prior.get("failures"), list):
        for f in prior["failures"]:
            # A path now excluded from discovery (derivatives/sourcedata/code,
            # or a BIDS calibration filename) will never be reconverted, so a
            # stale failure entry for it would otherwise persist in
            # index.json indefinitely -- showing users a failure for a file
            # we deliberately no longer serve at all.
            if (
                isinstance(f, dict)
                and f.get("path")
                and not is_excluded_from_discovery(f["path"])
            ):
                fails[f["path"]] = f
    converted_paths = {e["path"] for e in converted if e.get("path")}
    removed_set = set(removed_store_rels)
    # Drop prior failures that converted this run or whose recording was removed.
    fails = {
        p: f
        for p, f in fails.items()
        if p not in converted_paths and f.get("zarr") not in removed_set
    }
    for f in failures:
        fails[f["path"]] = f
    ordered_fails = [fails[k] for k in sorted(fails)]

    return {
        "dataset_id": dataset_id,
        "format": INDEX_FORMAT,
        "format_version": INDEX_FORMAT_VERSION,
        "source_commit": head_commit,
        "updated_utc": updated_utc,
        "store_count": len(ordered),
        "stores": ordered,
        "failure_count": len(ordered_fails),
        "failures": ordered_fails,
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


_AWS_RETRIES = int(os.environ.get("ZARR_AWS_RETRIES", "4"))
# Per-read socket timeout: the PRIMARY defense against a wedged S3 connection.
# Symptom (observed repeatedly from Hallu): aws opens several sockets to S3, the
# TCP handshakes complete, then a signed request stalls -- Send-Q backs up, no
# response bytes ever arrive, and the op sits burning a trickle of CPU on retries
# that reuse the dead socket. A SHORT read timeout is the cure: botocore abandons
# the wedged socket and reconnects (AWS_MAX_ATTEMPTS, below), and a fresh
# connection to a healthy S3 IP answers in ~200 ms, so the op recovers in seconds.
# The old 300 s made every wedge cost 5 minutes, so a recursive rm of an
# already-empty prefix could spin for hours before landing a good socket. 30 s
# reaps the wedge fast while far exceeding any healthy read gap -- a live transfer
# streams body bytes continuously, so 30 s of total silence is always a stall,
# never a legitimately slow-but-progressing read. Override with
# ZARR_AWS_READ_TIMEOUT.
_AWS_READ_TIMEOUT = os.environ.get("ZARR_AWS_READ_TIMEOUT", "30")
_AWS_TIMEOUTS = ["--cli-connect-timeout", "30", "--cli-read-timeout", _AWS_READ_TIMEOUT]
# Hard wall-clock cap per aws invocation (seconds) for transfers (cp/sync). A
# wedged process is killed and retried rather than hanging a worker forever.
# Generous so a legitimately slow multi-GB transfer never trips it; override with
# ZARR_AWS_TIMEOUT.
_AWS_OP_TIMEOUT = int(os.environ.get("ZARR_AWS_TIMEOUT", "1800"))
# Recursive deletes (`aws s3 rm --recursive` on a whole `<id>/zarr/` prefix or a
# store) are a different beast: a big dataset's prefix holds hundreds of stores x
# thousands of chunk objects = millions of keys, and DeleteObjects batches 1000 at
# a time, so a legitimate wipe can run far longer than a single transfer. Give it
# a much larger ceiling (and FEWER retries, so a true wedge fails in bounded time
# instead of N x the ceiling). The 1800 s transfer cap was killing real wipes of
# large datasets (e.g. on005261, 318 stores) mid-delete -> rebuild then 404'd.
_AWS_RM_TIMEOUT = int(os.environ.get("ZARR_AWS_RM_TIMEOUT", str(4 * 3600)))
_AWS_RM_RETRIES = int(os.environ.get("ZARR_AWS_RM_RETRIES", "2"))


def _aws_env() -> dict:
    """Environment for an aws subprocess: more internal API retries for transient
    throttle/5xx (the CLI honors ``AWS_MAX_ATTEMPTS``). The per-process S3
    transfer concurrency (``s3.max_concurrent_requests``) is config-only and is
    pinned low on the runner's profile out of band, so JOBS-way parallelism does
    not fan out to hundreds of concurrent connections (the cause of multipart
    download races and ``Need to rewind the stream`` upload failures)."""
    env = dict(os.environ)
    env.setdefault("AWS_MAX_ATTEMPTS", "10")
    return env


def _aws(
    cmd: list[str], *, timeout: int = _AWS_OP_TIMEOUT, retries: int = _AWS_RETRIES
) -> None:
    """Run an aws CLI command with a wall-clock timeout + backoff retry.

    A transfer that errors (throttle, multipart race) OR wedges past ``timeout``
    is retried; both `CalledProcessError` and `TimeoutExpired` (the wedge) count
    as a failed attempt. Raises RuntimeError after the last attempt.
    """
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            subprocess.run([*cmd, *_AWS_TIMEOUTS], check=True, timeout=timeout, env=_aws_env())
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last = exc
            if attempt < retries:
                time.sleep(min(2**attempt, 30))
    raise RuntimeError(f"aws {' '.join(cmd[1:3])} failed after {retries} attempts: {last}")


def _s3_prefix_empty(bucket: str, prefix: str) -> bool:
    """True only when a LIST of ``s3://bucket/prefix`` confirmably returns 0 keys.

    Lets the ``--clean`` wipe skip ``aws s3 rm --recursive`` when the serving
    prefix is already empty -- the common first-conversion / backfill case. The
    recursive rm still opens sockets and, if one wedges, spins on a prefix with
    nothing to delete; a single cheap LIST (short read timeout, so a wedge is
    reaped fast) sidesteps that entirely. Any error or ambiguity returns False so
    the caller falls through to the real rm rather than skipping a needed wipe.
    """
    try:
        res = subprocess.run(
            [
                "aws", "s3api", "list-objects-v2", "--bucket", bucket,
                "--prefix", prefix, "--max-items", "1",
                "--query", "Contents[0].Key", "--output", "text",
                *_AWS_TIMEOUTS,
            ],
            capture_output=True, text=True, timeout=_AWS_OP_TIMEOUT, env=_aws_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if res.returncode != 0:
        return False
    # `--query Contents[0].Key --output text` prints the first key, or "None" when
    # the prefix holds no objects.
    return res.stdout.strip() in ("", "None")


# How many `aws s3 rm --recursive` processes to shard a big prefix across. Each
# inherits the runner profile's own (deliberately low) per-process request
# concurrency, so this multiplies delete throughput without raising the transfer
# concurrency that was pinned low to stop JOBS-way uploads causing multipart
# races. A single rm measured 13.8k objects/min on Hallu; the wipes that remain
# after the --clean change are rare (recovery via --wipe) but can still be huge.
_AWS_RM_SHARDS = int(os.environ.get("ZARR_AWS_RM_SHARDS", "8"))


def _s3_child_prefixes(url: str) -> list[str]:
    """Immediate child prefixes of an ``s3://bucket/prefix/`` URL (delimited LIST).

    Used to shard a recursive delete. Returns [] on any error or when the prefix
    has no subdirectories, so the caller falls back to one unsharded rm.
    """
    rest = url[len("s3://") :]
    bucket, _, prefix = rest.partition("/")
    try:
        res = subprocess.run(
            [
                "aws", "s3api", "list-objects-v2", "--bucket", bucket,
                "--prefix", prefix, "--delimiter", "/",
                "--query", "CommonPrefixes[].Prefix", "--output", "text",
                *_AWS_TIMEOUTS,
            ],
            capture_output=True, text=True, timeout=_AWS_OP_TIMEOUT, env=_aws_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if res.returncode != 0 or res.stdout.strip() in ("", "None"):
        return []
    return [f"s3://{bucket}/{p}" for p in res.stdout.split() if p and p != "None"]


def _rm_recursive(url: str) -> None:
    """Recursively delete an S3 prefix, sharded across child prefixes.

    `aws s3 rm --recursive` is single-process and paces at its profile's request
    concurrency, so a large prefix serializes into a long, near-idle wall-clock
    block. Splitting by child prefix lets N of them run at once. Falls back to one
    unsharded rm when the prefix has no children (or the LIST fails), and always
    finishes with an unsharded sweep so any key directly under `url` -- which no
    child prefix covers -- is deleted too.
    """
    children = _s3_child_prefixes(url) if _AWS_RM_SHARDS > 1 else []
    if children:
        print(f"[zarr] deleting {url} across {len(children)} shard(s)", flush=True)
        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=min(_AWS_RM_SHARDS, len(children))) as pool:
            futures = {
                pool.submit(
                    _aws,
                    ["aws", "s3", "rm", child, "--recursive", "--only-show-errors"],
                    timeout=_AWS_RM_TIMEOUT,
                    retries=_AWS_RM_RETRIES,
                ): child
                for child in children
            }
            for fut in futures:
                try:
                    fut.result()
                except BaseException as exc:  # noqa: BLE001 -- re-raised below
                    errors.append(exc)
        if errors:
            # Surface the first failure; a partially-deleted prefix must not be
            # reported as a clean wipe.
            raise errors[0]
    # Sweep: catches loose keys at the top level, and is a cheap no-op once the
    # shards have done the bulk.
    _aws(
        ["aws", "s3", "rm", url, "--recursive", "--only-show-errors"],
        timeout=_AWS_RM_TIMEOUT,
        retries=_AWS_RM_RETRIES,
    )


def aws_cp(src: str, dst: str, *, extra: list[str] | None = None) -> None:
    # --only-show-errors drops the per-file transfer progress meter; with JOBS
    # workers each streaming a blob, that meter otherwise floods the log.
    _aws(["aws", "s3", "cp", src, dst, "--only-show-errors", *(extra or [])])


def annex_key_size(key: str | None) -> int | None:
    """Byte size a git-annex SHA256E/MD5E key declares in its ``-s<N>`` field
    (``SHA256E-s628291820--<hash>.con`` -> ``628291820``). ``None`` when the key
    carries no size (e.g. a URL/WORM key)."""
    m = re.search(r"-s(\d+)", key or "")
    return int(m.group(1)) if m else None


def _blob_key_and_size(repo_dir: str, path: str, head: str) -> tuple[str | None, int]:
    """For a tracked path at ``head``: ``(annex_key, in_git_blob_bytes)``. An
    annexed path returns ``(key, 0)`` -- its size lives in the key's ``-s`` field;
    a small in-git blob returns ``(None, len(blob))``. Reads pointers only, never
    downloads S3 content. Mirrors ``_fetch_blob``'s annex-vs-in-git decision."""
    meta = _run(["git", "-C", repo_dir, "ls-tree", head, "--", path]).strip()
    if not meta:
        return None, 0
    mode, _, rest = meta.split(" ", 2)
    sha = rest.split("\t", 1)[0].strip()
    blob = subprocess.check_output(["git", "-C", repo_dir, "cat-file", "blob", sha])
    if mode == "120000" or len(blob) < 1024:
        key = parse_annex_key(blob.decode("utf-8", "replace"))
        if key:
            return key, 0
    return None, len(blob)


def recording_size_from_pointers(
    repo_dir: str, primary_path: str, head_files: set[str], head: str
) -> int:
    """On-disk bytes of a recording's whole file set, read from git-annex pointers
    at ``head`` WITHOUT downloading: primary + same-stem companions + FIF split
    members, or every file under a directory recording (CTF ``.ds``/MEF3
    ``.mefd``/4D-BTi). Mirrors ``materialize_recording``'s wanted set and
    ``_recording_size_bytes`` so the parent's admission estimate (main) lines up
    with the worker's #909 preflight."""
    if is_dir_recording(primary_path):
        members: list[str] = [p for p in head_files if dir_recording_of(p) == primary_path]
    elif is_bti_dir(primary_path):
        # 4D/BTi: extension-less directory, so membership is exact-dirname
        # matching rather than `dir_recording_of`'s ancestor-component match.
        members = [p for p in head_files if os.path.dirname(p) == primary_path]
    else:
        d = os.path.dirname(primary_path)
        stem = filename_stem(primary_path)
        siblings = [
            p for p in head_files
            if os.path.dirname(p) == d and filename_stem(p) == stem
        ]
        members = list(
            dict.fromkeys([primary_path, *siblings, *split_members_for(primary_path, head_files)])
        )
    total = 0
    for path in members:
        key, blob_size = _blob_key_and_size(repo_dir, path, head)
        total += (annex_key_size(key) or 0) if key else blob_size
    return total


def download_blob(src: str, dst: str, expected_size: int | None) -> None:
    """Download an annex blob to ``dst`` robustly: a unique temp + atomic rename
    + size check against the key's declared size, retried with backoff.

    Large MEG/iEEG blobs (KIT ``.con`` ~1 GB, BrainVision ``.eeg`` >10 GB, split
    FIF >2 GB) intermittently arrive truncated under JOBS-way parallelism, which
    then surfaces downstream as ``not EDF compliant (Filesize)`` or a split chain
    that can't read its next file. A short/zero copy must never reach the reader:
    verify the byte count, and on any mismatch/transfer error drop the temp and
    retry rather than convert a corrupt file into a wrong store.
    """
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    last: Exception | None = None
    for attempt in range(1, _AWS_RETRIES + 1):
        tmp = f"{dst}.part.{os.getpid()}.{attempt}"
        try:
            subprocess.run(
                ["aws", "s3", "cp", src, tmp, "--only-show-errors", *_AWS_TIMEOUTS],
                check=True,
                timeout=_AWS_OP_TIMEOUT,
                env=_aws_env(),
            )
            got = os.path.getsize(tmp)
            if expected_size is not None and got != expected_size:
                raise RuntimeError(
                    f"truncated download: got {got} of {expected_size} bytes"
                )
            os.replace(tmp, dst)
            return
        except Exception as exc:  # noqa: BLE001 - any failure -> drop temp + retry
            last = exc
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            if attempt < _AWS_RETRIES:
                time.sleep(min(2**attempt, 30))
    raise RuntimeError(f"download {src} -> {dst} failed after {_AWS_RETRIES} attempts: {last}")


def s3_read_json(bucket: str, key: str) -> dict | None:
    """Read a JSON object from S3.

    Returns None ONLY for a genuine 404 (NoSuchKey) -- the legitimate first-run
    case. Any other non-zero exit (credentials, network, wrong bucket) RAISES:
    silently treating it as "no prior index" would send the run full AND drop
    every prior store from the rewritten index. A corrupt body raises for the
    same reason (absent != corrupt).
    """
    res = subprocess.run(
        ["aws", "s3", "cp", f"s3://{bucket}/{key}", "-", *_AWS_TIMEOUTS],
        capture_output=True,
        text=True,
        timeout=_AWS_OP_TIMEOUT,
        env=_aws_env(),
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


def _fetch_blob(
    repo_dir: str, bucket: str, dataset_id: str, path: str, head: str, local: str
) -> tuple[bool, str | None]:
    """Materialize one tracked path to `local`. Returns (found, annex_key).

    Annex content (locked symlink or unlocked pointer) is pulled from S3 with
    authenticated `aws s3 cp`; an in-git blob is written directly. `found=False`
    when the path is absent from `ls-tree head` (caller decides if that is fatal).
    Reads against the pinned `head` SHA so it matches the worklist's tree.
    """
    meta = _run(["git", "-C", repo_dir, "ls-tree", head, "--", path]).strip()
    if not meta:
        return False, None
    mode, _, rest = meta.split(" ", 2)
    sha = rest.split("\t", 1)[0].strip()
    blob = subprocess.check_output(["git", "-C", repo_dir, "cat-file", "blob", sha])
    key = None
    if mode == "120000" or len(blob) < 1024:
        key = parse_annex_key(blob.decode("utf-8", "replace"))
    os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
    if key:
        download_blob(f"s3://{bucket}/{dataset_id}/objects/{key}", local, annex_key_size(key))
    else:
        with open(local, "wb") as fh:
            fh.write(blob)
    return True, key


def _materialize_dir_members(
    repo_dir: str,
    bucket: str,
    dataset_id: str,
    dir_path: str,
    inner: list[str],
    head_files: set[str],
    head: str,
    work_dir: str,
    kind: str,
) -> tuple[str, str | None, str | None]:
    """Shared download step for a directory-keyed recording (CTF `.ds`, MEF3
    `.mefd`, or 4D/BTi): materialize the already-resolved `inner` member paths
    into `work_dir`, preserving the directory's internal layout the reader
    expects, plus the BIDS events sidecar if present. `kind` (e.g. ``"CTF"``,
    ``"MEF3"``, ``"4D/BTi"``) only flavors error/warning text -- the download
    logic itself is identical for all three, which is the point of factoring it
    out rather than repeating it per format. Returns (local_dir, events_local|
    None, None) -- a directory recording carries no single git-annex key of its
    own (see `materialize_recording`).
    """
    local_dir = os.path.join(work_dir, os.path.basename(dir_path))
    if not inner:
        raise RuntimeError(f"{kind} recording {dir_path!r} has no files at ls-tree {head[:8]}")
    for path in inner:
        rel = path[len(dir_path) + 1 :]  # path relative to the recording dir
        found, _ = _fetch_blob(repo_dir, bucket, dataset_id, path, head, os.path.join(local_dir, rel))
        if not found:
            # Every inner file came from `ls-tree head`; a missing one means a real
            # tree/pack desync. The recording is read as a whole, and we cannot tell
            # a mandatory file from an optional sidecar, so FAIL rather than convert
            # a partial recording into a wrong store that would then
            # `aws s3 sync --delete` over a good one.
            raise RuntimeError(
                f"{kind} file {path!r} absent from ls-tree {head[:8]}; refusing to convert a "
                "partial recording"
            )
    events_path = events_sibling_for(dir_path)
    events_local = None
    if events_path in head_files:
        events_local = os.path.join(work_dir, os.path.basename(events_path))
        found, _ = _fetch_blob(repo_dir, bucket, dataset_id, events_path, head, events_local)
        if not found:
            # Sidecar tracked at HEAD but unfetchable -> don't claim a phantom path
            # (downstream would silently embed no events); warn and drop it.
            print(f"::warning::{kind} events {events_path!r} absent from ls-tree {head[:8]}; skipping", flush=True)
            events_local = None
    return local_dir, events_local, None


def _materialize_dir_recording(
    repo_dir: str,
    bucket: str,
    dataset_id: str,
    dir_path: str,
    head_files: set[str],
    head: str,
    work_dir: str,
) -> tuple[str, str | None, str | None]:
    """Download every file under a CTF `.ds` or MEF3 `.mefd` recording directory
    (extension-derived membership; see `dir_recording_of`)."""
    inner = sorted(p for p in head_files if dir_recording_of(p) == dir_path)
    kind = "MEF3" if is_mefd(dir_path) else "CTF"
    return _materialize_dir_members(
        repo_dir, bucket, dataset_id, dir_path, inner, head_files, head, work_dir, kind
    )


def _materialize_bti(
    repo_dir: str,
    bucket: str,
    dataset_id: str,
    bti_dir: str,
    head_files: set[str],
    head: str,
    work_dir: str,
) -> tuple[str, str | None, str | None]:
    """Download every file directly inside a 4D/BTi recording directory (see
    `bti_recordings`). BIDS gives it no extension, so unlike `.ds`/`.mefd` its
    members are exact-dirname matches rather than ancestor-derived."""
    inner = sorted(p for p in head_files if os.path.dirname(p) == bti_dir)
    # Name, in this converter's own log, the same file biosigIO's `_find_bti_pdf`
    # is expected to choose (see `bti_pdf_choice`) whenever the choice is
    # ambiguous -- so an operator reading THIS converter's output can already see
    # which processed-data file will end up in the store, without cross-
    # referencing biosigIO's separate warning.
    chosen, ambiguous = bti_pdf_choice({os.path.basename(p) for p in inner})
    if chosen and ambiguous:
        print(
            f"::warning::4D/BTi {bti_dir!r} has multiple processed-data candidates; "
            f"biosigio is expected to read {chosen!r}",
            flush=True,
        )
    return _materialize_dir_members(
        repo_dir, bucket, dataset_id, bti_dir, inner, head_files, head, work_dir, "4D/BTi"
    )


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
    `_events.tsv` sidecar if present. A directory recording (CTF `.ds`/MEF3
    `.mefd`/4D-BTi) is handled by `_materialize_dir_recording`/`_materialize_bti`
    instead. Returns (primary_local_path, events_local_path|None,
    primary_annex_key|None).
    """
    if is_dir_recording(primary_path):
        return _materialize_dir_recording(repo_dir, bucket, dataset_id, primary_path, head_files, head, work_dir)
    if is_bti_dir(primary_path):
        return _materialize_bti(repo_dir, bucket, dataset_id, primary_path, head_files, head, work_dir)

    d = os.path.dirname(primary_path)
    stem = filename_stem(primary_path)
    siblings = [
        p
        for p in head_files
        if os.path.dirname(p) == d and filename_stem(p) == stem
    ]
    # For a split FIF, pull every split in the group (read_raw_fif(split-01) follows
    # the chain on disk; without split-02.. present the head read raises). Their
    # basenames are distinct, so they land beside the head under their BIDS names
    # and MNE resolves the chain. [] for non-split recordings.
    split_members = split_members_for(primary_path, head_files)
    events_path = events_sibling_for(primary_path)
    wanted = list(dict.fromkeys([primary_path, *siblings, *split_members]))
    if events_path in head_files:
        wanted.append(events_path)

    primary_key: str | None = None
    for path in wanted:
        local = os.path.join(work_dir, os.path.basename(path))
        found, key = _fetch_blob(repo_dir, bucket, dataset_id, path, head, local)
        if not found:
            if path == primary_path:
                raise RuntimeError(
                    f"primary {path!r} in the worklist but absent from ls-tree {head[:8]} "
                    "(possible pack corruption or path-encoding issue)"
                )
            print(f"::warning::companion {path!r} absent from ls-tree {head[:8]}; skipping", flush=True)
            continue
        if path == primary_path:
            primary_key = key
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


def fix_source_file_attr(store_path: str, bids_relpath: str) -> None:
    """Overwrite the store's `recording_metadata.source_file` root attribute
    with the repository-relative BIDS path.

    Every biosigIO importer calls ``rec.set_metadata("source_file", filepath)``
    with whatever path this driver handed it -- the conversion host's scratch
    materialisation (``.../zarr-scratch/tmpXXXXXXXX/work/...``), a fresh
    ``mkdtemp`` name every run. Left as-is, re-converting the same recording at
    the same source commit produces byte-different store metadata (defeating
    reproducibility), needlessly publishes the conversion host's internal
    directory layout, and names a directory that no longer exists by the time
    anyone reads it. ``bids_relpath`` is the same ``path`` this recording is
    already keyed at in index.json, so it is stable and known here without any
    extra lookup. biosigIO itself is untouched; this only corrects what NEMAR
    publishes downstream, after biosigIO has written the store and before it
    is validated/uploaded. nemarOrg/nemar-cli#1102.
    """
    meta_path = os.path.join(store_path, "zarr.json")
    with open(meta_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    # `or {}` rather than a `{}` default: an explicit `"attributes": null` makes
    # `.get("attributes", {})` return None, and the chained .get would then raise.
    rec_meta = (doc.get("attributes") or {}).get("recording_metadata")
    if not isinstance(rec_meta, dict):
        # Unexpected biosigIO version/shape: nothing to correct, and we must
        # not fabricate a key biosigIO did not write.
        print(
            f"::warning::no recording_metadata attribute at {meta_path}; "
            "source_file scratch-path fix skipped",
            flush=True,
        )
        return
    rec_meta["source_file"] = bids_relpath
    # Write to a sibling temp file then os.replace: opening `meta_path` with "w"
    # truncates it first, so an interruption mid-dump would leave a truncated
    # zarr.json behind -- and validate_store only checks that the file EXISTS,
    # not that it parses, so a corrupt one could pass the gate. os.replace is
    # atomic within a directory, so the store metadata is never half-written.
    tmp_path = f"{meta_path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp_path, meta_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


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


def _recording_size_bytes(primary_local: str) -> int:
    """On-disk size of a recording: its primary file + same-stem companions
    (`.eeg`/`.vmrk` for BrainVision; FIF is single-file), or every file under a
    directory recording. The `os.path.isdir` branch is format-agnostic, so it
    already covers CTF `.ds`, MEF3 `.mefd` (a real MEF3 session's bulk lives in
    its many `.timd/.../.tdat` channel-segment files, not any single header), and
    a 4D/BTi directory (whose bulk is the `c,rf*` processed-data file) with no
    extension-specific code -- summing the whole tree is correct for all three.
    Drives the streaming decision -- the bulk lives in the `.eeg` companion /
    `.meg4` / `.tdat` / `c,rf*`, not the tiny header files beside it.

    On any stat/listing error this returns a value that FORCES the (bounded-memory)
    streaming path rather than an undercount/zero, which would misroute a large
    recording to the OOM-prone in-memory path. Only MNE-native exts reach streaming,
    so over-forcing a small file there is at worst slower, never wrong."""
    force = 1 << 62  # exceeds any real STREAM_MIN_BYTES -> routes to streaming
    # Any directory recording (CTF `.ds` / MEF3 `.mefd` / 4D-BTi): sum the whole
    # directory tree. `os.path.isdir` alone decides this -- no extension check
    # needed, which is exactly why this branch already covers `.mefd`/BTi too.
    if os.path.isdir(primary_local):
        errored = False

        def _onerr(_exc: OSError) -> None:
            nonlocal errored
            errored = True

        total = 0
        for root, _dirs, files in os.walk(primary_local, onerror=_onerr):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    errored = True
        if errored:
            print(f"::warning::could not fully stat recording dir {primary_local!r}; forcing streaming", flush=True)
            return force
        return total
    d = os.path.dirname(primary_local) or "."
    try:
        entries = os.listdir(d)
    except OSError:
        print(f"::warning::could not list {d!r}; forcing streaming", flush=True)
        return force
    # A split FIF's bulk lives in split-02.., which carry DIFFERENT stems
    # (`_split-02_meg` vs `_split-01_meg`); summing only split-01's same-stem
    # companions undercounts the chain, so should_stream misroutes a multi-GB
    # recording onto the OOM-prone in-memory path (read_raw_fif follows the whole
    # chain on disk). Sum every local member of the split group instead. #909
    if is_split_fif(primary_local):
        gkey = split_group_key(primary_local)
        total = 0
        for fn in entries:
            full = os.path.join(d, fn)  # split_group_key keeps the dir; compare on full paths
            if is_split_fif(full) and split_group_key(full) == gkey:
                try:
                    total += os.path.getsize(full)
                except OSError:
                    print(f"::warning::could not stat split member {fn!r}; forcing streaming", flush=True)
                    return force
        return total
    stem = filename_stem(primary_local)
    total = 0
    for fn in entries:
        if os.path.splitext(fn)[0] == stem:
            try:
                total += os.path.getsize(os.path.join(d, fn))
            except OSError:
                print(f"::warning::could not stat {fn!r}; forcing streaming", flush=True)
                return force
    return total


def convert_recording(
    primary_local: str,
    events_local: str | None,
    store_path: str,
    power_line_frequency: float | None = None,
    value_descriptions: dict[str, str] | None = None,
    electrode_positions: dict | None = None,
    mem_budget_bytes: int | None = None,
) -> None:
    modality = bids_suffix_modality(primary_local)
    size_bytes = _recording_size_bytes(primary_local)
    streaming = should_stream(primary_local, size_bytes)
    # Preflight (#909): skip -- BEFORE any load -- a recording whose projected
    # peak RAM won't fit this run's budget, so it can never OOM-crash the worker
    # and BrokenProcessPool-cascade its siblings. Raised as a typed, coded failure
    # -> a DETERMINISTIC skip surfaced in the index, not an infra retry.
    if mem_budget_bytes is not None:
        peak = projected_peak_bytes(primary_local, size_bytes)
        if peak > mem_budget_bytes:
            raise RecordingTooLarge(
                f"projected peak ~{peak // 1024**3} GiB exceeds the "
                f"~{mem_budget_bytes // 1024**3} GiB per-recording budget for this run "
                f"(on-disk {size_bytes // 1024**3} GiB via the "
                f"{'streaming' if streaming else 'in-memory'} path; "
                "re-run with fewer --jobs to raise the budget)"
            )
    def _convert_in_memory() -> None:
        from biosigio import Recording, bids  # type: ignore[import-not-found]  # lazy: runtime-only dep

        # mixed_rate="resample": a Zarr store is a derived serving copy (viewing + ML),
        # not the authoritative recording, so for a mixed-sampling-rate EDF/BDF (e.g.
        # polysomnography: EEG ~200 Hz + SpO2 ~12.5 Hz) upsample the slow channels onto
        # the fastest channel's grid rather than failing the conversion. biosigIO
        # defaults to "error" everywhere else so no one gets resampled data unknowingly
        # (requires biosigio>=1.1.4; ignored for non-EDF formats). See nemar-cli#737.
        rec = Recording.from_file(primary_local, mixed_rate="resample")
        if events_local and os.path.exists(events_local):
            bids.apply_events_tsv(rec, events_local)
        # Suffix-driven modality: group + resample the whole recording by its BIDS
        # datatype (an _eeg file -> eeg_250hz), regardless of what the importer guessed
        # per channel. Without this, EEGLAB's empty chanlocs type -> MISC -> misc_1024hz.
        if modality:
            for label in rec.channels:
                rec.channels[label]["modality"] = modality
        rec.to_zarr(store_path, dtype="int16", modality_rates=MODALITY_RATES)

    # Large recordings use the streaming converter so peak RAM stays bounded; the
    # in-memory path would load them at float64 2-3x and OOM. (multi-GB BrainVision/
    # FIF/CTF/MEF3/4D-BTi, KIT above its much lower threshold, and EDF/BDF via
    # pyedflib on biosigio>=1.2.0 -- see should_stream.)
    if streaming:
        from biosigio import stream_to_zarr  # type: ignore[import-not-found]  # lazy
        from biosigio.bids import read_events_tsv  # type: ignore[import-not-found]  # lazy
        from biosigio.exceptions import MixedSamplingRateError  # type: ignore[import-not-found]

        events_df = (
            read_events_tsv(events_local)
            if events_local and os.path.exists(events_local)
            else None
        )
        try:
            stream_to_zarr(
                primary_local,
                store_path,
                force_modality=modality,
                modality_rates=MODALITY_RATES,
                dtype="int16",
                events_df=events_df,
                # Keep the temp channel-major memmap on the same (fast) scratch volume as
                # the store; it is a sibling temp dir, not synced to S3.
                scratch_dir=os.path.dirname(store_path) or None,
            )
        except MixedSamplingRateError:
            # A mixed per-channel-rate EDF can't stream on a single grid; the
            # in-memory path resamples it (mixed_rate="resample"). Re-check the
            # (larger) in-memory budget before the full-load fallback so a big
            # mixed-rate EDF is #909-skipped rather than OOMing.
            if mem_budget_bytes is not None:
                inmem_peak = int(size_bytes * INMEM_MEM_FACTOR)
                if inmem_peak > mem_budget_bytes:
                    raise RecordingTooLarge(
                        f"mixed-rate EDF needs the in-memory resample path "
                        f"(projected ~{inmem_peak // 1024**3} GiB > "
                        f"~{mem_budget_bytes // 1024**3} GiB budget); re-run with fewer --jobs"
                    ) from None
            _convert_in_memory()
    else:
        _convert_in_memory()
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
        convert_recording(
            primary_local, events_local, store_local, plf, descs or None, elec,
            mem_budget_bytes=c.get("mem_budget"),
        )
        # biosigIO stamped recording_metadata.source_file with the scratch path
        # it was handed (this run's tmpdir); overwrite it with the stable,
        # reproducible BIDS-repo-relative path before validating/uploading.
        # nemarOrg/nemar-cli#1102.
        fix_source_file_attr(store_local, primary)
        # Guard the --delete sync: an empty/partial store would otherwise wipe a
        # previously-valid one. zarr.json => v3 root.
        validate_store(store_local)
        meta = store_metadata(store_local)
        if not meta.get("groups"):
            raise RuntimeError(f"store has no channel groups: {store_local}")
        # Fidelity gate: the BIDS channels.tsv is ground truth for how many
        # channels this recording has. A store that comes up short means the
        # importer silently dropped signals (biosigio#110 served 74-channel
        # EEGLAB recordings as 1 channel for weeks, nemarDatasets/on002718#1);
        # withhold it as a typed data failure rather than publish an unfaithful
        # copy. Runs BEFORE the sync, so on the incremental path this
        # recording's previous store survives untouched; under --clean the
        # prefix was already wiped and the recording simply stays absent (see
        # the ChannelCountMismatch docstring for why absent beats unfaithful,
        # and note a gated recording needs an explicit re-run after a fix).
        expected = expected_channel_count_for(
            c["repo"], primary, c["head_files"], c["head"]
        )
        if expected:
            total = store_total_channels(meta)
            if total < expected:
                raise ChannelCountMismatch(
                    f"store has {total} channel(s) but {primary}'s channels.tsv "
                    f"declares {expected}; refusing to publish an unfaithful copy"
                )
        # Latest-only: --delete drops stale chunk objects a smaller new store no
        # longer needs. Long origin TTL; the callback purges zarr.json/index.json.
        # Through `_aws` for the wall-clock timeout + retry: a store is thousands of
        # tiny chunk PUTs, which under contention intermittently fail ("Need to
        # rewind the stream") or wedge; sync is idempotent so a retry just re-PUTs
        # whatever is missing.
        _aws([
            "aws", "s3", "sync", store_local,
            safe_store_prefix(c["bucket"], c["dataset_id"], rel_store),
            "--delete", "--only-show-errors",
            "--cache-control", "public, max-age=86400",
        ])
        entry = {
            "path": primary,
            "zarr": rel_store,
            "source_key": primary_key,
            "updated_utc": c["updated"],
            **meta,
        }
        # For a split FIF, record all member source paths so the browser can map any
        # split file (e.g. a click on split-02) to this single head store.
        members = split_members_for(primary, c["head_files"])
        if members:
            entry["split_members"] = members
        return {"ok": True, "primary": primary, "entry": entry}
    except Exception as exc:  # noqa: BLE001 - isolate one bad recording
        # biosigIO read failures carry a stable `.code` (not_continuous,
        # corrupt_or_truncated, ...) so the index can tell the viewer WHY a
        # recording has no store. Infra failures (a plain RuntimeError, a crashed
        # worker) have no code -> not surfaced, they retry on the next run.
        return {
            "ok": False,
            "primary": primary,
            "error": str(exc),
            "code": getattr(exc, "code", None),
        }
    finally:
        # Parallel workers share the NVMe scratch; reclaim each recording's copy
        # right after upload so N concurrent stores don't accumulate on disk.
        for d in (store_local, work):
            shutil.rmtree(d, ignore_errors=True)


def _next_admission(
    pending_peaks: list[int], in_flight_count: int, running_peak: int,
    cpu_cap: int, ram_ceiling: int,
) -> int | None:
    """Index into ``pending_peaks`` of the next recording to dispatch, or ``None``
    to wait for a running one to finish. Admittable when a worker slot is free AND
    either nothing is in flight (it runs alone, guaranteeing progress) or it fits
    the remaining RAM ceiling. Picks the first pending recording that fits, so a
    head-of-line giant doesn't starve smaller ones behind it."""
    if in_flight_count >= cpu_cap:
        return None
    idle = in_flight_count == 0
    return next(
        (j for j, pk in enumerate(pending_peaks)
         if idle or running_peak + pk <= ram_ceiling),
        None,
    )


def _drain_with_admission(convert, peaks, cpu_cap, ram_ceiling, ctx, record) -> None:
    """Run ``convert_one`` over ``convert`` in a pool of up to ``cpu_cap`` workers,
    dispatching a recording only while the SUM of in-flight projected peaks stays
    within ``ram_ceiling`` (see ``_next_admission``). Results are reported via
    ``record(r, i)`` in completion order; a crashed worker is a non-coded (infra)
    failure that retries next run."""
    pending = list(convert)
    in_flight: dict = {}  # future -> (primary, peak)
    running_peak = 0
    done = 0
    with ProcessPoolExecutor(
        max_workers=cpu_cap, initializer=_init_worker, initargs=(ctx,)
    ) as ex:
        def admit() -> None:
            nonlocal running_peak
            while pending:
                idx = _next_admission(
                    [peaks[p] for p in pending], len(in_flight), running_peak,
                    cpu_cap, ram_ceiling,
                )
                if idx is None:
                    break
                p = pending.pop(idx)
                in_flight[ex.submit(convert_one, p)] = (p, peaks[p])
                running_peak += peaks[p]

        admit()
        while in_flight:
            finished, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
            for fut in finished:
                p, peak = in_flight.pop(fut)
                running_peak -= peak
                done += 1
                try:
                    r = fut.result()
                except Exception as exc:  # worker process died (OOM/segfault)
                    r = {"ok": False, "primary": p, "error": f"worker crashed: {exc}"}
                record(r, done)
            admit()


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
        help="full-rebuild every recording and rewrite the index fresh (no merge). "
        "Each store is uploaded with `aws s3 sync --delete`, so its contents are "
        "reconciled exactly; stores for recordings no longer at HEAD are removed "
        "afterwards. Does NOT erase the serving prefix up front -- see --wipe. "
        "Implies --full.",
    )
    ap.add_argument(
        "--wipe",
        action="store_true",
        help="erase s3://<bucket>/<id>/zarr/ before rebuilding. Recovery only (a "
        "corrupt prefix, or an index that no longer describes what is on S3): it "
        "destroys the serving copy before the replacement exists, so the dataset "
        "has no viewer for the length of the run. Use with --clean.",
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

    # --clean rebuilds every recording from scratch: no diff, and the index is
    # rewritten fresh (no carry-forward of stale entries) rather than merged.
    # The prior index is still READ -- purely to find orphaned stores below, not
    # to seed the merge or the diff.
    prior_for_orphans: dict | None = None
    if args.clean:
        prior, prior_commit, full = None, None, True
        prior_for_orphans = s3_read_json(bucket, f"{dataset_id}/zarr/index.json")
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

    # --clean no longer wipes the serving prefix up front.
    #
    # It used to, and that dominated the run: nm000338 (a v1.0.1 -> v1.0.2 bump)
    # spent ~45 min deleting ~620k objects at 13.8k/min before converting a single
    # recording, with 30 of 32 cores idle -- then re-uploaded almost exactly what
    # it had just deleted. The wipe was never what made the copy exact: each store
    # is uploaded with `aws s3 sync --delete`, which already reconciles that
    # store's contents precisely (stale chunks, renamed groups, a shortened
    # recording). The ONLY thing the wipe added was dropping stores for recordings
    # that no longer exist at HEAD.
    #
    # So compute exactly those, and hand them to the `remove` path that the
    # incremental branch already uses -- which deletes per store, AFTER a
    # successful conversion, instead of pre-emptively destroying a good serving
    # copy. A recording that is still at HEAD but fails to convert is in `convert`,
    # never an orphan, so it keeps its previous store (ADR 0005: partial data
    # still serves) instead of being deleted by a wipe that ran before we knew.
    #
    # `--wipe` keeps the old behaviour for recovery (a corrupt prefix, an index
    # that no longer describes what is on S3).
    if args.clean:
        # `compute_clean_orphans` also protects already-published stores under
        # an excluded tree (derivatives/sourcedata/code) from this removal: a
        # raw-only `convert` no longer contains them, but that must not be
        # misread as "gone from HEAD" -- see its docstring.
        orphans = compute_clean_orphans(prior_for_orphans, convert)
        if orphans:
            print(
                f"[zarr] --clean: {len(orphans)} store(s) no longer at HEAD; "
                "removing those, keeping the rest in place",
                flush=True,
            )
        # Stays a sorted LIST: `remove` is JSON-serialized into the callback and
        # the index, and a set would blow up json.dump.
        remove = sorted(set(remove) | orphans)
    if args.wipe and convert:
        prefix = f"{dataset_id}/zarr/"
        if _s3_prefix_empty(bucket, prefix):
            print(f"[zarr] --wipe: s3://{bucket}/{prefix} already empty; skipping wipe", flush=True)
        else:
            print(f"[zarr] --wipe: erasing s3://{bucket}/{prefix} before full rebuild", flush=True)
            _rm_recursive(f"s3://{bucket}/{prefix}")
    print(
        f"[zarr] {dataset_id} head={head[:8]} prior={(prior_commit or 'none')[:8]} "
        f"full={full} convert={len(convert)} remove={len(remove)}",
        flush=True,
    )

    head_set = set(head_files)
    converted_entries: list[dict] = []
    failures: list[str] = []
    failure_entries: list[dict] = []

    n = len(convert)

    def record(r: dict, i: int) -> None:
        # Log each recording as it finishes (live progress over a long backfill),
        # not all at once at the end.
        if r["ok"]:
            converted_entries.append(r["entry"])
            print(f"[zarr] [{i}/{n}] converted {r['primary']} -> {r['entry']['zarr']}", flush=True)
        else:
            failures.append(r["primary"])
            # A typed biosigIO failure (it carries a .code) is a property of the
            # DATA -- record WHY in the index so the viewer can explain it. An infra
            # failure (no code: crashed worker, transient S3) is omitted so it
            # retries on the next run rather than being shown as a data problem.
            code = r.get("code")
            if code:
                failure_entries.append({
                    "path": r["primary"],
                    "zarr": store_rel_for(r["primary"]),
                    "code": code,
                    "reason": reason_for_code(code),
                })
            print(f"::warning::[{i}/{n}] conversion failed for {r['primary']}: {r['error']}", flush=True)

    cpu_cap = max(1, args.jobs)
    ram_ceiling = per_recording_ceiling_bytes()
    # RAM-admission control: workers are sized to CPU (cpu_cap), but a recording is
    # dispatched only while the SUM of in-flight projected peaks stays within the
    # node's usable RAM. Small (EEG) recordings pack many-wide -> cores stay busy;
    # large (MEG) ones self-limit concurrency -> no OOM; and a recording is
    # #909-skipped only when it can't fit the node even alone (independent of jobs).
    # Peaks are projected from git-annex pointers (no download) so the estimate is
    # cheap and matches the worker's preflight.
    peaks = {
        p: projected_peak_bytes(p, recording_size_from_pointers(repo, p, head_set, head))
        for p in convert
    }
    print(
        f"[zarr] admission: up to {cpu_cap} worker(s), RAM ceiling "
        f"~{ram_ceiling // 1024**3} GiB; a recording projected above it alone is "
        f"skipped (#909)",
        flush=True,
    )
    with tempfile.TemporaryDirectory() as tmp:
        ctx = {
            "repo": repo, "bucket": bucket, "dataset_id": dataset_id, "head": head,
            "head_files": head_set, "local": args.local, "tmp": tmp, "updated": updated,
            "mem_budget": ram_ceiling,
        }
        if cpu_cap == 1 or n <= 1:
            _init_worker(ctx)
            for i, p in enumerate(convert, 1):
                record(convert_one(p), i)
        else:
            _drain_with_admission(convert, peaks, cpu_cap, ram_ceiling, ctx, record)

    for rel_store in remove:
        _rm_recursive(safe_store_prefix(bucket, dataset_id, rel_store))
        print(f"[zarr] removed store {rel_store}", flush=True)

    # `deterministic` = every failure is a typed DATA failure (biosigIO carries a
    # `.code`); none are infra (crashed worker / transient S3). The driver uses
    # this to mark a total failure terminal (`data_failed`, no retry) vs infra
    # (bounded retry) — and the backend records it for the failures dashboard.
    # See nemarOrg/nemar-cli#774.
    infra_failures = len(failures) - len(failure_entries)
    deterministic = bool(failures) and infra_failures == 0

    # Hard fail: every attempted conversion errored and nothing was removed. Do
    # NOT advance the checkpoint or rewrite the index (that would strand the
    # failed recordings); return non-zero. Still write the callback (status
    # "failed") so the driver can classify data-vs-infra and the backend records
    # WHAT failed even on a total failure (#774 — previously no callback was
    # written here, so total failures were invisible).
    if convert and not converted_entries and not remove:
        print(f"::error::all {len(convert)} conversion(s) failed; index left untouched", flush=True)
        with open(args.callback_out, "w") as fh:
            json.dump(
                {
                    "dataset_id": dataset_id,
                    "status": "failed",
                    "store_count": int((prior or {}).get("store_count", 0) or 0),
                    "commit": head,
                    "converted": [],
                    "removed": [],
                    "errors": len(failures),
                    "failed": failures,
                    "failure_count": len(failure_entries),
                    "data_failures": failure_entries,
                    "deterministic": deterministic,
                },
                fh,
            )
        return 1

    # Advance source_commit to HEAD unless there are INFRA failures to retry. A
    # typed data failure (a derivative, a corrupt file) is permanent -- retrying it
    # never helps and would pin the checkpoint forever on a derivative-heavy
    # dataset -- so it does not hold the commit back; it's recorded in the index's
    # `failures` instead. An infra failure (no code: crashed worker, transient S3)
    # keeps the prior commit ("" -> next run goes full) so it is re-diffed + retried.
    # (`infra_failures` / `deterministic` computed above, before the total-fail path.)
    index_commit = head if not infra_failures else (prior_commit or "")
    index = merge_index(
        prior, dataset_id, index_commit, converted_entries, remove, updated, failure_entries
    )
    # `delete=False` is deliberate -- aws_cp reads the file back by path after the
    # handle closes -- but it left the upload as the file's only reader and nothing
    # as its owner, so every dataset conversion leaked one temp file into TMPDIR
    # (= the Hallu NVMe scratch); 544 strays had piled up by 2026-08-12. Unlink in
    # a `finally` so a failed upload leaks nothing either. nemarOrg/nemar-cli#1068.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(index, fh, separators=(",", ":"))
        index_local = fh.name
    try:
        aws_cp(
            index_local,
            f"s3://{bucket}/{dataset_id}/zarr/index.json",
            extra=["--content-type", "application/json", "--cache-control", "public, max-age=60"],
        )
        etag = _run(
            ["aws", "s3api", "head-object", "--bucket", bucket, "--key",
             f"{dataset_id}/zarr/index.json", "--query", "ETag", "--output", "text"]
        ).strip().strip('"')
    finally:
        with contextlib.suppress(OSError):
            os.unlink(index_local)

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
        # Typed data failures (recordings the viewer should explain, not retry).
        "failure_count": index["failure_count"],
        "data_failures": failure_entries,
        # On a partial run the dataset is still `done` (the index has what
        # converted); `deterministic` only tells the backend whether the skipped
        # recordings are data (won't retry) vs infra. See #774.
        "deterministic": deterministic,
    }
    with open(args.callback_out, "w") as fh:
        json.dump(callback, fh)

    if failures:
        print(f"::error::{len(failures)} recording(s) failed to convert: {failures}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
