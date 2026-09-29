#!/usr/bin/env python3
"""
Real-process integration test for scripts/archive-cleanup-plan.sh
(#1518, PR review round 2).

No mocks. Every test invokes the real script as a subprocess against a
real synthetic `aws s3api list-object-versions --output json` document (or,
for the two AccessDenied-classification suites, a real
`aws s3api delete-objects --output json` document or plain error text).
Nothing calls AWS; the workflow step still makes every AWS call itself and
feeds this script's output back into its own delete-objects loop.

Fixture shapes mirror the reviewer's adversarial harness
(archive-cleanup-test/case*.json in the review scratchpad) -- each of that
harness's 11 cases has a corresponding test here, using the same key names
and version numbers so the two can be cross-checked by hand.

Run with either:
    python3 scripts/test_archive_cleanup_plan.py
    uv run python scripts/test_archive_cleanup_plan.py
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "archive-cleanup-plan.sh"


def run_plan(dataset_id: str, version: str, listing: dict) -> subprocess.CompletedProcess:
    """Invoke `archive-cleanup-plan.sh plan <listing>` against a real
    subprocess, with the listing dict written to a real temp file."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(listing, f)
        listing_path = f.name
    try:
        env = {"DATASET_ID": dataset_id, "VERSION": version, "PATH": "/usr/bin:/bin:/usr/local/bin"}
        return subprocess.run(
            ["bash", str(SCRIPT), "plan", listing_path],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        Path(listing_path).unlink(missing_ok=True)


def deleted_keys(proc: subprocess.CompletedProcess) -> list[tuple[str, str]]:
    """(Key, VersionId) pairs from a successful plan's stdout JSON array."""
    items = json.loads(proc.stdout)
    return [(i["Key"], i["VersionId"]) for i in items]


def entry(key: str, version_id: str, is_latest: bool, last_modified: str = "2026-01-01T00:00:00.000Z") -> dict:
    return {"Key": key, "VersionId": version_id, "IsLatest": is_latest, "LastModified": last_modified}


class OutOfOrderCompletionTests(unittest.TestCase):
    """concurrency is per (dataset_id, version): two versions of one
    dataset can finish in either order. Case 1 from the adversarial
    harness -- a newer version exists under BOTH the new and legacy key
    forms, and an older, out-of-order retry must not touch either."""

    def test_older_retry_after_newer_already_built_leaves_newer_untouched_both_key_forms(self):
        listing = {
            "Versions": [
                entry("nm000200/archives/nm000200_v1.1.0.zip", "V-new-110-cur", True, "2026-09-20T00:00:00.000Z"),
                entry("nm000200/archives/v1.1.0.zip", "V-legacy-110-cur", True, "2026-08-01T00:00:00.000Z"),
                entry("nm000200/archives/nm000200_v1.0.0.zip", "V-new-100-cur", True, "2026-09-28T00:00:00.000Z"),
            ],
            "DeleteMarkers": [],
        }
        proc = run_plan("nm000200", "1.0.0", listing)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(deleted_keys(proc), [])


class NumericVersionComparisonTests(unittest.TestCase):
    """1.10.0 vs 1.9.0 in both directions -- a lexical string comparison
    gets this backwards ("1.10.0" < "1.9.0" lexically); it must be numeric
    per component. Case 2a from the harness, run with both dispatched
    values against the same listing."""

    LISTING = {
        "Versions": [
            entry("nm000132/archives/nm000132_v1.9.0.zip", "V-19-cur", True, "2026-01-01T00:00:00.000Z"),
            entry("nm000132/archives/nm000132_v1.10.0.zip", "V-110-cur", True, "2026-02-01T00:00:00.000Z"),
        ],
        "DeleteMarkers": [],
    }

    def test_dispatched_1_10_0_deletes_the_lower_1_9_0(self):
        proc = run_plan("nm000132", "1.10.0", self.LISTING)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            deleted_keys(proc),
            [("nm000132/archives/nm000132_v1.9.0.zip", "V-19-cur")],
        )

    def test_dispatched_1_9_0_never_deletes_the_higher_1_10_0(self):
        # An out-of-order retry of the OLDER version must not touch the
        # real, higher-version latest.
        proc = run_plan("nm000132", "1.9.0", self.LISTING)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(deleted_keys(proc), [])


class PrereleaseAndBuildSuffixTests(unittest.TestCase):
    """A key with a semver pre-release/build suffix (valid semver, but not
    the bare X.Y.Z this bucket's archives use) must be reported and never
    compared or deleted. Case 3 from the harness."""

    def test_rc_and_build_suffixed_keys_are_reported_not_deleted(self):
        listing = {
            "Versions": [
                entry("nm000150/archives/nm000150_v1.0.0-rc.1.zip", "V-rc1", False, "2026-01-01T00:00:00.000Z"),
                entry("nm000150/archives/nm000150_v1.0.0+build5.zip", "V-build5", False, "2026-01-02T00:00:00.000Z"),
                entry("nm000150/archives/nm000150_v1.0.0.zip", "V-100-cur", True, "2026-09-28T00:00:00.000Z"),
            ],
            "DeleteMarkers": [],
        }
        proc = run_plan("nm000150", "1.0.0", listing)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(deleted_keys(proc), [])
        self.assertIn("nm000150_v1.0.0-rc.1.zip", proc.stderr)
        self.assertIn("nm000150_v1.0.0+build5.zip", proc.stderr)
        self.assertIn("::warning::", proc.stderr)


class DatasetIdContainingVTests(unittest.TestCase):
    """A dataset id that itself starts with (or contains) the letter "v"
    must not confuse new-vs-legacy key parsing. Case 4 from the harness."""

    def test_legacy_key_under_a_v_prefixed_dataset_id_still_parses_and_deletes(self):
        listing = {
            "Versions": [
                entry("onv00012/archives/v0.9.0.zip", "V-legacy-09", True, "2026-01-01T00:00:00.000Z"),
                entry("onv00012/archives/onv00012_v1.0.0.zip", "V-new-10-cur", True, "2026-09-28T00:00:00.000Z"),
            ],
            "DeleteMarkers": [],
        }
        proc = run_plan("onv00012", "1.0.0", listing)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            deleted_keys(proc),
            [("onv00012/archives/v0.9.0.zip", "V-legacy-09")],
        )


class DeleteMarkerTests(unittest.TestCase):
    """Case 5 from the harness: a noncurrent version and a stale delete
    marker of the DISPATCHED version are both deleted; a noncurrent
    version and a CURRENT delete marker of a HIGHER version both survive
    untouched."""

    def test_dispatched_version_noncurrent_and_marker_deleted_higher_version_untouched(self):
        listing = {
            "Versions": [
                entry("nm000300/archives/nm000300_v1.0.0.zip", "V-100-noncurrent-shadowed-by-marker", False, "2026-01-01T00:00:00.000Z"),
                entry("nm000300/archives/nm000300_v2.0.0.zip", "V-200-noncurrent", False, "2026-01-01T00:00:00.000Z"),
                entry("nm000300/archives/nm000300_v1.0.0.zip", "V-100-cur-fresh-upload", True, "2026-09-28T00:00:00.000Z"),
            ],
            "DeleteMarkers": [
                entry("nm000300/archives/nm000300_v1.0.0.zip", "V-100-marker-stale", False, "2026-06-01T00:00:00.000Z"),
                entry("nm000300/archives/nm000300_v2.0.0.zip", "V-200-marker-current", True, "2026-08-01T00:00:00.000Z"),
            ],
        }
        proc = run_plan("nm000300", "1.0.0", listing)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            sorted(deleted_keys(proc)),
            sorted(
                [
                    ("nm000300/archives/nm000300_v1.0.0.zip", "V-100-noncurrent-shadowed-by-marker"),
                    ("nm000300/archives/nm000300_v1.0.0.zip", "V-100-marker-stale"),
                ]
            ),
        )
        # The higher version's noncurrent object AND its current delete
        # marker must both survive -- deleting the marker would resurrect
        # the noncurrent v2.0.0 object as current, which is exactly wrong.
        deleted_version_ids = {vid for _, vid in deleted_keys(proc)}
        self.assertNotIn("V-200-noncurrent", deleted_version_ids)
        self.assertNotIn("V-200-marker-current", deleted_version_ids)


class NoncurrentHigherVersionSurvivesTests(unittest.TestCase):
    """Case 6 from the harness, as its own dedicated scenario: a noncurrent
    version of a key belonging to a HIGHER version must never be deleted,
    even though it isn't the current object."""

    def test_noncurrent_and_current_higher_version_both_survive(self):
        listing = {
            "Versions": [
                entry("nm000400/archives/nm000400_v2.0.0.zip", "V-200-noncurrent-older-reupload", False, "2026-01-01T00:00:00.000Z"),
                entry("nm000400/archives/nm000400_v2.0.0.zip", "V-200-current", True, "2026-05-01T00:00:00.000Z"),
                entry("nm000400/archives/nm000400_v1.0.0.zip", "V-100-current-fresh", True, "2026-09-28T00:00:00.000Z"),
            ],
            "DeleteMarkers": [],
        }
        proc = run_plan("nm000400", "1.0.0", listing)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(deleted_keys(proc), [])


class StrayFilesAndNestedPathsTests(unittest.TestCase):
    """Case 7 from the harness: a non-.zip file, a nested path under
    archives/, and a file that superficially starts with "v" but whose
    extracted "version" isn't valid semver must all be reported, never
    deleted or misclassified."""

    def test_non_zip_nested_path_and_v_prefixed_non_semver_name_all_reported(self):
        listing = {
            "Versions": [
                entry("nm000500/archives/README.txt", "V-readme", True, "2026-01-01T00:00:00.000Z"),
                entry("nm000500/archives/subdir/nm000500_v0.5.0.zip", "V-nested", True, "2026-01-01T00:00:00.000Z"),
                entry("nm000500/archives/video.zip", "V-video", True, "2026-01-01T00:00:00.000Z"),
                entry("nm000500/archives/nm000500_v1.0.0.zip", "V-100-cur", True, "2026-09-28T00:00:00.000Z"),
            ],
            "DeleteMarkers": [],
        }
        proc = run_plan("nm000500", "1.0.0", listing)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(deleted_keys(proc), [])
        for name in ("README.txt", "subdir/nm000500_v0.5.0.zip", "video.zip"):
            self.assertIn(name, proc.stderr)


class AbortGuardTests(unittest.TestCase):
    """Case 8 and case 9 from the harness: the plan must refuse to delete
    ANYTHING when it cannot find exactly one current, new-name object at
    the dispatched version -- an empty listing, or a listing where the
    "current" state of that key is actually a delete marker (the upload
    this run made isn't really current for some reason)."""

    def test_empty_listing_aborts_without_deleting(self):
        proc = run_plan("nm000900", "1.0.0", {"Versions": [], "DeleteMarkers": []})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("expected exactly 1 current object", proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_current_state_is_a_delete_marker_not_a_real_object_aborts(self):
        listing = {
            "Versions": [
                entry("nm000600/archives/nm000600_v1.0.0.zip", "V-100-noncurrent", False, "2026-09-28T00:00:00.000Z"),
            ],
            "DeleteMarkers": [
                entry("nm000600/archives/nm000600_v1.0.0.zip", "V-100-marker-current", True, "2026-09-28T00:01:00.000Z"),
            ],
        }
        proc = run_plan("nm000600", "1.0.0", listing)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("expected exactly 1 current object", proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")


class OutOfScopeGuardTests(unittest.TestCase):
    """Case 10 from the harness: a key outside <id>/archives/ anywhere in
    the listing refuses the whole plan rather than silently ignoring it."""

    def test_key_outside_prefix_refuses_the_plan(self):
        listing = {
            "Versions": [
                entry("nm000700/archives/nm000700_v1.0.0.zip", "V-100-cur", True, "2026-09-28T00:00:00.000Z"),
                entry("nm000700other/archives/sneaky.zip", "V-sneaky", True, "2026-09-28T00:00:00.000Z"),
            ],
            "DeleteMarkers": [],
        }
        proc = run_plan("nm000700", "1.0.0", listing)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("outside", proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")


class LegacyCopyOfDispatchedVersionTests(unittest.TestCase):
    """Case 11 from the harness: the legacy-named object for the EXACT
    dispatched version is deleted once the new-name object is confirmed
    current (rule (c))."""

    def test_legacy_copy_of_the_just_built_version_is_deleted(self):
        listing = {
            "Versions": [
                entry("nm000800/archives/v1.0.0.zip", "V-legacy-100", True, "2026-01-01T00:00:00.000Z"),
                entry("nm000800/archives/nm000800_v1.0.0.zip", "V-new-100-cur", True, "2026-09-28T00:00:00.000Z"),
            ],
            "DeleteMarkers": [],
        }
        proc = run_plan("nm000800", "1.0.0", listing)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            deleted_keys(proc),
            [("nm000800/archives/v1.0.0.zip", "V-legacy-100")],
        )


class RequiredEnvVarsTests(unittest.TestCase):
    def test_missing_dataset_id_fails(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"Versions": [], "DeleteMarkers": []}, f)
            listing_path = f.name
        try:
            proc = subprocess.run(
                ["bash", str(SCRIPT), "plan", listing_path],
                env={"VERSION": "1.0.0", "PATH": "/usr/bin:/bin:/usr/local/bin"},
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
        finally:
            Path(listing_path).unlink(missing_ok=True)


class AccessDeniedClassificationTests(unittest.TestCase):
    """The IAM-permissions robustness findings: distinguishing a real
    failure from a permission denial the lead hasn't applied the policy
    for yet, so the workflow can warn instead of failing the job."""

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_is_access_denied_matches_a_whole_call_denial(self):
        proc = self._run(
            ["is-access-denied"],
            stdin="An error occurred (AccessDenied) when calling the ListObjectVersions operation",
        )
        self.assertEqual(proc.returncode, 0)

    def test_is_access_denied_does_not_match_other_errors(self):
        proc = self._run(
            ["is-access-denied"],
            stdin="An error occurred (InternalError) when calling the ListObjectVersions operation",
        )
        self.assertEqual(proc.returncode, 1)

    def _delete_result_file(self, errors: list[dict]) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"Deleted": [], "Errors": errors}, f)
            return f.name

    def test_delete_errors_all_access_denied(self):
        path = self._delete_result_file(
            [
                {"Key": "a", "VersionId": "1", "Code": "AccessDenied"},
                {"Key": "b", "VersionId": "2", "Code": "AccessDenied"},
            ]
        )
        try:
            proc = self._run(["delete-errors-are-access-denied", path])
            self.assertEqual(proc.returncode, 0)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_delete_errors_mixed_is_not_all_access_denied(self):
        path = self._delete_result_file(
            [
                {"Key": "a", "VersionId": "1", "Code": "AccessDenied"},
                {"Key": "b", "VersionId": "2", "Code": "InternalError"},
            ]
        )
        try:
            proc = self._run(["delete-errors-are-access-denied", path])
            self.assertEqual(proc.returncode, 1)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_no_errors_is_not_access_denied(self):
        path = self._delete_result_file([])
        try:
            proc = self._run(["delete-errors-are-access-denied", path])
            self.assertEqual(proc.returncode, 1)
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
