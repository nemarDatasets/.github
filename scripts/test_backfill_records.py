"""Tests for scripts/backfill_records.py — the pure selection + planning logic.

No mocks: real /datasets-shaped rows and an injected versions_lookup. The network
(fetch_public_datasets / versions_for) and the gh dispatch are I/O, exercised
manually / in the one-time backfill run, not here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _import():
    sys.path.insert(0, str(HERE))
    import backfill_records

    return backfill_records


# Real GET /datasets row shape (field names from the live API).
ROWS = [
    {"dataset_id": "nm000132", "latest_version": "1.1.1", "visibility": "public"},
    {"dataset_id": "on007052", "latest_version": "v1.0.0", "visibility": "public"},  # leading v stripped
    {"dataset_id": "nm000999", "latest_version": "1.0.0", "visibility": "private"},  # private -> skip
    {"dataset_id": "xx000001", "latest_version": "1.0.0", "visibility": "public"},  # sandbox -> skip
    {"dataset_id": "nm099999", "latest_version": "1.0.0", "visibility": "public"},  # default-excluded test
    {"dataset_id": "nm000000", "latest_version": "", "visibility": "public"},  # no version -> skip
]


class SelectDatasetsTests(unittest.TestCase):
    def setUp(self):
        self.m = _import()

    def test_public_nm_on_only_excludes_private_sandbox_test_versionless(self):
        got = self.m.select_datasets(ROWS, exclude={"nm099999"})
        self.assertEqual(got, [("nm000132", "1.1.1"), ("on007052", "1.0.0")])

    def test_exclude_set_is_honored(self):
        got = self.m.select_datasets(ROWS, exclude={"nm099999", "nm000132"})
        self.assertEqual(got, [("on007052", "1.0.0")])

    def test_result_is_sorted(self):
        rows = [
            {"dataset_id": "on009", "latest_version": "1.0.0", "visibility": "public"},
            {"dataset_id": "nm001", "latest_version": "1.0.0", "visibility": "public"},
        ]
        self.assertEqual(
            self.m.select_datasets(rows, exclude=set()),
            [("nm001", "1.0.0"), ("on009", "1.0.0")],
        )


class BuildPlanTests(unittest.TestCase):
    def setUp(self):
        self.m = _import()

    def test_latest_only_does_not_call_versions_lookup(self):
        def boom(_ds):
            raise AssertionError("versions_lookup must not run in latest-only mode")

        plan = self.m.build_plan(
            [("nm000132", "1.1.1"), ("on007052", "1.0.0")],
            all_versions=False,
            versions_lookup=boom,
        )
        self.assertEqual(plan, [("nm000132", "1.1.1"), ("on007052", "1.0.0")])

    def test_all_versions_expands_via_lookup(self):
        vmap = {"nm000132": ["1.0.0", "1.1.0", "1.1.1"], "on007052": ["1.0.0"]}
        plan = self.m.build_plan(
            [("nm000132", "1.1.1"), ("on007052", "1.0.0")],
            all_versions=True,
            versions_lookup=lambda ds: vmap[ds],
        )
        self.assertEqual(
            plan,
            [
                ("nm000132", "1.0.0"),
                ("nm000132", "1.1.0"),
                ("nm000132", "1.1.1"),
                ("on007052", "1.0.0"),
            ],
        )

    def test_latest_only_skips_versionless_dataset(self):
        plan = self.m.build_plan([("nm000132", "")], all_versions=False, versions_lookup=lambda ds: [])
        self.assertEqual(plan, [])


if __name__ == "__main__":
    unittest.main()
