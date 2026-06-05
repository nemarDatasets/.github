#!/usr/bin/env python3
"""
Real-git integration test for scripts/emit_records.py.

No mocks, no fake data. We initialise an actual git repo in a tmp dir,
commit a real BIDS-shaped tree (committed `*_eeg.json` sidecars carrying
RecordingDuration/SamplingFrequency, `*_channels.tsv` with a type column,
one primary stored as a real git-annex symlink AND one stored as a git
blob, plus a root-level inherited sidecar), tag it v0.0.0, then invoke
emit_records.py against it and assert:

  - the emitted records validate against the released neuroschema v0.3.0
    schema (located at the sibling neuroschema checkout), and
  - the derived fields are correct, including the no-sidecar /
    no-channels.tsv / no-RecordingDuration -> null branches and BIDS
    inheritance from the dataset root.

The neuroschema validation arm is SKIPPED (not failed) when the sibling
checkout or the jsonschema dependency is unavailable, so the field-level
assertions still run in a bare environment; CI installs jsonschema so the
validation arm runs there. The field assertions are the real correctness
gate either way.

Run with either:
    python3 scripts/test_emit_records.py
    uv run --with jsonschema python scripts/test_emit_records.py
    uv run --with pytest --with jsonschema python -m pytest scripts/test_emit_records.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EMIT = HERE / "emit_records.py"

# Sibling neuroschema checkout: ~/git/nemar/{nemarDatasets-github,neuroschema}
NEUROSCHEMA_DIR = HERE.parent.parent / "neuroschema"
NEUROSCHEMA_SCHEMA_DIR = NEUROSCHEMA_DIR / "schema"

# Primary stored as a real git-annex symlink (locked mode).
ANNEX_KEY = "SHA256E-s12345--abcdef0123456789.edf"
ANNEX_REL_PATH = "sub-01/eeg/sub-01_task-rest_eeg.edf"
ANNEX_TARGET = f"../../.git/annex/objects/aa/bb/{ANNEX_KEY}/{ANNEX_KEY}"

# Second subject's primary stored as a plain git blob (a stub byte payload;
# Tier-1 never reads the primary's bytes, only enumerates the path).
BLOB_REL_PATH = "sub-02/eeg/sub-02_task-rest_eeg.edf"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True)


def setup_repo(repo: Path) -> None:
    """Initialise a real BIDS-shaped git repo and tag it v0.0.0.

    Tree:
      task-rest_eeg.json                        (ROOT sidecar; SF=256, PLF=60,
                                                 NO RecordingDuration -> inherited
                                                 by sub-02, overridden by sub-01)
      sub-01/eeg/sub-01_task-rest_eeg.json      (SF=256, RecordingDuration=10 ->
                                                 duration + ntimes path)
      sub-01/eeg/sub-01_task-rest_channels.tsv  (2 EEG + 1 EOG, real type col)
      sub-01/eeg/sub-01_task-rest_eeg.edf       (annex SYMLINK primary)
      sub-02/eeg/sub-02_task-rest_eeg.edf       (git BLOB primary; inherits the
                                                 ROOT sidecar -> SF=256 but
                                                 recording_duration/ntimes null;
                                                 NO channels.tsv -> nchans null)
    """
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@nemar.local")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")

    (repo / "dataset_description.json").write_text(
        json.dumps(
            {"Name": "Test", "BIDSVersion": "1.8.0", "DatasetType": "raw"}, indent=2
        )
    )
    (repo / "README").write_text("# Test dataset\n")

    # ROOT-level inherited sidecar: SamplingFrequency only, NO RecordingDuration.
    (repo / "task-rest_eeg.json").write_text(
        json.dumps(
            {"TaskName": "rest", "SamplingFrequency": 256, "PowerLineFrequency": 60},
            indent=2,
        )
    )

    # sub-01: a more-specific sidecar that ADDS RecordingDuration and a real
    # channels.tsv with a type column.
    (repo / "sub-01" / "eeg").mkdir(parents=True)
    (repo / "sub-01" / "eeg" / "sub-01_task-rest_eeg.json").write_text(
        json.dumps({"SamplingFrequency": 256, "RecordingDuration": 10}, indent=2)
    )
    (repo / "sub-01" / "eeg" / "sub-01_task-rest_channels.tsv").write_text(
        "name\ttype\tunits\tstatus\n"
        "Fp1\tEEG\tuV\tgood\n"
        "Fp2\tEEG\tuV\tgood\n"
        "VEOG\tEOG\tuV\tgood\n"
    )
    # sub-01 primary as a real annex-style symlink (locked mode).
    annex_path = repo / ANNEX_REL_PATH
    os.symlink(ANNEX_TARGET, annex_path)

    # sub-02: NO own sidecar (inherits the ROOT one -> SF=256, no
    # RecordingDuration), NO channels.tsv. Primary as a plain git blob.
    (repo / "sub-02" / "eeg").mkdir(parents=True)
    (repo / BLOB_REL_PATH).write_bytes(b"EDFSTUB\x00not-real-edf-bytes")

    git(repo, "add", "-A")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
    env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
    subprocess.check_call(
        ["git", "-C", str(repo), "commit", "-q", "-m", "Initial dataset"], env=env
    )
    git(repo, "tag", "v0.0.0")


def run_emit(repo: Path, out: Path) -> subprocess.CompletedProcess:
    """Invoke emit_records.py against the tmp repo."""
    return subprocess.run(
        [
            sys.executable,
            str(EMIT),
            "--dataset-id",
            "nm099999",
            "--version",
            "0.0.0",
            "--repo-dir",
            str(repo),
            "--out-dir",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _neuroschema_validate(records: list[dict]) -> list[str] | None:
    """Validate each record through the neuroschema ROOT envelope (which
    dispatches doc_type 'record' -> core/record.schema.json) using the
    released v0.3.0 schema in the sibling checkout.

    Returns a list of "[i] path: message" error strings ([] when all valid),
    or None when the validator is unavailable (sibling checkout missing or
    jsonschema not installed) so the caller can skip rather than fail.
    """
    if not NEUROSCHEMA_SCHEMA_DIR.is_dir():
        return None
    try:
        from jsonschema import Draft202012Validator, RefResolver
    except ImportError:
        return None

    # Build the URI->schema store once (mirrors neuroschema.validate._build_store):
    # both the $id and the file:// URL form so relative $refs resolve either way.
    store: dict[str, dict] = {}
    for schema_file in NEUROSCHEMA_SCHEMA_DIR.rglob("*.schema.json"):
        with open(schema_file) as fh:
            schema = json.load(fh)
        if "$id" in schema:
            rel = schema_file.relative_to(NEUROSCHEMA_SCHEMA_DIR)
            store[f"file://{NEUROSCHEMA_SCHEMA_DIR}/{rel}"] = schema
            store[schema["$id"]] = schema
    root_path = NEUROSCHEMA_SCHEMA_DIR / "neuroschema.schema.json"
    with open(root_path) as fh:
        root = json.load(fh)

    # IMPORTANT: build a FRESH RefResolver + validator per document, exactly as
    # neuroschema.validate.validate_document does. A single shared RefResolver
    # carries a mutable scope stack across validations; reusing it across
    # records corrupts the base URI for relative $refs ("core/record.schema.json"
    # resolving against a leftover "definitions/" scope). Per-doc resolvers
    # keep each validation hermetic.
    errors: list[str] = []
    for i, doc in enumerate(records):
        resolver = RefResolver(
            base_uri=f"file://{NEUROSCHEMA_SCHEMA_DIR}/", referrer=root, store=store
        )
        validator = Draft202012Validator(root, resolver=resolver)
        for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
            path = ".".join(str(p) for p in e.absolute_path) or "(root)"
            errors.append(f"[{i}] {path}: {e.message}")
    return errors


class EmitRecordsRealGitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-records-test-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"
        setup_repo(cls.repo)
        cls.proc = run_emit(cls.repo, cls.out)
        cls.records = json.loads((cls.out / "records.json").read_text())
        cls.by_path = {r["bids_relpath"]: r for r in cls.records}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ---- output + process ---------------------------------------------------

    def test_process_succeeded(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)

    def test_records_file_written(self):
        self.assertTrue((self.out / "records.json").exists())

    def test_records_is_json_array(self):
        self.assertIsInstance(self.records, list)

    def test_both_primaries_enumerated(self):
        # One annex-symlink primary, one git-blob primary -> two records,
        # regardless of annex storage mode.
        self.assertEqual(
            set(self.by_path.keys()), {ANNEX_REL_PATH, BLOB_REL_PATH}
        )

    # ---- neuroschema v0.3.0 validation --------------------------------------

    def test_records_validate_against_neuroschema_v030(self):
        errors = _neuroschema_validate(self.records)
        if errors is None:
            self.skipTest(
                "neuroschema sibling checkout or jsonschema unavailable; "
                "field-level assertions still cover correctness"
            )
        self.assertEqual(errors, [], "neuroschema validation errors:\n" + "\n".join(errors))

    # ---- annex-symlink primary (sub-01): full sidecar + channels.tsv --------

    def test_annex_record_core_fields(self):
        rec = self.by_path[ANNEX_REL_PATH]
        self.assertEqual(rec["schema_version"], "0.3.0")
        self.assertEqual(rec["doc_type"], "record")
        self.assertEqual(rec["dataset"], "nm099999")
        self.assertEqual(rec["modality"], "EEG")
        self.assertEqual(rec["datatype"], "eeg")
        self.assertEqual(rec["suffix"], "eeg")
        self.assertEqual(rec["file_extension"], ".edf")

    def test_annex_record_entities_mapped_to_long_keys(self):
        rec = self.by_path[ANNEX_REL_PATH]
        # sub-01_task-rest -> subject/task; only the six allowed keys present.
        self.assertEqual(rec["entities"], {"subject": "01", "task": "rest"})

    def test_annex_record_sampling_frequency_from_sidecar(self):
        rec = self.by_path[ANNEX_REL_PATH]
        self.assertEqual(rec["signal_properties"]["sampling_frequency"], 256)
        # sampling_frequency is signal_properties-only, NEVER in signal_summary.
        self.assertNotIn("sampling_frequency", rec["signal_summary"])

    def test_annex_record_recording_duration_and_ntimes(self):
        rec = self.by_path[ANNEX_REL_PATH]
        # RecordingDuration=10 from the more-specific sub-01 sidecar.
        self.assertEqual(rec["signal_summary"]["recording_duration"], 10)
        # ntimes = round(256 * 10) = 2560.
        self.assertEqual(rec["signal_summary"]["ntimes"], 2560)

    def test_annex_record_channels_from_tsv(self):
        rec = self.by_path[ANNEX_REL_PATH]
        # channels.tsv is AUTHORITATIVE: 3 data rows, 2 EEG + 1 EOG.
        self.assertEqual(rec["signal_summary"]["nchans"], 3)
        self.assertEqual(
            rec["signal_summary"]["channel_type_counts"], {"EEG": 2, "EOG": 1}
        )

    def test_annex_record_provenance_digested_at(self):
        rec = self.by_path[ANNEX_REL_PATH]
        self.assertEqual(set(rec["provenance"].keys()), {"digested_at"})
        self.assertTrue(rec["provenance"]["digested_at"].endswith("Z"))
        # No forbidden duration_source key anywhere.
        self.assertNotIn("duration_source", rec["provenance"])

    # ---- git-blob primary (sub-02): inheritance + null branches -------------

    def test_blob_record_inherits_root_sidecar(self):
        rec = self.by_path[BLOB_REL_PATH]
        # sub-02 has no own sidecar; it inherits the ROOT task-rest_eeg.json
        # (SamplingFrequency=256) via the inheritance walk.
        self.assertEqual(rec["signal_properties"]["sampling_frequency"], 256)
        self.assertEqual(rec["entities"], {"subject": "02", "task": "rest"})

    def test_blob_record_recording_duration_null(self):
        rec = self.by_path[BLOB_REL_PATH]
        # ROOT sidecar declares NO RecordingDuration -> recording_duration null.
        self.assertIsNone(rec["signal_summary"]["recording_duration"])
        # ...and ntimes null because RecordingDuration is missing.
        self.assertIsNone(rec["signal_summary"]["ntimes"])

    def test_blob_record_no_channels_tsv_nchans_null(self):
        rec = self.by_path[BLOB_REL_PATH]
        # sub-02 has NO channels.tsv and no *ChannelCount fallback -> null.
        self.assertIsNone(rec["signal_summary"]["nchans"])
        self.assertIsNone(rec["signal_summary"]["channel_type_counts"])


class EmitRecordsNoSidecarTests(unittest.TestCase):
    """A primary with NO sidecar at all: signal_properties is omitted entirely
    and every signal_summary value is null, but the record still validates."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-records-nosidecar-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"
        cls.repo.mkdir(parents=True)
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "test@nemar.local")
        git(cls.repo, "config", "user.name", "Test")
        git(cls.repo, "config", "commit.gpgsign", "false")
        (cls.repo / "dataset_description.json").write_text(
            json.dumps({"Name": "Bare", "BIDSVersion": "1.8.0"}, indent=2)
        )
        # A primary with NO sidecar and NO channels.tsv at all.
        (cls.repo / "sub-01" / "eeg").mkdir(parents=True)
        (cls.repo / "sub-01" / "eeg" / "sub-01_task-rest_eeg.edf").write_bytes(
            b"EDFSTUB\x00"
        )
        git(cls.repo, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        subprocess.check_call(
            ["git", "-C", str(cls.repo), "commit", "-q", "-m", "bare"], env=env
        )
        git(cls.repo, "tag", "v0.0.0")
        run_emit(cls.repo, cls.out)
        cls.records = json.loads((cls.out / "records.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_single_record(self):
        self.assertEqual(len(self.records), 1)

    def test_signal_properties_omitted_when_no_sidecar(self):
        rec = self.records[0]
        self.assertNotIn("signal_properties", rec)

    def test_all_signal_summary_values_null(self):
        rec = self.records[0]
        self.assertEqual(
            rec["signal_summary"],
            {
                "nchans": None,
                "ntimes": None,
                "recording_duration": None,
                "channel_type_counts": None,
            },
        )

    def test_bare_record_still_validates(self):
        errors = _neuroschema_validate(self.records)
        if errors is None:
            self.skipTest("neuroschema/jsonschema unavailable")
        self.assertEqual(errors, [], "\n".join(errors))


class ParseChannelsTsvUnitTests(unittest.TestCase):
    """Direct unit coverage of the channels.tsv parser branches (n/a skip,
    missing type column, short rows) without spinning a git repo."""

    def setUp(self):
        sys.path.insert(0, str(HERE))
        import emit_records

        self.parse = emit_records.parse_channels_tsv

    def test_na_and_empty_type_skipped_but_counted_in_nchans(self):
        text = (
            "name\ttype\tunits\n"
            "A\tEEG\tuV\n"
            "B\tn/a\tuV\n"
            "C\t\tuV\n"
            "D\tEOG\tuV\n"
        )
        nchans, counts = self.parse(text)
        self.assertEqual(nchans, 4)  # all 4 rows count toward nchans
        self.assertEqual(counts, {"EEG": 1, "EOG": 1})  # n/a + empty skipped

    def test_no_type_column_yields_null_counts(self):
        text = "name\tunits\nA\tuV\nB\tuV\n"
        nchans, counts = self.parse(text)
        self.assertEqual(nchans, 2)
        self.assertIsNone(counts)

    def test_short_row_missing_type_cell(self):
        text = "name\ttype\nA\tEEG\nB\n"  # row B has no type cell
        nchans, counts = self.parse(text)
        self.assertEqual(nchans, 2)
        self.assertEqual(counts, {"EEG": 1})

    def test_header_only_channels_tsv_yields_null_nchans(self):
        # Zero data rows -> nchans None (not 0) so the *ChannelCount sidecar
        # fallback can fire in build_record.
        nchans, counts = self.parse("name\ttype\tunits\n")
        self.assertIsNone(nchans)
        self.assertIsNone(counts)


class ModalityConfigUnitTests(unittest.TestCase):
    """Every PRIMARY_EXTS extension must have a suffix in MODALITY, or
    build_record silently skips that recording (modality is required)."""

    def setUp(self):
        sys.path.insert(0, str(HERE))
        import emit_records

        self.m = emit_records

    def test_nirs_suffix_is_mapped(self):
        # Regression: .snirf was in PRIMARY_EXTS with no `nirs` modality, so
        # every fNIRS recording was silently dropped.
        self.assertIn(".snirf", self.m.PRIMARY_EXTS)
        self.assertEqual(self.m.MODALITY.get("nirs"), "NIRS")
        self.assertIn("nirs", self.m.BIDS_DATATYPES)

    def test_every_primary_ext_has_a_modality(self):
        ext_suffix = {
            ".set": "eeg",  # EEGLAB carries eeg/meg/ieeg; eeg is the common case
            ".edf": "eeg",
            ".bdf": "eeg",
            ".vhdr": "eeg",
            ".fif": "meg",
            ".snirf": "nirs",
        }
        for ext in self.m.PRIMARY_EXTS:
            self.assertIn(ext, ext_suffix, f"{ext} missing from the smoke map")
            self.assertIn(
                ext_suffix[ext],
                self.m.MODALITY,
                f"PRIMARY_EXTS {ext} (suffix {ext_suffix[ext]}) has no MODALITY entry",
            )


if __name__ == "__main__":
    unittest.main()
