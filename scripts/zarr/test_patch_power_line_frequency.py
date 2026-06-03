#!/usr/bin/env python3
"""Unit tests for the pure helpers in patch_power_line_frequency.py (no mocks:
plan_patches is exercised with a real resolver function, not a fake)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patch_power_line_frequency import (  # type: ignore[import-not-found]  # noqa: E402
    current_plf,
    plan_patches,
    set_plf,
    store_meta_key,
)


class TestStoreMetaKey(unittest.TestCase):
    def test_builds_root_zarr_json_key(self):
        self.assertEqual(
            store_meta_key("on007139", "sub-01/eeg/sub-01_task-rest_eeg.zarr"),
            "on007139/zarr/sub-01/eeg/sub-01_task-rest_eeg.zarr/zarr.json",
        )

    def test_strips_stray_slashes(self):
        self.assertEqual(store_meta_key("nm000132", "/a.zarr/"), "nm000132/zarr/a.zarr/zarr.json")


class TestCurrentPlf(unittest.TestCase):
    def test_reads_value(self):
        self.assertEqual(current_plf({"attributes": {"power_line_frequency": 60.0}}), 60.0)

    def test_none_when_unset_or_no_attributes(self):
        self.assertIsNone(current_plf({"attributes": {"format": "x"}}))
        self.assertIsNone(current_plf({}))
        self.assertIsNone(current_plf({"attributes": None}))


class TestSetPlf(unittest.TestCase):
    def test_adds_and_preserves(self):
        doc = {"attributes": {"format": "biosigio-zarr", "channel_groups": ["eeg_250hz"]}}
        set_plf(doc, 50.0)
        self.assertEqual(doc["attributes"]["power_line_frequency"], 50.0)
        self.assertEqual(doc["attributes"]["channel_groups"], ["eeg_250hz"])

    def test_creates_attributes_when_missing(self):
        doc: dict = {"zarr_format": 3}
        set_plf(doc, 60.0)
        self.assertEqual(doc["attributes"]["power_line_frequency"], 60.0)


class TestPlanPatches(unittest.TestCase):
    def test_resolves_and_filters(self):
        stores = [
            {"path": "sub-01/eeg/sub-01_eeg.set", "zarr": "sub-01/eeg/sub-01_eeg.zarr"},
            {"path": "sub-02/eeg/sub-02_eeg.set", "zarr": "sub-02/eeg/sub-02_eeg.zarr"},
            {"zarr": "broken.zarr"},  # no path -> skipped
        ]
        table = {"sub-01/eeg/sub-01_eeg.set": 60.0}  # sub-02 declares none

        def resolve(path: str):
            return table.get(path)

        out = plan_patches(stores, resolve)
        self.assertEqual(out, [("sub-01/eeg/sub-01_eeg.zarr", "sub-01/eeg/sub-01_eeg.set", 60.0)])


if __name__ == "__main__":
    unittest.main()
