#!/usr/bin/env python3
"""Unit tests for the pure helpers in scripts/zarr/purge_non_raw_stores.py.

No mocks: these exercise candidate selection, the escape-the-prefix guard,
index rewriting, and S3-listing-output parsing directly, over realistic
`index.json` documents and path/prefix strings -- no business logic (S3
calls, subprocess, network) is patched or faked anywhere in this file.

The S3 list/delete/read/write orchestration in `purge_dataset` and its I/O
helpers (`stat_prefix`, `discover_excluded_stores`, `write_index`,
`list_dataset_ids`) genuinely require a real S3 client and are deliberately
NOT exercised here -- see the PR description for what that leaves unverified.

Run with:
    python3 scripts/zarr/test_purge_non_raw_stores.py
    uv run python scripts/zarr/test_purge_non_raw_stores.py
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from purge_non_raw_stores import (  # type: ignore[import-not-found]
    assert_within_zarr_prefix,
    parse_s3_ls_summary,
    prepare_targets,
    rewrite_index,
    select_purge_candidates,
)

BUCKET = "nemar"
DATASET = "nm000123"


def _store(path: str, **extra) -> dict:
    root, _, _ext = path.rpartition(".")
    entry = {"path": path, "zarr": f"{root}.zarr", "source_key": path, "updated_utc": "2026-01-01T00:00:00Z"}
    entry.update(extra)
    return entry


def _failure(path: str, zarr: str, code: str = "corrupt_or_truncated") -> dict:
    return {"path": path, "zarr": zarr, "code": code, "reason": "corrupt or truncated file"}


class SelectPurgeCandidatesTests(unittest.TestCase):
    def test_only_excluded_tree_stores_selected(self):
        index = {
            "stores": [
                _store("derivatives/pipeline-x/sub-01/eeg/sub-01_task-x_eeg.set"),
                _store("sourcedata/raw-vendor/sub-02/eeg/sub-02_task-y_eeg.edf"),
                _store("code/analysis/helper_eeg.set"),
                _store("sub-03/eeg/sub-03_task-z_eeg.set"),
            ]
        }
        candidates, anomalies = select_purge_candidates(index)
        self.assertEqual(anomalies, [])
        selected_paths = {c["path"] for c in candidates}
        self.assertEqual(
            selected_paths,
            {
                "derivatives/pipeline-x/sub-01/eeg/sub-01_task-x_eeg.set",
                "sourcedata/raw-vendor/sub-02/eeg/sub-02_task-y_eeg.edf",
                "code/analysis/helper_eeg.set",
            },
        )

    def test_sub_star_raw_store_is_never_selected(self):
        index = {"stores": [_store("sub-01/eeg/sub-01_task-rest_eeg.set")]}
        candidates, anomalies = select_purge_candidates(index)
        self.assertEqual(candidates, [])
        self.assertEqual(anomalies, [])

    def test_many_raw_stores_none_selected(self):
        index = {
            "stores": [
                _store(f"sub-{i:02d}/eeg/sub-{i:02d}_task-rest_eeg.set") for i in range(1, 21)
            ]
        }
        candidates, anomalies = select_purge_candidates(index)
        self.assertEqual(candidates, [])
        self.assertEqual(anomalies, [])

    def test_nested_derivatives_under_a_subject_is_selected(self):
        index = {"stores": [_store("sub-01/derivatives/denoised/sub-01_task-x_eeg.set")]}
        candidates, _ = select_purge_candidates(index)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["path"], "sub-01/derivatives/denoised/sub-01_task-x_eeg.set"
        )

    def test_segment_boundary_negatives_not_selected(self):
        # "code", "derivatives", "sourcedata" must match a full path segment,
        # not a bare substring of a longer directory/task name.
        index = {
            "stores": [
                _store("mycode/sub-01/eeg/sub-01_task-x_eeg.set"),
                _store("derivatives_old/sub-01/eeg/sub-01_task-x_eeg.set"),
                _store("sourcedatafoo/sub-01/eeg/sub-01_task-x_eeg.set"),
                _store("sub-01/eeg/sub-01_task-decode_eeg.set"),
            ]
        }
        candidates, anomalies = select_purge_candidates(index)
        self.assertEqual(candidates, [])
        self.assertEqual(anomalies, [])

    def test_path_zarr_disagreement_is_an_anomaly_not_a_candidate(self):
        # A raw-looking `path` paired with an excluded-looking `zarr` is
        # exactly the shape a corrupted/hand-edited index entry would have.
        entry = {
            "path": "sub-01/eeg/sub-01_task-x_eeg.set",
            "zarr": "derivatives/evil/sub-01_task-x_eeg.zarr",
        }
        candidates, anomalies = select_purge_candidates({"stores": [entry]})
        self.assertEqual(candidates, [])
        self.assertEqual(len(anomalies), 1)
        self.assertIs(anomalies[0]["entry"], entry)

    def test_missing_zarr_field_is_an_anomaly(self):
        entry = {"path": "derivatives/x/sub-01_task-x_eeg.set"}
        candidates, anomalies = select_purge_candidates({"stores": [entry]})
        self.assertEqual(candidates, [])
        self.assertEqual(len(anomalies), 1)

    def test_non_dict_store_entry_is_an_anomaly_not_a_crash(self):
        candidates, anomalies = select_purge_candidates({"stores": ["not-a-dict", 42, None]})
        self.assertEqual(candidates, [])
        self.assertEqual(len(anomalies), 3)

    def test_missing_or_non_list_stores_key_is_empty_not_a_crash(self):
        self.assertEqual(select_purge_candidates({}), ([], []))
        self.assertEqual(select_purge_candidates({"stores": None}), ([], []))
        self.assertEqual(select_purge_candidates({"stores": "oops"}), ([], []))

    def test_selection_does_not_mutate_its_input(self):
        index = {
            "stores": [
                _store("derivatives/pipeline-x/sub-01/eeg/sub-01_task-x_eeg.set"),
                _store("sub-02/eeg/sub-02_task-y_eeg.set"),
            ]
        }
        before = copy.deepcopy(index)
        select_purge_candidates(index)
        self.assertEqual(index, before)


class PrepareTargetsTests(unittest.TestCase):
    def test_ordinary_candidate_gets_a_correct_key_prefix(self):
        entry = _store("derivatives/pipeline-x/sub-01/eeg/sub-01_task-x_eeg.set")
        targets, rejected = prepare_targets(BUCKET, DATASET, [entry])
        self.assertEqual(rejected, [])
        self.assertEqual(len(targets), 1)
        self.assertEqual(
            targets[0]["key_prefix"],
            f"s3://{BUCKET}/{DATASET}/zarr/derivatives/pipeline-x/sub-01/eeg/sub-01_task-x_eeg.zarr/",
        )
        self.assertTrue(
            targets[0]["key_prefix"].startswith(f"s3://{BUCKET}/{DATASET}/zarr/")
        )

    def test_path_traversal_zarr_value_is_rejected_not_deleted(self):
        entry = {"path": None, "zarr": "derivatives/../../../etc/evil.zarr"}
        targets, rejected = prepare_targets(BUCKET, DATASET, [entry])
        self.assertEqual(targets, [])
        self.assertEqual(len(rejected), 1)
        self.assertIs(rejected[0]["entry"], entry)

    def test_absolute_path_zarr_value_is_rejected(self):
        entry = {"path": None, "zarr": "/derivatives/evil.zarr"}
        targets, rejected = prepare_targets(BUCKET, DATASET, [entry])
        self.assertEqual(targets, [])
        self.assertEqual(len(rejected), 1)

    def test_empty_segment_zarr_value_is_rejected(self):
        entry = {"path": None, "zarr": "derivatives//evil.zarr"}
        targets, rejected = prepare_targets(BUCKET, DATASET, [entry])
        self.assertEqual(targets, [])
        self.assertEqual(len(rejected), 1)

    def test_non_zarr_suffixed_value_is_rejected(self):
        entry = {"path": None, "zarr": "derivatives/evil-not-a-store"}
        targets, rejected = prepare_targets(BUCKET, DATASET, [entry])
        self.assertEqual(targets, [])
        self.assertEqual(len(rejected), 1)

    def test_multiple_candidates_one_bad_one_good(self):
        good = _store("code/tool/sub-01_task-x_eeg.set")
        bad = {"path": None, "zarr": "../escape.zarr"}
        targets, rejected = prepare_targets(BUCKET, DATASET, [good, bad])
        self.assertEqual(len(targets), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(targets[0]["entry"], good)


class AssertWithinZarrPrefixTests(unittest.TestCase):
    def test_accepts_a_prefix_strictly_inside_the_dataset_zarr_tree(self):
        assert_within_zarr_prefix(
            f"s3://{BUCKET}/{DATASET}/zarr/derivatives/x.zarr/", bucket=BUCKET, dataset_id=DATASET
        )  # must not raise

    def test_refuses_the_bare_zarr_root_itself(self):
        with self.assertRaises(AssertionError):
            assert_within_zarr_prefix(f"s3://{BUCKET}/{DATASET}/zarr/", bucket=BUCKET, dataset_id=DATASET)

    def test_refuses_a_different_bucket(self):
        with self.assertRaises(AssertionError):
            assert_within_zarr_prefix(
                f"s3://other-bucket/{DATASET}/zarr/derivatives/x.zarr/",
                bucket=BUCKET,
                dataset_id=DATASET,
            )

    def test_refuses_a_sibling_dataset_id_that_is_a_string_prefix_of_this_one(self):
        # "nm0001" vs "nm00011": a naive substring check would wrongly accept
        # this. The required prefix includes the trailing "/zarr/" precisely
        # so a dataset id that merely starts with the same characters cannot
        # pass as this dataset's own tree.
        with self.assertRaises(AssertionError):
            assert_within_zarr_prefix(
                "s3://nemar/nm00011/zarr/x.zarr/", bucket=BUCKET, dataset_id="nm0001"
            )

    def test_refuses_escaping_outside_the_zarr_subtree(self):
        with self.assertRaises(AssertionError):
            assert_within_zarr_prefix(
                f"s3://{BUCKET}/{DATASET}/objects/x.zarr/", bucket=BUCKET, dataset_id=DATASET
            )


class RewriteIndexTests(unittest.TestCase):
    def _index(self) -> dict:
        return {
            "dataset_id": DATASET,
            "format": "nemar-zarr-index",
            "format_version": 1,
            "source_commit": "abc123",
            "updated_utc": "2026-08-01T00:00:00Z",
            "store_count": 3,
            "stores": [
                _store("derivatives/pipeline-x/sub-01_task-x_eeg.set"),
                _store("sub-01/eeg/sub-01_task-x_eeg.set"),
                _store("sub-02/eeg/sub-02_task-y_eeg.set"),
            ],
            "failure_count": 2,
            "failures": [
                _failure("derivatives/pipeline-x/sub-01_task-broken_eeg.set",
                          "derivatives/pipeline-x/sub-01_task-broken_eeg.zarr"),
                _failure("sub-03/eeg/sub-03_task-z_eeg.set", "sub-03/eeg/sub-03_task-z_eeg.zarr"),
            ],
        }

    def test_drops_purged_store_entries_and_recomputes_count(self):
        index = self._index()
        purged = {"derivatives/pipeline-x/sub-01_task-x_eeg.zarr"}
        out = rewrite_index(index, purged)
        self.assertEqual([e["zarr"] for e in out["stores"]],
                          ["sub-01/eeg/sub-01_task-x_eeg.zarr", "sub-02/eeg/sub-02_task-y_eeg.zarr"])
        self.assertEqual(out["store_count"], 2)

    def test_drops_matching_failure_entries_keeps_others(self):
        index = self._index()
        purged = {"derivatives/pipeline-x/sub-01_task-x_eeg.zarr",
                   "derivatives/pipeline-x/sub-01_task-broken_eeg.zarr"}
        out = rewrite_index(index, purged)
        self.assertEqual(len(out["failures"]), 1)
        self.assertEqual(out["failures"][0]["zarr"], "sub-03/eeg/sub-03_task-z_eeg.zarr")
        self.assertEqual(out["failure_count"], 1)

    def test_preserves_unrelated_top_level_fields_verbatim(self):
        index = self._index()
        index["some_future_field"] = {"nested": [1, 2, 3]}
        out = rewrite_index(index, set())
        self.assertEqual(out["dataset_id"], DATASET)
        self.assertEqual(out["format"], "nemar-zarr-index")
        self.assertEqual(out["format_version"], 1)
        self.assertEqual(out["source_commit"], "abc123")
        self.assertEqual(out["updated_utc"], "2026-08-01T00:00:00Z")
        self.assertEqual(out["some_future_field"], {"nested": [1, 2, 3]})

    def test_preserves_remaining_store_entries_byte_for_byte(self):
        index = self._index()
        purged = {"derivatives/pipeline-x/sub-01_task-x_eeg.zarr"}
        out = rewrite_index(index, purged)
        original_by_zarr = {e["zarr"]: e for e in index["stores"]}
        for entry in out["stores"]:
            self.assertEqual(entry, original_by_zarr[entry["zarr"]])

    def test_preserves_entry_order(self):
        index = self._index()
        out = rewrite_index(index, {"sub-01/eeg/sub-01_task-x_eeg.zarr"})
        self.assertEqual(
            [e["zarr"] for e in out["stores"]],
            ["derivatives/pipeline-x/sub-01_task-x_eeg.zarr", "sub-02/eeg/sub-02_task-y_eeg.zarr"],
        )

    def test_purging_nothing_is_a_no_op_on_content(self):
        index = self._index()
        out = rewrite_index(index, set())
        self.assertEqual(out["stores"], index["stores"])
        self.assertEqual(out["failures"], index["failures"])
        self.assertEqual(out["store_count"], index["store_count"])
        self.assertEqual(out["failure_count"], index["failure_count"])

    def test_does_not_mutate_the_input_index(self):
        index = self._index()
        before = copy.deepcopy(index)
        rewrite_index(index, {"derivatives/pipeline-x/sub-01_task-x_eeg.zarr"})
        self.assertEqual(index, before)

    def test_idempotent_rerun_on_its_own_output(self):
        index = self._index()
        purged = {"derivatives/pipeline-x/sub-01_task-x_eeg.zarr",
                   "derivatives/pipeline-x/sub-01_task-broken_eeg.zarr"}
        once = rewrite_index(index, purged)
        twice = rewrite_index(once, purged)
        self.assertEqual(once, twice)

    def test_never_introduces_a_stores_or_failures_key_that_was_absent(self):
        index = {"dataset_id": DATASET, "format": "nemar-zarr-index"}
        out = rewrite_index(index, {"anything.zarr"})
        self.assertNotIn("stores", out)
        self.assertNotIn("store_count", out)
        self.assertNotIn("failures", out)
        self.assertNotIn("failure_count", out)


class FullPurePipelineTests(unittest.TestCase):
    """The selection -> prepare -> rewrite chain end to end, over one
    realistic index, entirely with in-memory data structures (no S3)."""

    def _index(self) -> dict:
        return {
            "dataset_id": DATASET,
            "format": "nemar-zarr-index",
            "format_version": 1,
            "source_commit": "deadbeef",
            "updated_utc": "2026-08-01T00:00:00Z",
            "store_count": 4,
            "stores": [
                _store("derivatives/pipeline-x/sub-01_task-x_eeg.set"),
                _store("sourcedata/vendor/sub-02_task-y_eeg.edf"),
                _store("sub-01/eeg/sub-01_task-x_eeg.set"),
                _store("sub-02/eeg/sub-02_task-y_eeg.set"),
            ],
            "failure_count": 1,
            "failures": [
                _failure("derivatives/pipeline-x/sub-03_task-broken_eeg.set",
                          "derivatives/pipeline-x/sub-03_task-broken_eeg.zarr"),
            ],
        }

    def _purged_rels(self, index: dict) -> set[str]:
        candidates, anomalies = select_purge_candidates(index)
        self.assertEqual(anomalies, [])
        targets, rejected = prepare_targets(BUCKET, DATASET, candidates)
        self.assertEqual(rejected, [])
        return {t["rel_store"] for t in targets}

    def test_dry_run_computes_the_right_set_without_touching_the_index(self):
        index = self._index()
        before = copy.deepcopy(index)
        purged = self._purged_rels(index)
        self.assertEqual(
            purged,
            {
                "derivatives/pipeline-x/sub-01_task-x_eeg.zarr",
                "sourcedata/vendor/sub-02_task-y_eeg.zarr",
            },
        )
        # Merely computing what WOULD be purged must not touch the index.
        self.assertEqual(index, before)

    def test_execute_then_rerun_is_idempotent(self):
        index = self._index()
        purged = self._purged_rels(index)
        rewritten = rewrite_index(index, purged)

        # Raw stores and the unrelated failure entry all survive.
        self.assertEqual(
            sorted(e["zarr"] for e in rewritten["stores"]),
            ["sub-01/eeg/sub-01_task-x_eeg.zarr", "sub-02/eeg/sub-02_task-y_eeg.zarr"],
        )
        self.assertEqual(rewritten["store_count"], 2)
        self.assertEqual(len(rewritten["failures"]), 1)

        # Re-running the whole pipeline against the ALREADY-rewritten index
        # finds nothing left to purge, and rewriting again is a no-op.
        purged_again = self._purged_rels(rewritten)
        self.assertEqual(purged_again, set())
        self.assertEqual(rewrite_index(rewritten, purged_again), rewritten)


class ParseS3LsSummaryTests(unittest.TestCase):
    def test_typical_summary_output(self):
        output = (
            "2026-01-01 00:00:00        512 nm000123/zarr/derivatives/x.zarr/zarr.json\n"
            "2026-01-01 00:00:00       2048 nm000123/zarr/derivatives/x.zarr/c/0\n"
            "\n"
            "Total Objects: 2\n"
            "   Total Size: 2560\n"
        )
        self.assertEqual(parse_s3_ls_summary(output), (2, 2560))

    def test_empty_prefix_produces_empty_output(self):
        # aws s3 ls prints nothing at all (not even the summary) for a prefix
        # matching zero keys.
        self.assertEqual(parse_s3_ls_summary(""), (0, 0))

    def test_single_object(self):
        output = "2026-01-01 00:00:00        100 nm000123/zarr/code/x.zarr/zarr.json\n\nTotal Objects: 1\n   Total Size: 100\n"
        self.assertEqual(parse_s3_ls_summary(output), (1, 100))

    def test_missing_summary_lines_defaults_to_zero(self):
        self.assertEqual(parse_s3_ls_summary("some unrelated garbage\n"), (0, 0))


if __name__ == "__main__":
    unittest.main()
