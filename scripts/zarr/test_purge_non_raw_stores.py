#!/usr/bin/env python3
"""Unit tests for the pure helpers in scripts/zarr/purge_non_raw_stores.py.

No mocks: these exercise candidate selection, the escape-the-prefix guard,
index rewriting, S3-listing-output parsing, and the plan/decision/summary
functions that own `purge_dataset`'s ordering and error-isolation
properties directly, over realistic `index.json` documents and path/prefix
strings -- no business logic (S3 calls, subprocess, network) is patched or
faked anywhere in this file. `write_audit_log`'s tests use a real temp
directory on the local filesystem (not S3), so no I/O is faked there either.

The S3 list/delete/read/write orchestration itself -- `stat_prefix` and
`_execute_target_step`'s subprocess/S3 calls, `discover_excluded_stores`,
`write_index`, `list_dataset_ids`, and `purge_dataset`/`main()` end to end --
genuinely requires a real S3 client and is deliberately NOT exercised here;
see the PR description for what that leaves unverified.

Run with:
    python3 scripts/zarr/test_purge_non_raw_stores.py
    uv run python scripts/zarr/test_purge_non_raw_stores.py
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from purge_non_raw_stores import (  # type: ignore[import-not-found]
    INDEX_FORMAT,
    _print_dataset_summary,
    assert_within_zarr_prefix,
    dataset_has_issue,
    decide_target_action,
    interpret_s3_ls_result,
    load_snapshot_index,
    main,
    parse_s3_ls_summary,
    plan_dataset_operations,
    plan_snapshot_index_rewrite,
    prepare_targets,
    rewrite_index,
    select_purge_candidates,
    snapshot_dataset_ids,
    summarize_target_outcomes,
    write_audit_log,
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

    def test_excluded_zarr_with_no_path_at_all_is_an_anomaly_not_consent(self):
        # A `path`-less entry must not be read as "no disagreement, so
        # select it" -- a stripped `path` is itself the shape of corruption
        # the disagreement check exists to catch.
        entry = {"zarr": "derivatives/evil.zarr"}
        candidates, anomalies = select_purge_candidates({"stores": [entry]})
        self.assertEqual(candidates, [])
        self.assertEqual(len(anomalies), 1)
        self.assertIs(anomalies[0]["entry"], entry)

    def test_excluded_zarr_with_non_string_path_is_an_anomaly(self):
        entry = {"zarr": "derivatives/evil.zarr", "path": None}
        candidates, anomalies = select_purge_candidates({"stores": [entry]})
        self.assertEqual(candidates, [])
        self.assertEqual(len(anomalies), 1)

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

    def test_duplicate_zarr_value_second_occurrence_is_rejected_not_a_second_target(self):
        # A well-formed index can never have two `stores` entries sharing one
        # `zarr` value (it's the dict key in generate_zarr.merge_index), so
        # this only happens for a hand-edited/corrupted index -- both name
        # the identical S3 location, so only ONE target (and one stat/delete
        # attempt) should ever be produced for it.
        first = {"path": "derivatives/x/sub-01_task-a_eeg.set", "zarr": "derivatives/x/shared.zarr"}
        second = {"path": "derivatives/y/sub-02_task-b_eeg.set", "zarr": "derivatives/x/shared.zarr"}
        targets, rejected = prepare_targets(BUCKET, DATASET, [first, second])
        self.assertEqual(len(targets), 1)
        self.assertIs(targets[0]["entry"], first)
        self.assertEqual(len(rejected), 1)
        self.assertIs(rejected[0]["entry"], second)
        self.assertIn("duplicate", rejected[0]["reason"])

    def test_three_duplicates_only_the_first_becomes_a_target(self):
        entries = [{"path": None, "zarr": "code/shared.zarr"} for _ in range(3)]
        targets, rejected = prepare_targets(BUCKET, DATASET, entries)
        self.assertEqual(len(targets), 1)
        self.assertEqual(len(rejected), 2)


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


class DecideTargetActionTests(unittest.TestCase):
    def test_zero_objects_is_always_already_absent_regardless_of_execute(self):
        self.assertEqual(decide_target_action(0, execute=True), "already_absent")
        self.assertEqual(decide_target_action(0, execute=False), "already_absent")

    def test_nonzero_objects_dry_run_is_would_purge(self):
        self.assertEqual(decide_target_action(5, execute=False), "would_purge")

    def test_nonzero_objects_execute_is_delete(self):
        self.assertEqual(decide_target_action(5, execute=True), "delete")


class PlanDatasetOperationsTests(unittest.TestCase):
    def test_preserves_target_order_and_ends_with_exactly_one_rewrite_step(self):
        targets = [
            {"rel_store": "a.zarr", "key_prefix": "s3://b/d/zarr/a.zarr/", "entry": {"path": "a"}},
            {"rel_store": "b.zarr", "key_prefix": "s3://b/d/zarr/b.zarr/", "entry": {"path": "b"}},
        ]
        plan = plan_dataset_operations(targets)
        self.assertEqual(
            [s["op"] for s in plan],
            ["stat_then_maybe_delete", "stat_then_maybe_delete", "rewrite_index"],
        )
        self.assertEqual([s["rel_store"] for s in plan[:-1]], ["a.zarr", "b.zarr"])
        self.assertEqual(plan[-1], {"op": "rewrite_index"})

    def test_no_targets_still_ends_with_the_rewrite_step(self):
        self.assertEqual(plan_dataset_operations([]), [{"op": "rewrite_index"}])


class SummarizeTargetOutcomesTests(unittest.TestCase):
    def test_purged_rels_excludes_errored_targets(self):
        # The core error-isolation property: a target this run failed to
        # stat or failed to delete must NEVER end up in the set that gets
        # dropped from index.json -- its entry has to survive to be retried.
        outcomes = [
            {"rel_store": "a.zarr", "state": "purged", "object_count": 3, "bytes": 300},
            {"rel_store": "b.zarr", "state": "delete_error", "object_count": 2, "bytes": 200, "error": "boom"},
            {"rel_store": "c.zarr", "state": "already_absent", "object_count": 0, "bytes": 0},
            {"rel_store": "d.zarr", "state": "stat_error", "object_count": 0, "bytes": 0, "error": "timeout"},
            {"rel_store": "e.zarr", "state": "would_purge", "object_count": 1, "bytes": 10},
        ]
        summary = summarize_target_outcomes(outcomes)
        self.assertEqual(summary["purged_rels"], {"a.zarr", "c.zarr"})
        self.assertEqual(len(summary["delete_errors"]), 2)
        self.assertEqual({o["rel_store"] for o in summary["delete_errors"]}, {"b.zarr", "d.zarr"})
        self.assertEqual(summary["bytes_freed"], 300)
        self.assertEqual(summary["objects_freed"], 3)

    def test_already_absent_never_counted_as_freed_bytes_or_objects(self):
        # Folding an already-gone target into purged_rels must not inflate
        # "freed" accounting for data this run did not actually delete.
        outcomes = [{"rel_store": "a.zarr", "state": "already_absent", "object_count": 0, "bytes": 0}]
        summary = summarize_target_outcomes(outcomes)
        self.assertEqual(summary["purged_rels"], {"a.zarr"})
        self.assertEqual(summary["bytes_freed"], 0)
        self.assertEqual(summary["objects_freed"], 0)

    def test_preserves_outcome_order_within_each_bucket(self):
        outcomes = [
            {"rel_store": "z.zarr", "state": "purged", "object_count": 1, "bytes": 1},
            {"rel_store": "a.zarr", "state": "purged", "object_count": 1, "bytes": 1},
        ]
        summary = summarize_target_outcomes(outcomes)
        self.assertEqual([o["rel_store"] for o in summary["purged"]], ["z.zarr", "a.zarr"])

    def test_empty_outcomes(self):
        summary = summarize_target_outcomes([])
        self.assertEqual(summary["purged_rels"], set())
        self.assertEqual(summary["purged"], [])
        self.assertEqual(summary["bytes_freed"], 0)
        self.assertEqual(summary["objects_freed"], 0)


class DatasetHasIssueTests(unittest.TestCase):
    """The exit code and every printed summary read `dataset_has_issue` as
    their single source of truth, so this pins exactly which fields flip it
    -- including the two the bulk exit code previously missed."""

    def test_clean_result_has_no_issue(self):
        self.assertFalse(dataset_has_issue({"status": "ok", "purged": [{"zarr": "a.zarr"}]}))

    def test_hard_error_is_an_issue(self):
        self.assertTrue(dataset_has_issue({"status": "error", "error": "boom"}))

    def test_delete_errors_is_an_issue(self):
        self.assertTrue(dataset_has_issue({"status": "ok", "delete_errors": [{"zarr": "a.zarr"}]}))

    def test_rejected_is_an_issue(self):
        self.assertTrue(dataset_has_issue({"status": "ok", "rejected": [{"entry": {}}]}))

    def test_anomalies_is_an_issue(self):
        self.assertTrue(dataset_has_issue({"status": "ok", "anomalies": [{"entry": {}}]}))

    def test_extra_on_s3_not_in_index_is_an_issue(self):
        # This is the exact gap the bulk exit code had: a dry run that finds
        # real unindexed excluded-tree stores on S3 must not exit 0.
        self.assertTrue(dataset_has_issue({"status": "ok", "extra_on_s3_not_in_index": ["derivatives/x.zarr"]}))

    def test_extra_on_s3_check_error_is_an_issue(self):
        self.assertTrue(dataset_has_issue({"status": "ok", "extra_on_s3_check_error": "timeout"}))

    def test_no_index_status_alone_is_not_an_issue(self):
        self.assertFalse(dataset_has_issue({"status": "no_index"}))


class WriteAuditLogTests(unittest.TestCase):
    """Real local-filesystem I/O (no S3, no faking): these exercise the
    actual atomic-write behaviour `write_audit_log` promises."""

    def test_writes_valid_json_matching_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "audit.json")
            report = {"format": "nemar-zarr-purge-audit", "datasets": [{"dataset_id": "nm000123"}]}
            write_audit_log(path, report)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), report)

    def test_no_leftover_temp_file_after_a_successful_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "audit.json")
            write_audit_log(path, {"a": 1})
            leftovers = [p for p in Path(tmp).iterdir() if p.name != "audit.json"]
            self.assertEqual(leftovers, [])

    def test_second_call_does_not_collide_with_a_concurrent_first_calls_temp_file(self):
        # Regression check for the fixed bug: a deterministic f"{path}.tmp"
        # sibling would collide if two runs targeted the same --audit-log
        # concurrently. Simulate by holding the first call's temp file open
        # (via NamedTemporaryFile directly, same dir) while the second
        # `write_audit_log` call runs to completion -- it must not touch or
        # be blocked by the first's temp file, and must still produce valid
        # final content.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "audit.json")
            with tempfile.NamedTemporaryFile(
                "w", prefix=".purge-audit-", suffix=".json.tmp", dir=tmp, delete=False
            ) as held:
                held.write("not json -- simulates another run's in-progress temp file")
                write_audit_log(path, {"final": True})
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"final": True})

    def test_overwrites_an_existing_audit_file_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "audit.json")
            write_audit_log(path, {"run": 1})
            write_audit_log(path, {"run": 2})
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"run": 2})


def _index_doc(dataset_id: str, stores: list[dict], **extra) -> dict:
    doc = {
        "dataset_id": dataset_id,
        "format": INDEX_FORMAT,
        "format_version": 1,
        "source_commit": "a" * 40,
        "updated_utc": "2026-08-12T12:00:00Z",
        "store_count": len(stores),
        "stores": stores,
    }
    doc.update(extra)
    return doc


class LoadSnapshotIndexTests(unittest.TestCase):
    """Real temp files on disk; nothing about the filesystem is faked."""

    def _write(self, d: Path, name: str, doc) -> Path:
        p = d / name
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return p

    def test_loads_a_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            doc = _index_doc(DATASET, [_store("derivatives/x/a_eeg.edf")])
            self._write(d, f"{DATASET}.json", doc)
            self.assertEqual(load_snapshot_index(td, DATASET), doc)

    def test_missing_snapshot_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaises(FileNotFoundError):
            load_snapshot_index(td, DATASET)

    def test_rejects_snapshot_for_a_different_dataset(self):
        """The guard that matters most: a local path is whatever was typed, so a
        snapshot of another dataset must not be able to authorize deletes here."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write(d, f"{DATASET}.json", _index_doc("nm999999", []))
            with self.assertRaises(ValueError) as cm:
                load_snapshot_index(td, DATASET)
            self.assertIn("dataset_id", str(cm.exception))

    def test_rejects_foreign_json_without_the_index_format_marker(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write(d, f"{DATASET}.json", {"dataset_id": DATASET, "stores": []})
            with self.assertRaises(ValueError) as cm:
                load_snapshot_index(td, DATASET)
            self.assertIn("format", str(cm.exception))

    def test_rejects_non_object_json(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write(d, f"{DATASET}.json", [1, 2, 3])
            with self.assertRaises(TypeError):
                load_snapshot_index(td, DATASET)

    def test_malformed_json_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / f"{DATASET}.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_snapshot_index(td, DATASET)

    def test_a_snapshot_cannot_authorize_purging_a_raw_store(self):
        """Snapshot mode changes where the store list is read, not what may be
        deleted. A raw `sub-*/` entry stays unselectable even when a snapshot
        lists it, because selection re-derives the raw/non-raw split itself."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            raw = _store("sub-01/eeg/sub-01_task-a_eeg.edf")
            nonraw = _store("derivatives/prep/sub-01_task-a_eeg.edf")
            self._write(d, f"{DATASET}.json", _index_doc(DATASET, [raw, nonraw]))
            index = load_snapshot_index(td, DATASET)
            candidates, anomalies = select_purge_candidates(index)
            self.assertEqual([c["zarr"] for c in candidates], [nonraw["zarr"]])
            self.assertEqual(anomalies, [])


class SnapshotDatasetIdsTests(unittest.TestCase):
    def test_lists_stems_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for name in ("on000002.json", "nm000001.json", "notes.txt"):
                (d / name).write_text("{}", encoding="utf-8")
            self.assertEqual(snapshot_dataset_ids(td), ["nm000001", "on000002"])

    def test_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(snapshot_dataset_ids("/nonexistent/path/for/test"), [])


class PlanSnapshotIndexRewriteTests(unittest.TestCase):
    """The load-bearing property: snapshot mode must never publish the snapshot."""

    def test_skips_when_live_index_does_not_list_the_purged_stores(self):
        """The normal stranded case. The live index already omits them -- that is
        exactly why they were unreachable -- so there is nothing to rewrite, and
        writing anything here would resurrect the entries being cleaned up."""
        live = _index_doc(DATASET, [_store("sub-01/eeg/sub-01_task-a_eeg.edf")])
        self.assertIsNone(
            plan_snapshot_index_rewrite(live, {"derivatives/prep/sub-01_task-a_eeg.zarr"})
        )

    def test_rewrites_only_the_live_document_when_it_still_lists_them(self):
        raw = _store("sub-01/eeg/sub-01_task-a_eeg.edf")
        stale = _store("derivatives/prep/sub-01_task-a_eeg.edf")
        live = _index_doc(DATASET, [raw, stale], source_commit="b" * 40)
        out = plan_snapshot_index_rewrite(live, {stale["zarr"]})
        self.assertIsNotNone(out)
        self.assertEqual([e["zarr"] for e in out["stores"]], [raw["zarr"]])
        self.assertEqual(out["store_count"], 1)
        # Derived from the LIVE document, so the live commit survives; had the
        # snapshot been used, this would be the snapshot's "a"*40.
        self.assertEqual(out["source_commit"], "b" * 40)

    def test_matches_a_purged_rel_listed_only_under_failures(self):
        live = _index_doc(
            DATASET,
            [_store("sub-01/eeg/sub-01_task-a_eeg.edf")],
            failures=[_failure("derivatives/x/a_eeg.edf", "derivatives/x/a_eeg.zarr")],
            failure_count=1,
        )
        out = plan_snapshot_index_rewrite(live, {"derivatives/x/a_eeg.zarr"})
        self.assertIsNotNone(out)
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["failure_count"], 0)

    def test_no_purged_rels_or_no_live_index_is_a_skip(self):
        live = _index_doc(DATASET, [_store("derivatives/x/a_eeg.edf")])
        self.assertIsNone(plan_snapshot_index_rewrite(live, set()))
        self.assertIsNone(plan_snapshot_index_rewrite(None, {"derivatives/x/a_eeg.zarr"}))
        self.assertIsNone(plan_snapshot_index_rewrite({}, {"derivatives/x/a_eeg.zarr"}))

    def test_does_not_mutate_the_live_index(self):
        stale = _store("derivatives/prep/a_eeg.edf")
        live = _index_doc(DATASET, [stale])
        before = copy.deepcopy(live)
        plan_snapshot_index_rewrite(live, {stale["zarr"]})
        self.assertEqual(live, before)


class NoSnapshotStatusTests(unittest.TestCase):
    """A named target that was silently skipped must not look clean.

    The sibling `no_index` status is deliberately benign (nothing to purge, so
    the no-op was correct). `no_snapshot` is the opposite: the dataset was named
    as a target and never examined, and its zero-purged/zero-error result is
    byte-identical to a genuinely clean dataset's. So it has to be an issue for
    the exit code, and it has to print differently.
    """

    def test_no_snapshot_is_an_issue(self):
        self.assertTrue(dataset_has_issue({"status": "no_snapshot"}))

    def test_no_index_is_still_not_an_issue(self):
        """Pins the asymmetry, so a future change cannot collapse the two."""
        self.assertFalse(dataset_has_issue({"status": "no_index"}))

    def test_summary_line_says_skipped_not_zero_counts(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_dataset_summary({"dataset_id": DATASET, "status": "no_snapshot"})
        out = buf.getvalue()
        self.assertIn("SKIPPED", out)
        self.assertIn("no index snapshot", out)
        # The specific confusion this guards against: the generic counts line.
        self.assertNotIn("store(s) would purge", out)
        self.assertNotIn("store(s) purged", out)


class InterpretS3LsResultTests(unittest.TestCase):
    """`aws s3 ls` exits 1 for a prefix matching zero keys but still prints a
    valid summary. Reading that as a failure made `already_absent` unreachable
    and left deleted stores stuck in the index forever (see nm000172)."""

    # Verbatim from `aws s3 ls s3://nemar/<deleted-prefix>/ --recursive --summarize`,
    # which exits 1.
    EMPTY_OUT = "\nTotal Objects: 0\n   Total Size: 0\n"
    NONEMPTY_OUT = (
        "2026-08-12 12:52:14        512 nm000191/zarr/sub-01/eeg/x.zarr/zarr.json\n"
        "\nTotal Objects: 1\n   Total Size: 512\n"
    )

    def test_exit_1_with_a_zero_summary_is_a_real_answer(self):
        self.assertEqual(interpret_s3_ls_result(1, self.EMPTY_OUT), (0, 0))

    def test_already_absent_is_therefore_reachable(self):
        """The whole point: this must become `already_absent`, not an error."""
        counts = interpret_s3_ls_result(1, self.EMPTY_OUT)
        self.assertIsNotNone(counts)
        self.assertEqual(decide_target_action(counts[0], execute=True), "already_absent")

    def test_exit_0_with_objects_parses_normally(self):
        self.assertEqual(interpret_s3_ls_result(0, self.NONEMPTY_OUT), (1, 512))

    def test_exit_0_with_no_summary_is_still_zero_not_a_failure(self):
        self.assertEqual(interpret_s3_ls_result(0, ""), (0, 0))

    def test_genuine_failure_returns_none(self):
        """A credential/permission/network failure prints no summary, so it must
        stay an error rather than being silently read as 'already gone' -- which
        would drop the index entry for a store still present in S3."""
        self.assertIsNone(interpret_s3_ls_result(255, ""))
        self.assertIsNone(interpret_s3_ls_result(1, "aws: [ERROR]: 'deadbeef'\n"))
        self.assertIsNone(interpret_s3_ls_result(1, "fatal error: Unable to locate credentials\n"))


class MainSnapshotWiringTests(unittest.TestCase):
    """`main()` called for real -- no S3 is reached on either of these paths."""

    def test_nonexistent_snapshot_dir_exits_2_without_touching_s3(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--all", "--from-index-snapshot", "/nonexistent/dir/for/test"])
        self.assertEqual(rc, 2)
        self.assertIn("no such directory", buf.getvalue())

    def test_all_with_empty_snapshot_dir_reports_the_dir_not_the_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--all", "--from-index-snapshot", td, "--audit-log",
                           str(Path(td) / "audit.json")])
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            # Must not have fallen back to listing the bucket.
            self.assertIn(td, out)
            self.assertIn("0 dataset(s)", out)


if __name__ == "__main__":
    unittest.main()
