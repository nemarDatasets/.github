#!/usr/bin/env python3
"""
Real-git integration test for scripts/emit_manifest.py.

No mocks. We initialise an actual git repo in a tmp dir, commit a real
BIDS-shaped tree (a dataset_description.json, a README, a couple of
sub-XX/eeg/ files, and one real symlink mimicking a git-annex pointer),
tag it v0.0.0, then invoke emit_manifest.py against it with the canary
disabled and assert the JSON artifacts on disk match the documented shape.

Run with either:
    python3 scripts/test_emit_manifest.py
    uv run python scripts/test_emit_manifest.py
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
EMIT = HERE / "emit_manifest.py"

ANNEX_KEY = "SHA256E-s12345--abcdef0123456789.edf"
ANNEX_REL_PATH = "sub-01/eeg/sub-01_task-rest_eeg.edf"
ANNEX_TARGET = (
    f"../../.git/annex/objects/aa/bb/{ANNEX_KEY}/{ANNEX_KEY}"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True)


def setup_repo(repo: Path) -> None:
    """Initialise a real BIDS-shaped git repo and tag it v0.0.0."""
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@nemar.local")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")

    # dataset_description.json (BIDS root metadata)
    (repo / "dataset_description.json").write_text(
        json.dumps(
            {"Name": "Test", "BIDSVersion": "1.8.0", "DatasetType": "raw"},
            indent=2,
        )
    )
    # README.md (BIDS root README)
    (repo / "README.md").write_text("# Test dataset\n\nA real test dataset.\n")
    # A regular per-subject EEG sidecar (git-tracked, not annexed)
    (repo / "sub-01" / "eeg").mkdir(parents=True)
    (repo / "sub-01" / "eeg" / "sub-01_task-rest_eeg.json").write_text(
        json.dumps({"SamplingFrequency": 500}, indent=2)
    )
    # A second subject so derive_subjects has something to sort.
    (repo / "sub-02" / "eeg").mkdir(parents=True)
    (repo / "sub-02" / "eeg" / "sub-02_task-rest_eeg.json").write_text(
        json.dumps({"SamplingFrequency": 500}, indent=2)
    )

    # A real annex-style symlink: the link target encodes the key.
    annex_path = repo / ANNEX_REL_PATH
    annex_path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(ANNEX_TARGET, annex_path)

    git(repo, "add", "-A")
    # GIT_COMMITTER_DATE pinned so the tag SHA is reproducible across runs
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
    env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
    subprocess.check_call(
        ["git", "-C", str(repo), "commit", "-q", "-m", "Initial dataset"],
        env=env,
    )
    git(repo, "tag", "v0.0.0")


def run_emit(repo: Path, out: Path) -> subprocess.CompletedProcess:
    """Invoke emit_manifest.py against the tmp repo with canary disabled."""
    return subprocess.run(
        [
            sys.executable,
            str(EMIT),
            "--dataset-id",
            "nm099999",
            "--version",
            "0.0.0",
            "--doi",
            "10.82901/nemar.nm099999.v0.0.0",
            "--concept-doi",
            "10.82901/nemar.nm099999",
            "--repo-dir",
            str(repo),
            "--out-dir",
            str(out),
            "--no-verify-canary",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


class EmitManifestRealGitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-test-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"
        setup_repo(cls.repo)
        cls.proc = run_emit(cls.repo, cls.out)
        cls.manifest = json.loads((cls.out / "manifest.json").read_text())
        cls.summary = json.loads((cls.out / "summary.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ---- output files exist -------------------------------------------------

    def test_manifest_file_written(self):
        self.assertTrue((self.out / "manifest.json").exists())

    def test_summary_file_written(self):
        self.assertTrue((self.out / "summary.json").exists())

    def test_totals_file_written_for_workflow_callback(self):
        # totals.json is read by scripts/build_callback_body.py inside the
        # central workflow. Without it the callback step would 500.
        totals_path = self.out / "totals.json"
        self.assertTrue(totals_path.exists())
        totals = json.loads(totals_path.read_text())
        self.assertEqual(
            set(totals.keys()),
            {"files", "bytes", "annex", "git"},
        )
        self.assertEqual(totals["files"], len(self.manifest["files"]))
        self.assertGreaterEqual(totals["annex"], 1)  # we committed an annex symlink

    # ---- manifest shape -----------------------------------------------------

    def test_manifest_top_level_keys(self):
        self.assertEqual(
            set(self.manifest.keys()),
            {"dataset_id", "version", "doi", "concept_doi", "created", "files"},
        )
        self.assertEqual(self.manifest["dataset_id"], "nm099999")
        # leading 'v' must be stripped from VersionManifest.version
        self.assertEqual(self.manifest["version"], "0.0.0")
        self.assertEqual(self.manifest["doi"], "10.82901/nemar.nm099999.v0.0.0")
        self.assertEqual(self.manifest["concept_doi"], "10.82901/nemar.nm099999")
        self.assertTrue(self.manifest["created"].endswith("Z"))

    def test_dataset_description_is_git_keyed(self):
        meta = self.manifest["files"].get("dataset_description.json")
        self.assertIsNotNone(meta, "dataset_description.json missing from manifest")
        self.assertTrue(meta["key"].startswith("git:"), f"unexpected key={meta['key']}")
        self.assertEqual(meta["checksum"], meta["key"])
        self.assertGreater(meta["size"], 0)
        # bytes_url for a git-keyed file -> raw.githubusercontent pinned to the tag.
        self.assertEqual(
            meta["bytes_url"],
            "https://raw.githubusercontent.com/nemarDatasets/nm099999/v0.0.0/dataset_description.json",
        )

    def test_annex_symlink_is_annex_keyed(self):
        meta = self.manifest["files"].get(ANNEX_REL_PATH)
        self.assertIsNotNone(meta, f"{ANNEX_REL_PATH} missing from manifest")
        self.assertEqual(meta["key"], ANNEX_KEY)
        self.assertEqual(meta["size"], 12345)
        self.assertEqual(meta["checksum"], "sha256:abcdef0123456789")
        # bytes_url for an annex-keyed file -> the data.nemar.org per-file route.
        self.assertEqual(
            meta["bytes_url"],
            "https://data.nemar.org/nm099999/v0.0.0/sub-01/eeg/sub-01_task-rest_eeg.edf",
        )

    def test_git_internals_excluded(self):
        for path in self.manifest["files"].keys():
            self.assertFalse(path.startswith(".git/"))
            self.assertFalse(path.startswith(".github/"))

    # ---- summary shape ------------------------------------------------------

    def test_summary_schema_version(self):
        # Schema 1.1 (epic #618 / issue #619): readme now embeds inline content.
        # Bumped from "1.0". Consumers that read `readme["path"]` keep working;
        # consumers that did strict-equality on the whole readme dict (the
        # old 1-key {path} shape) will see a shape difference and must
        # branch on schema_version.
        self.assertEqual(self.summary["schema_version"], "1.1")

    def test_summary_totals_files_match_manifest(self):
        self.assertEqual(
            self.summary["totals"]["files"],
            len(self.manifest["files"]),
        )

    def test_summary_totals_bytes_is_sum_of_file_sizes(self):
        expected = sum(int(m["size"]) for m in self.manifest["files"].values())
        self.assertEqual(self.summary["totals"]["bytes"], expected)
        # bytes must include the annex file's 12345
        self.assertGreaterEqual(self.summary["totals"]["bytes"], 12345)

    def test_summary_paths_sorted_and_complete(self):
        self.assertEqual(self.summary["paths"], sorted(self.summary["paths"]))
        self.assertEqual(set(self.summary["paths"]), set(self.manifest["files"].keys()))

    def test_summary_subjects_derived_from_paths(self):
        self.assertEqual(self.summary["subjects"], ["sub-01", "sub-02"])
        self.assertEqual(self.summary["totals"]["subjects"], 2)

    def test_summary_modalities_detected(self):
        self.assertIn("eeg", self.summary["modalities"])

    def test_summary_readme_path(self):
        # Schema 1.1: readme is now a 5-key object, not the old 1-key {path}.
        self.assertEqual(self.summary["readme"]["path"], "README.md")

    def test_summary_readme_content_inline(self):
        """Schema 1.1: a small git-keyed README is embedded verbatim under
        readme.content as a UTF-8 string."""
        readme = self.summary["readme"]
        self.assertEqual(readme["content"], "# Test dataset\n\nA real test dataset.\n")
        self.assertFalse(readme["truncated"])

    def test_summary_readme_metadata(self):
        """content_bytes matches the raw blob size and sha256 is the
        hex-encoded SHA-256 of the same bytes."""
        import hashlib  # local to keep top-of-file imports unchanged

        readme = self.summary["readme"]
        expected_bytes = b"# Test dataset\n\nA real test dataset.\n"
        self.assertEqual(readme["content_bytes"], len(expected_bytes))
        self.assertEqual(readme["sha256"], hashlib.sha256(expected_bytes).hexdigest())

    def test_summary_doi_passthrough(self):
        self.assertEqual(self.summary["doi"], self.manifest["doi"])
        self.assertEqual(self.summary["concept_doi"], self.manifest["concept_doi"])

    def test_process_succeeded(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)


class MalformedSymlinkFallthroughTests(unittest.TestCase):
    """A symlink whose target is NOT a git-annex path must fall through to a
    ``git:<sha>`` entry instead of being silently dropped or mis-keyed.

    Production datasets occasionally contain symlinks to source code, docs,
    or other non-annex files (e.g. derivatives pointing back at the raw
    tree). Without explicit coverage a regression on parse_annex_key would
    silently corrupt those manifest entries.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-malformed-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"

        cls.repo.mkdir(parents=True, exist_ok=True)
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "test@nemar.local")
        git(cls.repo, "config", "user.name", "Test")
        git(cls.repo, "config", "commit.gpgsign", "false")

        (cls.repo / "dataset_description.json").write_text(
            json.dumps({"Name": "Bad", "BIDSVersion": "1.8.0", "DatasetType": "raw"}, indent=2)
        )
        (cls.repo / "README.md").write_text("# Bad symlink fixture\n")
        # The bad symlink: target is a relative path, NOT an annex object.
        bad_link = cls.repo / "shortcut.md"
        os.symlink("../README.md", bad_link)

        git(cls.repo, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        subprocess.check_call(
            ["git", "-C", str(cls.repo), "commit", "-q", "-m", "Bad symlink fixture"],
            env=env,
        )
        git(cls.repo, "tag", "v0.0.0")
        cls.proc = run_emit(cls.repo, cls.out)
        cls.manifest = json.loads((cls.out / "manifest.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_malformed_symlink_fallthrough(self):
        meta = self.manifest["files"].get("shortcut.md")
        self.assertIsNotNone(meta, "non-annex symlink missing from manifest")
        self.assertTrue(
            meta["key"].startswith("git:"),
            f"non-annex symlink should fall through to git:<sha>, got key={meta['key']}",
        )
        # Size is whatever git stored for the symlink blob (the target
        # string length), strictly positive.
        self.assertGreater(meta["size"], 0)


class ReadmeNoneTests(unittest.TestCase):
    """A dataset without a README at the BIDS root must yield
    ``summary["readme"] is None``. Contract: epic state doc, summary shape."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-no-readme-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"

        cls.repo.mkdir(parents=True, exist_ok=True)
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "test@nemar.local")
        git(cls.repo, "config", "user.name", "Test")
        git(cls.repo, "config", "commit.gpgsign", "false")
        (cls.repo / "dataset_description.json").write_text(
            json.dumps({"Name": "NoReadme", "BIDSVersion": "1.8.0", "DatasetType": "raw"})
        )
        # Deliberately no README, README.md, README.txt.
        git(cls.repo, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        subprocess.check_call(
            ["git", "-C", str(cls.repo), "commit", "-q", "-m", "No README fixture"],
            env=env,
        )
        git(cls.repo, "tag", "v0.0.0")
        cls.proc = run_emit(cls.repo, cls.out)
        cls.summary = json.loads((cls.out / "summary.json").read_text())
        cls.manifest = json.loads((cls.out / "manifest.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_readme_none(self):
        self.assertIsNone(self.summary["readme"])

    def test_summary_passthrough_fields(self):
        # dataset_id and version are mandatory pass-through fields per the
        # documented summary contract. The base test class asserts DOI
        # passthrough; this one asserts the simpler identity fields.
        self.assertEqual(self.summary["dataset_id"], "nm099999")
        self.assertEqual(self.summary["version"], "0.0.0")
        self.assertEqual(self.summary["dataset_id"], self.manifest["dataset_id"])
        self.assertEqual(self.summary["version"], self.manifest["version"])


class ReadmePriorityTests(unittest.TestCase):
    """When multiple README files exist at the BIDS root the priority order
    is ``README``, then ``README.md``, then ``README.txt``. The first match
    wins; later candidates are ignored even if also present.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-readme-prio-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"

        cls.repo.mkdir(parents=True, exist_ok=True)
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "test@nemar.local")
        git(cls.repo, "config", "user.name", "Test")
        git(cls.repo, "config", "commit.gpgsign", "false")
        (cls.repo / "dataset_description.json").write_text(
            json.dumps({"Name": "Prio", "BIDSVersion": "1.8.0", "DatasetType": "raw"})
        )
        # Commit BOTH README and README.md. README must win because it is
        # first in the candidates tuple.
        (cls.repo / "README").write_text("plain README\n")
        (cls.repo / "README.md").write_text("# markdown README\n")
        git(cls.repo, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        subprocess.check_call(
            ["git", "-C", str(cls.repo), "commit", "-q", "-m", "Two READMEs fixture"],
            env=env,
        )
        git(cls.repo, "tag", "v0.0.0")
        cls.proc = run_emit(cls.repo, cls.out)
        cls.summary = json.loads((cls.out / "summary.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_readme_priority_picks_plain_README(self):
        # README (priority 0) wins over README.md (priority 1). Content
        # embedded inline must come from README, not README.md, proving the
        # selection happens before content extraction.
        self.assertEqual(self.summary["readme"]["path"], "README")
        self.assertEqual(self.summary["readme"]["content"], "plain README\n")
        self.assertFalse(self.summary["readme"]["truncated"])


class VersionVPrefixTests(unittest.TestCase):
    """A caller may pass ``--version v0.0.0`` or ``--version 0.0.0``. Either
    form must produce a manifest/summary with the bare ``"0.0.0"`` value.

    The leading-v normalisation lives in emit_manifest.build_manifest()
    (``bare_version = version.lstrip('v')``); a regression there would
    break the documented VersionManifest shape and downstream S3 keys.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-v-prefix-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"

        cls.repo.mkdir(parents=True, exist_ok=True)
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "test@nemar.local")
        git(cls.repo, "config", "user.name", "Test")
        git(cls.repo, "config", "commit.gpgsign", "false")
        (cls.repo / "dataset_description.json").write_text(
            json.dumps({"Name": "VPrefix", "BIDSVersion": "1.8.0", "DatasetType": "raw"})
        )
        (cls.repo / "README.md").write_text("# v-prefix fixture\n")
        git(cls.repo, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        subprocess.check_call(
            ["git", "-C", str(cls.repo), "commit", "-q", "-m", "v-prefix fixture"],
            env=env,
        )
        git(cls.repo, "tag", "v0.0.0")

        # Run emit_manifest with --version v0.0.0 (leading v).
        cls.proc = subprocess.run(
            [
                sys.executable,
                str(EMIT),
                "--dataset-id",
                "nm099999",
                "--version",
                "v0.0.0",
                "--doi",
                "10.82901/nemar.nm099999.v0.0.0",
                "--concept-doi",
                "10.82901/nemar.nm099999",
                "--repo-dir",
                str(cls.repo),
                "--out-dir",
                str(cls.out),
                "--no-verify-canary",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.manifest = json.loads((cls.out / "manifest.json").read_text())
        cls.summary = json.loads((cls.out / "summary.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_manifest_version_stripped(self):
        self.assertEqual(self.manifest["version"], "0.0.0")

    def test_summary_version_stripped(self):
        self.assertEqual(self.summary["version"], "0.0.0")


class DeriveSubjectsLengthGuardTests(unittest.TestCase):
    """Direct unit test for the ``len(head) > 4`` guard in
    ``emit_manifest.derive_subjects``. A path like ``sub-/eeg/file.json``
    has the prefix ``sub-`` but no actual subject identifier; without the
    guard such a path would be silently emitted as the empty subject ``""``.
    """

    def setUp(self) -> None:
        # Import lazily so the import doesn't run for tests that exercise
        # the script as a subprocess.
        sys.path.insert(0, str(HERE))
        import emit_manifest  # noqa: E402  # pyright: ignore[reportMissingImports]

        self.derive_subjects = emit_manifest.derive_subjects

    def test_bare_sub_prefix_rejected(self):
        self.assertEqual(self.derive_subjects(["sub-/eeg/file.json"]), [])

    def test_valid_sub_accepted(self):
        self.assertEqual(
            self.derive_subjects(["sub-01/eeg/file.json", "sub-02/meg/file.json"]),
            ["sub-01", "sub-02"],
        )

    def test_mixed_rejects_bare_keeps_valid(self):
        self.assertEqual(
            self.derive_subjects(
                ["sub-/eeg/file.json", "sub-01/eeg/file.json", "sub-/meg/x.json"]
            ),
            ["sub-01"],
        )


class CallbackBodyRoundTripTests(unittest.TestCase):
    """Pin the cross-phase contract between scripts/build_callback_body.py
    and the Worker's validateManifestCallbackBody (in
    backend/src/routes/webhooks.ts). If either side's expected field set
    drifts, the callback POST 400s in production -- this test catches the
    drift in CI before merge.

    The corresponding Worker validator requires, for /webhooks/manifest-ready:
        {dataset_id, version, manifest_url, summary_url, totals, workflow_run_id}
    plus optional canary_skipped. The shape is documented in
    .context/epic_central_manifest_state.md (callback contract).
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-cb-")
        cls.tmp = Path(cls._tmp.name)
        cls.totals_path = cls.tmp / "totals.json"
        cls.totals_path.write_text(
            json.dumps({"files": 42, "bytes": 9999, "annex": 7, "git": 35})
        )
        cls.out = cls.tmp / "cb.json"
        cls.proc = subprocess.run(
            [
                sys.executable,
                str(HERE / "build_callback_body.py"),
                "--dataset-id",
                "nm099999",
                "--version",
                "1.0.0",
                "--manifest-url",
                "https://nemar.s3.us-east-2.amazonaws.com/nm099999/version/v1.0.0.json",
                "--summary-url",
                "https://nemar.s3.us-east-2.amazonaws.com/nm099999/version/v1.0.0-summary.json",
                "--totals-path",
                str(cls.totals_path),
                "--workflow-run-id",
                "424242",
                "--canary-skipped",
                "true",
                "--out",
                str(cls.out),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.body = json.loads(cls.out.read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_required_fields_present(self):
        # These six are the exact required fields enforced by
        # validateManifestCallbackBody in /webhooks/manifest-ready.
        required = {
            "dataset_id",
            "version",
            "manifest_url",
            "summary_url",
            "totals",
            "workflow_run_id",
        }
        self.assertTrue(
            required.issubset(set(self.body.keys())),
            f"missing required: {required - set(self.body.keys())}",
        )

    def test_no_unexpected_fields(self):
        # The full documented field set; canary_skipped is optional but
        # produced by this script. Any new field here MUST also be added
        # to backend/src/routes/webhooks.ts ManifestCallbackBody interface.
        allowed = {
            "dataset_id",
            "version",
            "manifest_url",
            "summary_url",
            "totals",
            "workflow_run_id",
            "canary_skipped",
        }
        extras = set(self.body.keys()) - allowed
        self.assertFalse(extras, f"unexpected fields produced: {extras}")

    def test_totals_shape(self):
        totals = self.body["totals"]
        self.assertEqual(set(totals.keys()), {"files", "bytes", "annex", "git"})
        self.assertEqual(totals["files"], 42)
        self.assertEqual(totals["annex"], 7)

    def test_canary_skipped_boolean(self):
        # The Python script accepts "true"/"false" string and emits an
        # actual JSON boolean. Worker handler reads it as boolean.
        self.assertIs(self.body["canary_skipped"], True)


class FailureBodyRoundTripTests(unittest.TestCase):
    """Pin the cross-phase contract for /webhooks/manifest-failed.

    The Worker handler reads body.error_message and writes it to
    manifest_jobs.error_message. Prior to the rename, this script emitted
    `failed_step` instead, so every failure row recorded "unknown error"
    regardless of which step died. This test ensures the rename stays.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-fail-cb-")
        cls.tmp = Path(cls._tmp.name)
        cls.out = cls.tmp / "fail.json"
        cls.proc = subprocess.run(
            [
                sys.executable,
                str(HERE / "build_failure_body.py"),
                "--dataset-id",
                "nm099999",
                "--version",
                "1.0.0",
                "--workflow-run-id",
                "424242",
                "--workflow-run-url",
                "https://github.com/nemarOrg/nemar-cli/actions/runs/424242",
                "--failed-step",
                "clone",
                "--out",
                str(cls.out),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.body = json.loads(cls.out.read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_required_fields_present(self):
        # /webhooks/manifest-failed requires only dataset_id+version per
        # the validator, but the script ships the full diagnostic set so
        # the DB row records workflow_run_id, run url, and the failed step.
        required = {
            "dataset_id",
            "version",
            "workflow_run_id",
            "workflow_run_url",
            "error_message",
        }
        self.assertTrue(
            required.issubset(set(self.body.keys())),
            f"missing required: {required - set(self.body.keys())}",
        )

    def test_no_unexpected_fields(self):
        # The Worker's ManifestCallbackBody interface defines the
        # full closed set; failure callback uses a subset. Drift here =
        # 400 in production on the very first failure.
        allowed = {
            "dataset_id",
            "version",
            "workflow_run_id",
            "workflow_run_url",
            "error_message",
        }
        extras = set(self.body.keys()) - allowed
        self.assertFalse(extras, f"unexpected fields produced: {extras}")

    def test_failed_step_renamed_to_error_message(self):
        # Regression guard: the old `failed_step` key broke the handler
        # because the validator never reads it. Make sure it does not
        # leak back into the output.
        self.assertNotIn("failed_step", self.body)

    def test_error_message_carries_failed_step(self):
        # The renamed field embeds the failed step so DB rows record the
        # actual point of failure, not the unhelpful "unknown error"
        # sentinel.
        self.assertEqual(self.body["error_message"], "failed at step: clone")


class ReadmeOversizeTruncatedTests(unittest.TestCase):
    """A README larger than README_INLINE_MAX_BYTES (256 KB) must NOT be
    embedded inline. The summary still records path + content_bytes so the
    website can render a length indicator, and sets truncated=True so the
    consumer falls back to the on-demand fetch."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-readme-oversize-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"

        cls.repo.mkdir(parents=True, exist_ok=True)
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "test@nemar.local")
        git(cls.repo, "config", "user.name", "Test")
        git(cls.repo, "config", "commit.gpgsign", "false")
        (cls.repo / "dataset_description.json").write_text(
            json.dumps({"Name": "BigReadme", "BIDSVersion": "1.8.0", "DatasetType": "raw"})
        )
        # 300 KB README — over the 256 KB cap. Mix multibyte characters so a
        # future refactor that confuses `len(str)` (codepoints) with
        # `len(bytes)` would visibly diverge from `content_bytes`.
        big = "# Big README ✓\n" + ("αβγδε" * (60 * 1024))
        (cls.repo / "README.md").write_text(big, encoding="utf-8")
        cls.big_bytes = len(big.encode("utf-8"))

        git(cls.repo, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        subprocess.check_call(
            ["git", "-C", str(cls.repo), "commit", "-q", "-m", "Oversize README fixture"],
            env=env,
        )
        git(cls.repo, "tag", "v0.0.0")
        cls.proc = run_emit(cls.repo, cls.out)
        cls.summary = json.loads((cls.out / "summary.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_oversize_readme_marked_truncated(self):
        readme = self.summary["readme"]
        self.assertEqual(readme["path"], "README.md")
        self.assertIsNone(readme["content"])
        self.assertTrue(readme["truncated"])
        self.assertEqual(readme["content_bytes"], self.big_bytes)
        # sha256 left null when content was never read (cap hit before cat-file).
        self.assertIsNone(readme["sha256"])

    def test_oversize_run_still_succeeded(self):
        # The oversize-README path must NOT fail the workflow; it degrades
        # gracefully so a giant README doesn't block a publish.
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)


class ReadmeAnnexedTruncatedTests(unittest.TestCase):
    """An annexed README (rare in BIDS but possible) must ship truncated=True
    with content=null. We don't have IAM here to dereference annex keys to S3,
    so the website must fall back to its own fetch path."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-readme-annex-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"

        cls.repo.mkdir(parents=True, exist_ok=True)
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "test@nemar.local")
        git(cls.repo, "config", "user.name", "Test")
        git(cls.repo, "config", "commit.gpgsign", "false")
        (cls.repo / "dataset_description.json").write_text(
            json.dumps({"Name": "AnnexReadme", "BIDSVersion": "1.8.0", "DatasetType": "raw"})
        )
        # Make README.md a real annex-style symlink instead of plain blob.
        annex_key = "SHA256E-s4096--deadbeefdeadbeef.md"
        annex_target = f"../../.git/annex/objects/aa/bb/{annex_key}/{annex_key}"
        os.symlink(annex_target, cls.repo / "README.md")
        cls.expected_size = 4096  # encoded in the annex key

        git(cls.repo, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        subprocess.check_call(
            ["git", "-C", str(cls.repo), "commit", "-q", "-m", "Annex README fixture"],
            env=env,
        )
        git(cls.repo, "tag", "v0.0.0")
        cls.proc = run_emit(cls.repo, cls.out)
        cls.summary = json.loads((cls.out / "summary.json").read_text())
        cls.manifest = json.loads((cls.out / "manifest.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_readme_is_annex_keyed_in_manifest(self):
        # Sanity check the fixture: the README should be annex-keyed, not
        # git-keyed. If this fails the test below is meaningless.
        key = self.manifest["files"]["README.md"]["key"]
        self.assertFalse(key.startswith("git:"), f"expected annex key, got {key!r}")

    def test_annexed_readme_truncated(self):
        readme = self.summary["readme"]
        self.assertEqual(readme["path"], "README.md")
        self.assertIsNone(readme["content"])
        self.assertTrue(readme["truncated"])
        self.assertEqual(readme["content_bytes"], self.expected_size)
        self.assertIsNone(readme["sha256"])


class ReadmeBinaryTruncatedTests(unittest.TestCase):
    """A non-UTF-8 README blob (binary or mis-encoded) must ship
    truncated=True with content=None, but **keep sha256 populated** because
    the bytes were actually read. This is the only path where sha256 is
    set while content is None — pin the unique 4-tuple so a refactor that
    swaps the decode/hash order would break visibly."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-readme-binary-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"

        cls.repo.mkdir(parents=True, exist_ok=True)
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "test@nemar.local")
        git(cls.repo, "config", "user.name", "Test")
        git(cls.repo, "config", "commit.gpgsign", "false")
        (cls.repo / "dataset_description.json").write_text(
            json.dumps({"Name": "BinReadme", "BIDSVersion": "1.8.0", "DatasetType": "raw"})
        )
        # Bytes that are not valid UTF-8: lone continuation byte, then
        # incomplete multibyte start. write_bytes since the BIDS spec
        # requires UTF-8 README — this fixture deliberately breaks the spec
        # to exercise the encoding fallback.
        #
        # NOTE: the payload must be > 512 bytes so build_manifest's
        # unlocked-mode annex-pointer probe (which calls `cat-file blob`
        # in text mode) skips it. The probe would otherwise raise
        # UnicodeDecodeError before our derive_readme code is reached.
        # That's a pre-existing latent bug tracked separately; sidestep
        # here by oversizing the fixture.
        import hashlib
        cls.binary_payload = b"\xff\xfe\x00\x01binary garbage \xc3\x28 not utf-8 " * 32
        (cls.repo / "README.md").write_bytes(cls.binary_payload)
        cls.expected_sha = hashlib.sha256(cls.binary_payload).hexdigest()

        git(cls.repo, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        subprocess.check_call(
            ["git", "-C", str(cls.repo), "commit", "-q", "-m", "Binary README fixture"],
            env=env,
        )
        git(cls.repo, "tag", "v0.0.0")
        cls.proc = run_emit(cls.repo, cls.out)
        cls.summary = json.loads((cls.out / "summary.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_binary_readme_truncated_with_sha(self):
        readme = self.summary["readme"]
        self.assertEqual(readme["path"], "README.md")
        self.assertIsNone(readme["content"])
        self.assertTrue(readme["truncated"])
        self.assertEqual(readme["content_bytes"], len(self.binary_payload))
        # sha256 IS set (bytes were read) — distinguishes this branch from
        # oversize/annexed/cat-file-failure where sha256 is None.
        self.assertEqual(readme["sha256"], self.expected_sha)

    def test_binary_run_still_succeeded(self):
        # Spec-violating READMEs warn (see stderr) but don't fail the publish.
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)

    def test_binary_emits_non_utf8_warning(self):
        # Structured stderr line so ops can grep for it post-publish.
        self.assertIn("readme_truncated", self.proc.stderr)
        self.assertIn("reason=non_utf8", self.proc.stderr)


class ReadmeCatFileFailureTests(unittest.TestCase):
    """If `git cat-file blob <sha>` fails for the README (corruption,
    stale manifest, wrong repo_dir) we must NOT crash the publish. The
    summary records the path with `content=None, content_bytes=None,
    sha256=None, truncated=True` so the website knows to fall back.

    Reproduced by mutating an existing summary's expected behaviour via
    a fixture where we substitute a nonexistent git sha into the manifest
    before re-running `build_summary` directly. Because the script as a
    whole derives the manifest from a real tree (and a real tree can't
    contain a nonexistent sha), we exercise this via a direct unit call
    rather than the subprocess pathway."""

    def setUp(self) -> None:
        sys.path.insert(0, str(HERE))
        import emit_manifest  # noqa: E402  # pyright: ignore[reportMissingImports]

        self.derive_readme = emit_manifest.derive_readme

    def test_cat_file_failure_marks_truncated(self):
        # Real but empty git repo so cat-file invocations have a working
        # GIT_DIR but the bogus sha doesn't resolve.
        with tempfile.TemporaryDirectory(prefix="emit-cat-fail-") as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.check_call(["git", "-C", str(repo), "init", "-q", "-b", "main"])

            bogus_sha = "0" * 40
            files = {
                "README.md": {
                    "key": f"git:{bogus_sha}",
                    "size": 1234,
                    "checksum": f"git:{bogus_sha}",
                }
            }
            paths = {"README.md"}

            result = self.derive_readme(
                paths=paths,
                files=files,
                repo_dir=str(repo),
                dataset_id="nm099999",
                version="0.0.0",
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["path"], "README.md")
            self.assertIsNone(result["content"])
            self.assertTrue(result["truncated"])
            # content_bytes=None signals "we never opened the blob"; the
            # 1234 from the manifest is NOT echoed back, because it would
            # be misleading (no bytes were ever read).
            self.assertIsNone(result["content_bytes"])
            self.assertIsNone(result["sha256"])


class ReadmeNoneSchema11Tests(unittest.TestCase):
    """When no README exists at the BIDS root, schema 1.1 still emits
    `readme: null` (not an empty dict). Forward-compatible with 1.0
    consumers that only checked `readme is None` for absence."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="emit-manifest-no-readme-11-")
        cls.tmp = Path(cls._tmp.name)
        cls.repo = cls.tmp / "repo"
        cls.out = cls.tmp / "out"

        cls.repo.mkdir(parents=True, exist_ok=True)
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "test@nemar.local")
        git(cls.repo, "config", "user.name", "Test")
        git(cls.repo, "config", "commit.gpgsign", "false")
        (cls.repo / "dataset_description.json").write_text(
            json.dumps({"Name": "NoReadme11", "BIDSVersion": "1.8.0", "DatasetType": "raw"})
        )
        git(cls.repo, "add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        subprocess.check_call(
            ["git", "-C", str(cls.repo), "commit", "-q", "-m", "No README schema-1.1 fixture"],
            env=env,
        )
        git(cls.repo, "tag", "v0.0.0")
        cls.proc = run_emit(cls.repo, cls.out)
        cls.summary = json.loads((cls.out / "summary.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_readme_is_null(self):
        self.assertIsNone(self.summary["readme"])

    def test_schema_version_still_11(self):
        self.assertEqual(self.summary["schema_version"], "1.1")


class BytesUrlForUnitTests(unittest.TestCase):
    """Pure-function coverage of bytes_url_for, incl. per-segment URL-encoding
    (parity with nemar-cli backend/src/services/data-router.ts buildBytesUrl)."""

    def setUp(self):
        sys.path.insert(0, str(HERE))
        import emit_manifest  # noqa: E402  # pyright: ignore[reportMissingImports]

        self.fn = emit_manifest.bytes_url_for

    def test_git_keyed_uses_raw_github(self):
        self.assertEqual(
            self.fn("nm099999", "v1.0.0", "dataset_description.json", "git:abc123"),
            "https://raw.githubusercontent.com/nemarDatasets/nm099999/v1.0.0/dataset_description.json",
        )

    def test_annex_keyed_uses_data_nemar(self):
        self.assertEqual(
            self.fn("nm099999", "v1.0.0", "sub-01/eeg/sub-01_eeg.edf", "SHA256E-s10--ab.edf"),
            "https://data.nemar.org/nm099999/v1.0.0/sub-01/eeg/sub-01_eeg.edf",
        )

    def test_path_segments_url_encoded_slashes_preserved(self):
        self.assertEqual(
            self.fn("nm099999", "v1.0.0", "sub-01/eeg/sub-01_task-rest events.tsv", "git:zzz"),
            "https://raw.githubusercontent.com/nemarDatasets/nm099999/v1.0.0/sub-01/eeg/sub-01_task-rest%20events.tsv",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
