#!/usr/bin/env python3
"""Unit tests for the pure helpers in scripts/zarr/generate_zarr.py.

No mocks: these exercise the path-classification, worklist, index-merge, and
annex-key parsing logic directly (the git/S3/biosigIO I/O is validated E2E by a
`workflow_dispatch` run of run-generate-zarr.yml on nm099999, not here).

Run with:
    python3 scripts/zarr/test_generate_zarr.py
    uv run python scripts/zarr/test_generate_zarr.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_zarr import (  # type: ignore[import-not-found]  # noqa: E402  (sibling module via sys.path)
    _AWS_OP_TIMEOUT,
    _AWS_RM_TIMEOUT,
    INMEM_MEM_FACTOR,
    STREAM_PEAK_BYTES,
    RecordingTooLarge,
    _aws,
    _recording_size_bytes,
    affected_primaries,
    annex_key_size,
    bids_suffix_modality,
    convert_recording,
    projected_peak_bytes,
    recording_mem_budget_bytes,
    should_stream,
    STREAM_EDF_MIN_BYTES,
    compute_worklist,
    ctf_ds_of,
    ctf_ds_recordings,
    electrode_positions_for,
    embed_attr,
    embed_root_attr,
    event_descriptions_for,
    events_sibling_for,
    is_ctf_ds,
    is_primary,
    is_split_fif,
    materialize_local,
    merge_index,
    parse_annex_key,
    power_line_frequency_for,
    reason_for_code,
    safe_store_prefix,
    split_group_key,
    split_heads_and_members,
    split_index,
    split_members_for,
    store_rel_for,
)


def by_dir(primaries: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in primaries:
        d = p.rsplit("/", 1)[0] if "/" in p else ""
        out.setdefault(d, []).append(p)
    return out


class TestPathClassification(unittest.TestCase):
    def test_is_primary(self):
        self.assertTrue(is_primary("sub-01/eeg/sub-01_task-x_eeg.set"))
        self.assertTrue(is_primary("sub-01/eeg/sub-01_eeg.EDF"))  # case-insensitive
        self.assertTrue(is_primary("sub-01/meg/sub-01_meg.fif"))
        self.assertFalse(is_primary("sub-01/eeg/sub-01_task-x_eeg.fdt"))  # companion
        self.assertFalse(is_primary("sub-01/eeg/sub-01_task-x_events.tsv"))
        self.assertFalse(is_primary("dataset_description.json"))

    def test_store_rel_for(self):
        self.assertEqual(
            store_rel_for("sub-01/eeg/sub-01_task-x_eeg.set"),
            "sub-01/eeg/sub-01_task-x_eeg.zarr",
        )
        self.assertEqual(
            store_rel_for("sub-01/emg/sub-01_task-x_emg.edf"),
            "sub-01/emg/sub-01_task-x_emg.zarr",
        )
        self.assertEqual(
            store_rel_for("sub-01/eeg/sub-01_eeg.vhdr"), "sub-01/eeg/sub-01_eeg.zarr"
        )

    def test_events_sibling_for(self):
        self.assertEqual(
            events_sibling_for("sub-01/eeg/sub-01_task-x_eeg.set"),
            "sub-01/eeg/sub-01_task-x_events.tsv",
        )
        self.assertEqual(
            events_sibling_for("sub-02/emg/sub-02_task-rest_run-1_emg.edf"),
            "sub-02/emg/sub-02_task-rest_run-1_events.tsv",
        )

    def test_events_sibling_for_split_fif_drops_split_entity(self):
        # A split recording shares one events file without the split- entity.
        self.assertEqual(
            events_sibling_for("sub-03/meg/sub-03_task-x_run-02_split-01_meg.fif"),
            "sub-03/meg/sub-03_task-x_run-02_events.tsv",
        )


class TestAffectedPrimaries(unittest.TestCase):
    def setUp(self):
        self.primaries = [
            "sub-01/eeg/sub-01_task-x_eeg.set",
            "sub-01/eeg/sub-01_eeg.vhdr",
        ]
        self.bd = by_dir(self.primaries)

    def test_primary_maps_to_itself(self):
        self.assertEqual(
            affected_primaries("sub-01/eeg/sub-01_task-x_eeg.set", self.bd),
            {"sub-01/eeg/sub-01_task-x_eeg.set"},
        )

    def test_primary_not_at_head_maps_to_nothing(self):
        self.assertEqual(affected_primaries("sub-09/eeg/sub-09_eeg.set", self.bd), set())

    def test_fdt_companion_maps_to_set(self):
        self.assertEqual(
            affected_primaries("sub-01/eeg/sub-01_task-x_eeg.fdt", self.bd),
            {"sub-01/eeg/sub-01_task-x_eeg.set"},
        )

    def test_brainvision_companions_map_to_vhdr(self):
        for comp in ("sub-01/eeg/sub-01_eeg.eeg", "sub-01/eeg/sub-01_eeg.vmrk"):
            self.assertEqual(
                affected_primaries(comp, self.bd), {"sub-01/eeg/sub-01_eeg.vhdr"}
            )

    def test_events_maps_to_same_base_primaries(self):
        self.assertEqual(
            affected_primaries("sub-01/eeg/sub-01_task-x_events.tsv", self.bd),
            {"sub-01/eeg/sub-01_task-x_eeg.set"},
        )


class TestComputeWorklist(unittest.TestCase):
    def setUp(self):
        self.head = [
            "dataset_description.json",
            "sub-01/eeg/sub-01_task-x_eeg.set",
            "sub-01/eeg/sub-01_task-x_eeg.fdt",
            "sub-01/eeg/sub-01_task-x_events.tsv",
            "sub-02/eeg/sub-02_task-x_eeg.set",
        ]

    def test_full_converts_every_primary(self):
        convert, remove = compute_worklist(self.head, [], full=True)
        self.assertEqual(
            convert,
            ["sub-01/eeg/sub-01_task-x_eeg.set", "sub-02/eeg/sub-02_task-x_eeg.set"],
        )
        self.assertEqual(remove, [])

    def test_modify_primary(self):
        convert, remove = compute_worklist(
            self.head, [("M", "sub-01/eeg/sub-01_task-x_eeg.set")], full=False
        )
        self.assertEqual(convert, ["sub-01/eeg/sub-01_task-x_eeg.set"])
        self.assertEqual(remove, [])

    def test_modify_events_only(self):
        convert, _ = compute_worklist(
            self.head, [("M", "sub-01/eeg/sub-01_task-x_events.tsv")], full=False
        )
        self.assertEqual(convert, ["sub-01/eeg/sub-01_task-x_eeg.set"])

    def test_modify_companion_only(self):
        convert, _ = compute_worklist(
            self.head, [("M", "sub-01/eeg/sub-01_task-x_eeg.fdt")], full=False
        )
        self.assertEqual(convert, ["sub-01/eeg/sub-01_task-x_eeg.set"])

    def test_delete_primary_removes_store(self):
        head = [p for p in self.head if p != "sub-02/eeg/sub-02_task-x_eeg.set"]
        convert, remove = compute_worklist(
            head, [("D", "sub-02/eeg/sub-02_task-x_eeg.set")], full=False
        )
        self.assertEqual(convert, [])
        self.assertEqual(remove, ["sub-02/eeg/sub-02_task-x_eeg.zarr"])

    def test_rename_is_remove_plus_convert(self):
        # git diff --no-renames emits D old + A new
        head = [
            "sub-01/eeg/sub-01_task-y_eeg.set",  # renamed-to exists at HEAD
        ]
        convert, remove = compute_worklist(
            head,
            [
                ("D", "sub-01/eeg/sub-01_task-x_eeg.set"),
                ("A", "sub-01/eeg/sub-01_task-y_eeg.set"),
            ],
            full=False,
        )
        self.assertEqual(convert, ["sub-01/eeg/sub-01_task-y_eeg.set"])
        self.assertEqual(remove, ["sub-01/eeg/sub-01_task-x_eeg.zarr"])

    def test_delete_events_reconverts_sibling(self):
        # events.tsv removed but the recording remains -> rebuild without events
        convert, remove = compute_worklist(
            self.head, [("D", "sub-01/eeg/sub-01_task-x_events.tsv")], full=False
        )
        self.assertEqual(convert, ["sub-01/eeg/sub-01_task-x_eeg.set"])
        self.assertEqual(remove, [])

    def test_metadata_only_change_is_empty(self):
        convert, remove = compute_worklist(
            self.head, [("M", "dataset_description.json")], full=False
        )
        self.assertEqual(convert, [])
        self.assertEqual(remove, [])


class TestMergeIndex(unittest.TestCase):
    def test_upsert_remove_and_carry_over(self):
        prior = {
            "source_commit": "old",
            "stores": [
                {"zarr": "sub-01/eeg/a_eeg.zarr", "store": "old-a"},
                {"zarr": "sub-02/eeg/b_eeg.zarr", "store": "keep-b"},
            ],
        }
        converted = [{"zarr": "sub-01/eeg/a_eeg.zarr", "store": "new-a"}]
        index = merge_index(
            prior, "nm000104", "newsha", converted, ["sub-02/eeg/b_eeg.zarr"], "2026-06-02T00:00:00Z"
        )
        self.assertEqual(index["source_commit"], "newsha")
        self.assertEqual(index["store_count"], 1)
        self.assertEqual(index["format"], "nemar-zarr-index")
        self.assertEqual(index["stores"], [{"zarr": "sub-01/eeg/a_eeg.zarr", "store": "new-a"}])

    def test_no_prior_builds_fresh(self):
        index = merge_index(
            None, "nm000104", "sha", [{"zarr": "x/y_eeg.zarr"}], [], "2026-06-02T00:00:00Z"
        )
        self.assertEqual(index["store_count"], 1)
        self.assertEqual([s["zarr"] for s in index["stores"]], ["x/y_eeg.zarr"])

    def test_stores_sorted_by_zarr_path(self):
        converted = [{"zarr": "b.zarr"}, {"zarr": "a.zarr"}]
        index = merge_index(None, "nm000104", "sha", converted, [], "2026-06-02T00:00:00Z")
        self.assertEqual([s["zarr"] for s in index["stores"]], ["a.zarr", "b.zarr"])


class TestSafeStorePrefix(unittest.TestCase):
    def test_valid_store_path(self):
        self.assertEqual(
            safe_store_prefix("nemar", "nm000104", "sub-01/eeg/sub-01_task-x_eeg.zarr"),
            "s3://nemar/nm000104/zarr/sub-01/eeg/sub-01_task-x_eeg.zarr/",
        )

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            safe_store_prefix("nemar", "nm000104", "")

    def test_rejects_non_zarr(self):
        with self.assertRaises(ValueError):
            safe_store_prefix("nemar", "nm000104", "sub-01/eeg/sub-01_eeg.set")

    def test_rejects_traversal(self):
        for bad in ("../escape.zarr", "sub-01/../../x.zarr", "/abs/x.zarr", "a//b.zarr"):
            with self.assertRaises(ValueError):
                safe_store_prefix("nemar", "nm000104", bad)


class TestMaterializeLocal(unittest.TestCase):
    def test_resolves_working_tree_paths_and_annex_key(self):
        key = "SHA256E-s100--abcdef.set"
        primary = "sub-01/eeg/sub-01_task-x_eeg.set"
        events = "sub-01/eeg/sub-01_task-x_events.tsv"
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "sub-01", "eeg"))
            # annex-style symlink for the primary; a plain file for the events sidecar
            os.symlink(
                f"../../.git/annex/objects/aa/bb/{key}/{key}",
                os.path.join(repo, primary),
            )
            with open(os.path.join(repo, events), "w") as fh:
                fh.write("onset\tduration\n0\t0\n")
            pl, el, k = materialize_local(repo, primary, {primary, events})
            self.assertEqual(pl, os.path.join(repo, primary))
            self.assertEqual(el, os.path.join(repo, events))
            self.assertEqual(k, key)

    def test_no_events_sibling_when_absent(self):
        primary = "sub-02/eeg/sub-02_task-x_eeg.edf"
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "sub-02", "eeg"))
            with open(os.path.join(repo, primary), "w") as fh:
                fh.write("not-an-annex-blob")  # regular in-git file -> key None
            pl, el, k = materialize_local(repo, primary, {primary})
            self.assertEqual(pl, os.path.join(repo, primary))
            self.assertIsNone(el)
            self.assertIsNone(k)


class TestParseAnnexKey(unittest.TestCase):
    def test_locked_symlink_target(self):
        key = "SHA256E-s12345--abcdef0123456789.set"
        target = f"../../.git/annex/objects/aa/bb/{key}/{key}"
        self.assertEqual(parse_annex_key(target), key)

    def test_unlocked_pointer_content(self):
        key = "MD5E-s59778400--abc.edf"
        self.assertEqual(parse_annex_key(f"/annex/objects/{key}"), key)

    def test_non_annex_blob_returns_none(self):
        self.assertIsNone(parse_annex_key("just some file contents\n"))


class TestBidsSuffixModality(unittest.TestCase):
    def test_known_suffixes_map_to_modality(self):
        self.assertEqual(bids_suffix_modality("sub-01/eeg/sub-01_task-rest_eeg.set"), "EEG")
        self.assertEqual(bids_suffix_modality("sub-01/meg/sub-01_task-rest_meg.fif"), "MEG")
        self.assertEqual(bids_suffix_modality("sub-01/ieeg/sub-01_task-rest_ieeg.edf"), "IEEG")
        self.assertEqual(bids_suffix_modality("sub-01/emg/sub-01_task-grip_emg.edf"), "EMG")

    def test_suffix_is_case_insensitive_and_uses_basename(self):
        self.assertEqual(bids_suffix_modality("X/sub-01_task-A_EEG.SET"), "EEG")

    def test_unknown_or_missing_suffix_returns_none(self):
        self.assertIsNone(bids_suffix_modality("sub-01/beh/sub-01_task-rest_physio.tsv"))
        self.assertIsNone(bids_suffix_modality("sub-01/eeg/sub-01_channels.tsv"))
        self.assertIsNone(bids_suffix_modality("noextnounderscore"))


class TestPowerLineFrequencyFor(unittest.TestCase):
    def _write(self, root: str, rel: str, body: dict) -> None:
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(body, fh)

    def test_sibling_sidecar_wins_over_root(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            self._write(d, "sub-01/eeg/sub-01_task-rest_eeg.json", {"PowerLineFrequency": 60})
            self._write(d, "task-rest_eeg.json", {"PowerLineFrequency": 50})  # less specific
            head = {"sub-01/eeg/sub-01_task-rest_eeg.json", "task-rest_eeg.json"}
            self.assertEqual(power_line_frequency_for(d, rec, head, "HEAD"), 60.0)

    def test_inherited_from_root_when_no_sibling(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            self._write(d, "task-rest_eeg.json", {"PowerLineFrequency": 50})
            head = {"task-rest_eeg.json"}
            self.assertEqual(power_line_frequency_for(d, rec, head, "HEAD"), 50.0)

    def test_none_when_field_absent(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            self._write(d, "sub-01/eeg/sub-01_task-rest_eeg.json", {"SamplingFrequency": 1024})
            head = {"sub-01/eeg/sub-01_task-rest_eeg.json"}
            self.assertIsNone(power_line_frequency_for(d, rec, head, "HEAD"))

    def test_non_subset_entities_do_not_apply(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            # A sidecar for a different task must not apply to this recording.
            self._write(d, "sub-01/eeg/sub-01_task-other_eeg.json", {"PowerLineFrequency": 60})
            head = {"sub-01/eeg/sub-01_task-other_eeg.json"}
            self.assertIsNone(power_line_frequency_for(d, rec, head, "HEAD"))

    def test_wrong_suffix_does_not_satisfy(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            self._write(d, "sub-01/eeg/sub-01_task-rest_ieeg.json", {"PowerLineFrequency": 60})
            head = {"sub-01/eeg/sub-01_task-rest_ieeg.json"}
            self.assertIsNone(power_line_frequency_for(d, rec, head, "HEAD"))

    def test_non_numeric_value_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            self._write(d, "sub-01/eeg/sub-01_task-rest_eeg.json", {"PowerLineFrequency": "n/a"})
            head = {"sub-01/eeg/sub-01_task-rest_eeg.json"}
            self.assertIsNone(power_line_frequency_for(d, rec, head, "HEAD"))

    def test_reads_via_git_when_no_working_tree(self):
        # The workflow clones --no-checkout, so the sidecar is only in the git
        # object store, not on disk. Resolution must fall back to `git cat-file`.
        sidecar = "sub-01/eeg/sub-01_task-rest_eeg.json"
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as clone_parent:
            self._write(src, sidecar, {"PowerLineFrequency": 60})
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            }
            def run(*a: str) -> None:
                subprocess.run(a, check=True, env=env, capture_output=True)

            run("git", "-C", src, "init", "-q", "-b", "main")
            run("git", "-C", src, "add", "-A")
            run("git", "-C", src, "commit", "-qm", "init")
            clone = os.path.join(clone_parent, "repo")
            run("git", "clone", "--no-checkout", "-q", src, clone)
            self.assertFalse(os.path.exists(os.path.join(clone, sidecar)))  # no working tree
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            self.assertEqual(
                power_line_frequency_for(clone, rec, {sidecar}, "HEAD"), 60.0
            )


class TestEmbedRootAttr(unittest.TestCase):
    def test_adds_attribute_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as d:
            store = os.path.join(d, "rec.zarr")
            os.makedirs(store)
            with open(os.path.join(store, "zarr.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "zarr_format": 3,
                        "node_type": "group",
                        "attributes": {"format": "biosigio-zarr", "channel_groups": ["eeg_250hz"]},
                    },
                    fh,
                )
            embed_root_attr(store, "power_line_frequency", 60.0)
            with open(os.path.join(store, "zarr.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["attributes"]["power_line_frequency"], 60.0)
            self.assertEqual(doc["attributes"]["channel_groups"], ["eeg_250hz"])  # preserved


class TestEmbedAttr(unittest.TestCase):
    """embed_attr writes into an arbitrary group zarr.json, not only the store root."""

    def _make_zarr_json(self, path: str, attrs: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"zarr_format": 3, "node_type": "group", "attributes": attrs}, fh)

    def test_writes_into_sub_group_zarr_json(self):
        with tempfile.TemporaryDirectory() as d:
            meta = os.path.join(d, "rec.zarr", "events", "zarr.json")
            self._make_zarr_json(meta, {"n_events": 42, "label_map": {}})
            embed_attr(meta, "value_descriptions", {"21": "stimulus - face"})
            with open(meta, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["attributes"]["value_descriptions"], {"21": "stimulus - face"})
            self.assertEqual(doc["attributes"]["n_events"], 42)  # preserved

    def test_creates_attributes_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            meta = os.path.join(d, "zarr.json")
            with open(meta, "w", encoding="utf-8") as fh:
                json.dump({"zarr_format": 3}, fh)
            embed_attr(meta, "my_key", "my_value")
            with open(meta, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["attributes"]["my_key"], "my_value")

    def test_embed_root_attr_delegates(self):
        """embed_root_attr must still work (it now delegates to embed_attr)."""
        with tempfile.TemporaryDirectory() as d:
            store = os.path.join(d, "rec.zarr")
            os.makedirs(store)
            with open(os.path.join(store, "zarr.json"), "w", encoding="utf-8") as fh:
                json.dump({"zarr_format": 3, "attributes": {"x": 1}}, fh)
            embed_root_attr(store, "power_line_frequency", 50.0)
            with open(os.path.join(store, "zarr.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["attributes"]["power_line_frequency"], 50.0)
            self.assertEqual(doc["attributes"]["x"], 1)


class TestEventDescriptionsFor(unittest.TestCase):
    def _write(self, root: str, rel: str, body: dict) -> None:
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(body, fh)

    def test_sibling_sidecar_wins_over_root(self):
        """Most-specific sidecar overrides less-specific one for the same code."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            # Root-level (less specific): code 1 -> "boundary event"
            self._write(d, "task-rest_events.json", {
                "value": {"Levels": {"1": "boundary event", "21": "generic face"}},
            })
            # Sibling (more specific): code 21 overrides; code 99 is new
            self._write(d, "sub-01/eeg/sub-01_task-rest_events.json", {
                "value": {"Levels": {"21": "stimulus - face", "99": "response"}},
            })
            head = {
                "task-rest_events.json",
                "sub-01/eeg/sub-01_task-rest_events.json",
            }
            result = event_descriptions_for(d, rec, head, "HEAD")
            self.assertEqual(result["21"], "stimulus - face")  # sibling wins
            self.assertEqual(result["1"], "boundary event")    # root carries over
            self.assertEqual(result["99"], "response")          # sibling-only code

    def test_inherited_from_root_when_no_sibling(self):
        """on007139 pattern: events.json at dataset root, no sibling in eeg dir."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-Flanker_eeg.set"
            self._write(d, "task-Flanker_events.json", {
                "value": {"Levels": {"1": "left arrow", "2": "right arrow"}},
            })
            head = {"task-Flanker_events.json"}
            result = event_descriptions_for(d, rec, head, "HEAD")
            self.assertEqual(result, {"1": "left arrow", "2": "right arrow"})

    def test_merge_levels_across_multiple_columns(self):
        """Codes from 'value' and 'trial_type' columns are both captured."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-x_eeg.set"
            self._write(d, "sub-01/eeg/sub-01_task-x_events.json", {
                "value": {"Levels": {"21": "stimulus - face"}},
                "trial_type": {"Levels": {"go": "go trial", "nogo": "no-go trial"}},
            })
            head = {"sub-01/eeg/sub-01_task-x_events.json"}
            result = event_descriptions_for(d, rec, head, "HEAD")
            self.assertIn("21", result)
            self.assertIn("go", result)
            self.assertIn("nogo", result)

    def test_non_subset_entities_not_applied(self):
        """A sidecar for a different task must not apply to this recording."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            self._write(d, "sub-01/eeg/sub-01_task-other_events.json", {
                "value": {"Levels": {"10": "face"}},
            })
            head = {"sub-01/eeg/sub-01_task-other_events.json"}
            result = event_descriptions_for(d, rec, head, "HEAD")
            self.assertEqual(result, {})

    def test_absent_sidecar_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            result = event_descriptions_for(d, rec, set(), "HEAD")
            self.assertEqual(result, {})

    def test_non_string_values_ignored(self):
        """Levels entries with non-string key or value are skipped."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            # JSON keys are always strings, but values might be non-string
            self._write(d, "sub-01/eeg/sub-01_task-rest_events.json", {
                "value": {"Levels": {"21": 42, "22": None, "23": "valid"}},
            })
            head = {"sub-01/eeg/sub-01_task-rest_events.json"}
            result = event_descriptions_for(d, rec, head, "HEAD")
            self.assertEqual(result, {"23": "valid"})

    def test_empty_string_keys_and_values_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            self._write(d, "sub-01/eeg/sub-01_task-rest_events.json", {
                "value": {"Levels": {"": "empty key", "21": ""}},
            })
            head = {"sub-01/eeg/sub-01_task-rest_events.json"}
            result = event_descriptions_for(d, rec, head, "HEAD")
            self.assertEqual(result, {})

    def test_no_levels_field_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            self._write(d, "sub-01/eeg/sub-01_task-rest_events.json", {
                "value": {"Description": "the event value column", "Units": "n/a"},
            })
            head = {"sub-01/eeg/sub-01_task-rest_events.json"}
            result = event_descriptions_for(d, rec, head, "HEAD")
            self.assertEqual(result, {})

    def test_reads_via_git_when_no_working_tree(self):
        """Mirrors the PLF git test: clone --no-checkout, must use git cat-file."""
        sidecar = "sub-01/eeg/sub-01_task-rest_events.json"
        sidecar_body = {"value": {"Levels": {"10": "face", "20": "house"}}}
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as clone_parent:
            p = os.path.join(src, sidecar)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(sidecar_body, fh)
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            }

            def run(*a: str) -> None:
                subprocess.run(a, check=True, env=env, capture_output=True)

            run("git", "-C", src, "init", "-q", "-b", "main")
            run("git", "-C", src, "add", "-A")
            run("git", "-C", src, "commit", "-qm", "init")
            clone = os.path.join(clone_parent, "repo")
            run("git", "clone", "--no-checkout", "-q", src, clone)
            self.assertFalse(os.path.exists(os.path.join(clone, sidecar)))
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            result = event_descriptions_for(clone, rec, {sidecar}, "HEAD")
            self.assertEqual(result, {"10": "face", "20": "house"})


class TestElectrodePositionsFor(unittest.TestCase):
    """Tests for electrode_positions_for -- TSV parsing, BIDS inheritance, coordsystem."""

    def _write(self, root: str, rel: str, body: str) -> None:
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)

    def _write_json(self, root: str, rel: str, body: dict) -> None:
        self._write(root, rel, json.dumps(body))

    def _tsv(self, *rows: tuple) -> str:
        return "\n".join("\t".join(str(c) for c in row) for row in rows) + "\n"

    # -- TSV parsing -----------------------------------------------------------

    def test_standard_tsv_parses_positions(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("FP1", "80.784", "26.133", "-4.001"),
                ("FP2", "-80.784", "26.133", "-4.001"),
            )
            self._write(d, "sub-01/eeg/sub-01_task-rest_electrodes.tsv", tsv)
            head = {"sub-01/eeg/sub-01_task-rest_electrodes.tsv"}
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNotNone(result)
            self.assertAlmostEqual(result["positions"]["FP1"][0], 80.784)
            self.assertAlmostEqual(result["positions"]["FP2"][0], -80.784)

    def test_extra_columns_do_not_break_parsing(self):
        """Columns like type, impedance, status after z must be ignored."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-x_eeg.set"
            tsv = self._tsv(
                ("name", "x", "y", "z", "type", "impedance"),
                ("Fz", "1.0", "2.0", "3.0", "EEG", "5"),
                ("Cz", "0.0", "0.0", "4.0", "EEG", "n/a"),
            )
            self._write(d, "sub-01/eeg/sub-01_task-x_electrodes.tsv", tsv)
            head = {"sub-01/eeg/sub-01_task-x_electrodes.tsv"}
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNotNone(result)
            self.assertIn("Fz", result["positions"])
            self.assertIn("Cz", result["positions"])
            self.assertEqual(result["positions"]["Fz"], [1.0, 2.0, 3.0])

    def test_non_standard_column_order(self):
        """z before y before x order must still work."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_eeg.set"
            tsv = self._tsv(
                ("z", "name", "y", "x"),
                ("9.0", "Oz", "0.0", "0.0"),
            )
            self._write(d, "sub-01/eeg/sub-01_electrodes.tsv", tsv)
            head = {"sub-01/eeg/sub-01_electrodes.tsv"}
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNotNone(result)
            self.assertEqual(result["positions"]["Oz"], [0.0, 0.0, 9.0])

    def test_na_rows_skipped(self):
        """Rows where x, y, or z is 'n/a' (case-insensitive) must be skipped."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_eeg.set"
            tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("FP1", "80.0", "26.0", "n/a"),
                ("FP2", "N/A", "26.0", "-4.0"),
                ("Cz", "0.0", "0.0", "88.0"),
            )
            self._write(d, "sub-01/eeg/sub-01_electrodes.tsv", tsv)
            head = {"sub-01/eeg/sub-01_electrodes.tsv"}
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNotNone(result)
            self.assertNotIn("FP1", result["positions"])
            self.assertNotIn("FP2", result["positions"])
            self.assertIn("Cz", result["positions"])

    def test_non_numeric_rows_skipped(self):
        """Rows where x/y/z cannot be parsed as float must be skipped."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_eeg.set"
            tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("REF", "unknown", "0.0", "0.0"),
                ("Cz", "0.0", "0.0", "88.0"),
            )
            self._write(d, "sub-01/eeg/sub-01_electrodes.tsv", tsv)
            head = {"sub-01/eeg/sub-01_electrodes.tsv"}
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNotNone(result)
            self.assertNotIn("REF", result["positions"])
            self.assertIn("Cz", result["positions"])

    def test_missing_name_xyz_columns_returns_none(self):
        """A TSV without a 'name' or 'x'/'y'/'z' column must return None."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_eeg.set"
            tsv = self._tsv(
                ("label", "lat", "lon"),
                ("FP1", "10.0", "20.0"),
            )
            self._write(d, "sub-01/eeg/sub-01_electrodes.tsv", tsv)
            head = {"sub-01/eeg/sub-01_electrodes.tsv"}
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNone(result)

    def test_all_rows_skipped_returns_none(self):
        """If all data rows are invalid (all n/a), return None."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_eeg.set"
            tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("FP1", "n/a", "n/a", "n/a"),
            )
            self._write(d, "sub-01/eeg/sub-01_electrodes.tsv", tsv)
            head = {"sub-01/eeg/sub-01_electrodes.tsv"}
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNone(result)

    # -- BIDS inheritance ------------------------------------------------------

    def test_absent_electrodes_tsv_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            result = electrode_positions_for(d, rec, set(), "HEAD")
            self.assertIsNone(result)

    def test_sibling_beats_root(self):
        """More-specific sibling must win over a root-level electrodes.tsv."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            root_tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("FP1", "1.0", "2.0", "3.0"),
            )
            sibling_tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("FP1", "80.0", "26.0", "-4.0"),
            )
            self._write(d, "electrodes.tsv", root_tsv)
            self._write(d, "sub-01/eeg/sub-01_task-rest_electrodes.tsv", sibling_tsv)
            head = {
                "electrodes.tsv",
                "sub-01/eeg/sub-01_task-rest_electrodes.tsv",
            }
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNotNone(result)
            self.assertAlmostEqual(result["positions"]["FP1"][0], 80.0)

    def test_root_only_inheritance(self):
        """When only a root-level electrodes.tsv exists, it must be used."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("FP1", "80.0", "26.0", "-4.0"),
            )
            self._write(d, "electrodes.tsv", tsv)
            head = {"electrodes.tsv"}
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNotNone(result)
            self.assertIn("FP1", result["positions"])

    def test_non_subset_entities_not_applied(self):
        """An electrodes.tsv for a different task must not apply."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("Cz", "0.0", "0.0", "88.0"),
            )
            self._write(d, "sub-01/eeg/sub-01_task-other_electrodes.tsv", tsv)
            head = {"sub-01/eeg/sub-01_task-other_electrodes.tsv"}
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNone(result)

    # -- coordsystem.json ------------------------------------------------------

    def test_coordsystem_units_and_system_extracted(self):
        """EEGCoordinateSystem and EEGCoordinateUnits must appear in the result."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("FP1", "80.0", "26.0", "-4.0"),
            )
            self._write(d, "sub-01/eeg/sub-01_task-rest_electrodes.tsv", tsv)
            self._write_json(d, "sub-01/eeg/sub-01_task-rest_coordsystem.json", {
                "EEGCoordinateSystem": "EEGLAB",
                "EEGCoordinateUnits": "mm",
            })
            head = {
                "sub-01/eeg/sub-01_task-rest_electrodes.tsv",
                "sub-01/eeg/sub-01_task-rest_coordsystem.json",
            }
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNotNone(result)
            self.assertEqual(result["coordinate_system"], "EEGLAB")
            self.assertEqual(result["coordinate_units"], "mm")

    def test_absent_coordsystem_gives_empty_strings(self):
        """When no coordsystem.json resolves, both strings must be empty."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/eeg/sub-01_eeg.set"
            tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("Cz", "0.0", "0.0", "88.0"),
            )
            self._write(d, "sub-01/eeg/sub-01_electrodes.tsv", tsv)
            head = {"sub-01/eeg/sub-01_electrodes.tsv"}
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNotNone(result)
            self.assertEqual(result["coordinate_system"], "")
            self.assertEqual(result["coordinate_units"], "")

    def test_ieeg_coordsystem_keys_extracted(self):
        """iEEGCoordinateSystem/iEEGCoordinateUnits must also be read."""
        with tempfile.TemporaryDirectory() as d:
            rec = "sub-01/ieeg/sub-01_task-rest_ieeg.set"
            tsv = self._tsv(
                ("name", "x", "y", "z"),
                ("A1", "10.0", "20.0", "30.0"),
            )
            self._write(d, "sub-01/ieeg/sub-01_task-rest_electrodes.tsv", tsv)
            self._write_json(d, "sub-01/ieeg/sub-01_task-rest_coordsystem.json", {
                "iEEGCoordinateSystem": "Talairach",
                "iEEGCoordinateUnits": "mm",
            })
            head = {
                "sub-01/ieeg/sub-01_task-rest_electrodes.tsv",
                "sub-01/ieeg/sub-01_task-rest_coordsystem.json",
            }
            result = electrode_positions_for(d, rec, head, "HEAD")
            self.assertIsNotNone(result)
            self.assertEqual(result["coordinate_system"], "Talairach")
            self.assertEqual(result["coordinate_units"], "mm")

    # -- embed onto root -------------------------------------------------------

    def test_embed_electrode_attrs_onto_root(self):
        """The three attrs land on the root zarr.json and preserve existing attrs."""
        with tempfile.TemporaryDirectory() as d:
            store = os.path.join(d, "rec.zarr")
            os.makedirs(store)
            with open(os.path.join(store, "zarr.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "zarr_format": 3,
                        "node_type": "group",
                        "attributes": {
                            "format": "biosigio-zarr",
                            "channel_groups": ["eeg_250hz"],
                            "power_line_frequency": 60.0,
                        },
                    },
                    fh,
                )
            positions = {"FP1": [80.0, 26.0, -4.0], "FP2": [-80.0, 26.0, -4.0]}
            embed_root_attr(store, "electrode_positions", positions)
            embed_root_attr(store, "electrode_coordinate_system", "EEGLAB")
            embed_root_attr(store, "electrode_coordinate_units", "mm")
            with open(os.path.join(store, "zarr.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            attrs = doc["attributes"]
            self.assertEqual(attrs["electrode_positions"], positions)
            self.assertEqual(attrs["electrode_coordinate_system"], "EEGLAB")
            self.assertEqual(attrs["electrode_coordinate_units"], "mm")
            self.assertEqual(attrs["power_line_frequency"], 60.0)  # preserved
            self.assertEqual(attrs["channel_groups"], ["eeg_250hz"])  # preserved

    # -- git cat-file fallback (no-checkout clone) -----------------------------

    def test_reads_via_git_when_no_working_tree(self):
        """The workflow clones --no-checkout; must resolve via git cat-file."""
        elec_rel = "sub-01/eeg/sub-01_task-rest_electrodes.tsv"
        cs_rel = "sub-01/eeg/sub-01_task-rest_coordsystem.json"
        tsv_body = "name\tx\ty\tz\nFP1\t80.784\t26.133\t-4.001\n"
        cs_body = json.dumps({"EEGCoordinateSystem": "EEGLAB", "EEGCoordinateUnits": "mm"})
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as clone_parent:
            for rel, body in ((elec_rel, tsv_body), (cs_rel, cs_body)):
                p = os.path.join(src, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(body)
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            }

            def run(*a: str) -> None:
                subprocess.run(a, check=True, env=env, capture_output=True)

            run("git", "-C", src, "init", "-q", "-b", "main")
            run("git", "-C", src, "add", "-A")
            run("git", "-C", src, "commit", "-qm", "init")
            clone = os.path.join(clone_parent, "repo")
            run("git", "clone", "--no-checkout", "-q", src, clone)
            # Confirm no working tree
            self.assertFalse(os.path.exists(os.path.join(clone, elec_rel)))
            rec = "sub-01/eeg/sub-01_task-rest_eeg.set"
            result = electrode_positions_for(clone, rec, {elec_rel, cs_rel}, "HEAD")
            self.assertIsNotNone(result)
            self.assertAlmostEqual(result["positions"]["FP1"][0], 80.784)
            self.assertEqual(result["coordinate_system"], "EEGLAB")
            self.assertEqual(result["coordinate_units"], "mm")


class TestKitAndCtf(unittest.TestCase):
    """KIT `.con`/`.sqd`/`.kdf` files and CTF `.ds` directory recordings."""

    def test_kit_extensions_are_primary(self):
        for ext in (".con", ".sqd", ".kdf"):
            self.assertTrue(is_primary(f"sub-01/meg/sub-01_task-x_meg{ext}"))
        # And map to MEG by their BIDS suffix.
        self.assertEqual(bids_suffix_modality("sub-01/meg/sub-01_task-x_meg.con"), "MEG")
        self.assertEqual(
            store_rel_for("sub-01/meg/sub-01_task-x_meg.con"),
            "sub-01/meg/sub-01_task-x_meg.zarr",
        )

    def test_ctf_ds_of_and_is_ctf_ds(self):
        inner = "sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.meg4"
        ds = "sub-01/meg/sub-01_task-x_meg.ds"
        self.assertEqual(ctf_ds_of(inner), ds)
        self.assertEqual(ctf_ds_of(ds), ds)  # the dir maps to itself
        self.assertIsNone(ctf_ds_of("sub-01/meg/sub-01_task-x_meg.fif"))
        self.assertTrue(is_ctf_ds(ds))
        self.assertFalse(is_ctf_ds(inner))

    def test_ctf_ds_recordings_derived_from_inner_files(self):
        head = [
            "sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.meg4",
            "sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.res4",
            "sub-01/meg/sub-01_task-x_meg.ds/BadChannels",
            "sub-02/meg/sub-02_task-y_meg.ds/sub-02_task-y_meg.meg4",
            "dataset_description.json",
        ]
        self.assertEqual(
            ctf_ds_recordings(head),
            {"sub-01/meg/sub-01_task-x_meg.ds", "sub-02/meg/sub-02_task-y_meg.ds"},
        )

    def test_ctf_store_rel_and_events_and_modality(self):
        ds = "sub-01/meg/sub-01_task-x_meg.ds"
        self.assertEqual(store_rel_for(ds), "sub-01/meg/sub-01_task-x_meg.zarr")
        self.assertEqual(
            events_sibling_for(ds), "sub-01/meg/sub-01_task-x_events.tsv"
        )
        self.assertEqual(bids_suffix_modality(ds), "MEG")

    def test_full_converts_ctf_ds_as_one_primary(self):
        head = [
            "sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.meg4",
            "sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.res4",
            "sub-01/meg/sub-01_task-x_events.tsv",
        ]
        convert, remove = compute_worklist(head, [], full=True)
        self.assertEqual(convert, ["sub-01/meg/sub-01_task-x_meg.ds"])
        self.assertEqual(remove, [])

    def test_modify_inner_ctf_file_rebuilds_recording(self):
        head = [
            "sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.meg4",
            "sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.res4",
        ]
        convert, remove = compute_worklist(
            head, [("M", "sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.meg4")], full=False
        )
        self.assertEqual(convert, ["sub-01/meg/sub-01_task-x_meg.ds"])
        self.assertEqual(remove, [])

    def test_delete_whole_ctf_ds_removes_store(self):
        # Every inner file deleted, none remain at HEAD -> drop the recording's store.
        convert, remove = compute_worklist(
            [],
            [
                ("D", "sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.meg4"),
                ("D", "sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.res4"),
            ],
            full=False,
        )
        self.assertEqual(convert, [])
        self.assertEqual(remove, ["sub-01/meg/sub-01_task-x_meg.zarr"])

    def test_delete_one_ctf_file_with_others_remaining_rebuilds(self):
        head = ["sub-01/meg/sub-01_task-x_meg.ds/sub-01_task-x_meg.meg4"]
        convert, remove = compute_worklist(
            head, [("D", "sub-01/meg/sub-01_task-x_meg.ds/BadChannels")], full=False
        )
        self.assertEqual(convert, ["sub-01/meg/sub-01_task-x_meg.ds"])
        self.assertEqual(remove, [])

    def test_ctf_size_sums_directory_tree(self):
        with tempfile.TemporaryDirectory() as d:
            ds = os.path.join(d, "sub-01_task-x_meg.ds")
            os.makedirs(ds)
            with open(os.path.join(ds, "sub-01_task-x_meg.meg4"), "wb") as fh:
                fh.write(b"m" * 8000)
            with open(os.path.join(ds, "sub-01_task-x_meg.res4"), "wb") as fh:
                fh.write(b"r" * 200)
            self.assertEqual(_recording_size_bytes(ds), 8200)


class TestRecordingSizeBytes(unittest.TestCase):
    """Streaming gate sizing: primary + same-stem companions, not the whole dir."""

    def test_sums_primary_and_same_stem_companions(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "sub-01", "ieeg")
            os.makedirs(sub)
            stem = "sub-01_task-movie_ieeg"
            # BrainVision triplet: the bulk lives in the .eeg companion.
            with open(os.path.join(sub, f"{stem}.vhdr"), "wb") as fh:
                fh.write(b"x" * 100)
            with open(os.path.join(sub, f"{stem}.eeg"), "wb") as fh:
                fh.write(b"y" * 5000)
            with open(os.path.join(sub, f"{stem}.vmrk"), "wb") as fh:
                fh.write(b"z" * 50)
            # A different recording in the same dir must NOT be counted.
            with open(os.path.join(sub, "sub-01_task-rest_ieeg.eeg"), "wb") as fh:
                fh.write(b"q" * 9999)
            primary = os.path.join(sub, f"{stem}.vhdr")
            self.assertEqual(_recording_size_bytes(primary), 100 + 5000 + 50)

    def test_unreadable_dir_forces_streaming(self):
        # A listdir failure must NOT read as size 0 (which would misroute a large
        # recording to the OOM-prone in-memory path); it forces the streaming path.
        self.assertGreater(
            _recording_size_bytes("/no/such/dir/sub-01_eeg.vhdr"), 2 * 1024**3
        )

    def test_split_fif_sums_the_whole_chain(self):
        # #909: split-02.. carry DIFFERENT stems (`_split-02_meg`); summing only
        # split-01's same-stem companions undercounts the chain, so should_stream
        # misroutes a multi-GB recording to the in-memory path and OOMs. The size
        # must be the whole split group, not just split-01.
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "sub-01", "meg")
            os.makedirs(sub)
            base = "sub-01_task-rest"
            for idx, size in ((1, 1000), (2, 2000), (3, 500)):
                with open(os.path.join(sub, f"{base}_split-0{idx}_meg.fif"), "wb") as fh:
                    fh.write(b"f" * size)
            # A non-member file in the same dir must NOT be counted.
            with open(os.path.join(sub, "sub-01_task-other_meg.fif"), "wb") as fh:
                fh.write(b"x" * 9999)
            primary = os.path.join(sub, f"{base}_split-01_meg.fif")
            self.assertEqual(_recording_size_bytes(primary), 3500)


class TestMemoryGuard(unittest.TestCase):
    """Per-recording memory guard (#909): projection, budget, deterministic skip."""

    def test_projected_peak_streaming_is_bounded(self):
        # A large .fif streams -> peak is bounded regardless of on-disk size.
        self.assertEqual(
            projected_peak_bytes("sub-01_task-x_meg.fif", 50 * 1024**3), STREAM_PEAK_BYTES
        )

    def test_projected_peak_inmemory_scales_with_size(self):
        # An EDF has no streaming reader -> float64 blow-up scales with size.
        size = 3 * 1024**3
        self.assertEqual(
            projected_peak_bytes("sub-01_task-x_eeg.edf", size), int(size * INMEM_MEM_FACTOR)
        )

    def test_budget_scales_with_jobs(self):
        os.environ.pop("ZARR_REC_MEM_BUDGET_BYTES", None)
        serial = recording_mem_budget_bytes(1)
        parallel = recording_mem_budget_bytes(8)
        # Serial gets the whole node; 8-way gets ~1/8 -> a large in-memory recording
        # the cron skips is convertible on a `--jobs 1` re-run.
        self.assertEqual(parallel, serial // 8)
        self.assertGreater(serial, parallel)

    def test_budget_explicit_override_wins(self):
        os.environ["ZARR_REC_MEM_BUDGET_BYTES"] = str(123 * 1024**2)
        try:
            self.assertEqual(recording_mem_budget_bytes(4), 123 * 1024**2)
        finally:
            os.environ.pop("ZARR_REC_MEM_BUDGET_BYTES", None)

    def test_preflight_skips_oversized_with_deterministic_code(self):
        # A tiny budget forces a skip BEFORE any load (no biosigIO import, no OOM);
        # the exception carries the code so it surfaces as a deterministic skip.
        with tempfile.TemporaryDirectory() as d:
            rec = os.path.join(d, "sub-01_task-x_eeg.edf")
            with open(rec, "wb") as fh:
                fh.write(b"e" * 100_000)
            with self.assertRaises(RecordingTooLarge) as cm:
                convert_recording(rec, None, os.path.join(d, "store"), mem_budget_bytes=1)
            self.assertEqual(cm.exception.code, "recording_too_large")

    def test_reason_for_code_too_large_is_user_facing(self):
        self.assertIn("too large", reason_for_code("recording_too_large").lower())


class TestSplitFif(unittest.TestCase):
    """Multi-file FIF split recordings collapse to one head store."""

    def test_split_index_and_group_key(self):
        p1 = "sub-03/meg/sub-03_task-x_run-02_split-01_meg.fif"
        p2 = "sub-03/meg/sub-03_task-x_run-02_split-02_meg.fif"
        self.assertEqual(split_index(p1), 1)
        self.assertEqual(split_index(p2), 2)
        self.assertIsNone(split_index("sub-03/meg/sub-03_task-x_run-02_meg.fif"))
        # Both splits resolve to the same group key (split entity removed).
        self.assertEqual(split_group_key(p1), "sub-03/meg/sub-03_task-x_run-02_meg.fif")
        self.assertEqual(split_group_key(p1), split_group_key(p2))

    def test_is_split_fif_only_true_for_fif_with_split(self):
        self.assertTrue(is_split_fif("sub-03/meg/sub-03_task-x_split-01_meg.fif"))
        self.assertFalse(is_split_fif("sub-03/meg/sub-03_task-x_meg.fif"))  # no split
        # A `split-` entity on a non-FIF format is not part of the FIF chain logic.
        self.assertFalse(is_split_fif("sub-03/eeg/sub-03_task-x_split-01_eeg.set"))

    def test_heads_and_members_picks_lowest_split(self):
        primaries = [
            "sub-03/meg/sub-03_task-x_split-01_meg.fif",
            "sub-03/meg/sub-03_task-x_split-02_meg.fif",
            "sub-03/meg/sub-03_task-x_split-03_meg.fif",
            "sub-04/eeg/sub-04_task-y_eeg.set",  # non-split primary, carried verbatim
        ]
        heads, member_to_head = split_heads_and_members(primaries)
        self.assertEqual(
            heads,
            {
                "sub-03/meg/sub-03_task-x_split-01_meg.fif",
                "sub-04/eeg/sub-04_task-y_eeg.set",
            },
        )
        self.assertEqual(
            member_to_head,
            {
                "sub-03/meg/sub-03_task-x_split-02_meg.fif": "sub-03/meg/sub-03_task-x_split-01_meg.fif",
                "sub-03/meg/sub-03_task-x_split-03_meg.fif": "sub-03/meg/sub-03_task-x_split-01_meg.fif",
            },
        )

    def test_heads_picks_lowest_present_when_split01_absent(self):
        # Degenerate group missing split-01: lowest present split is the head.
        primaries = [
            "sub-03/meg/sub-03_task-x_split-02_meg.fif",
            "sub-03/meg/sub-03_task-x_split-03_meg.fif",
        ]
        heads, member_to_head = split_heads_and_members(primaries)
        self.assertEqual(heads, {"sub-03/meg/sub-03_task-x_split-02_meg.fif"})
        self.assertEqual(
            member_to_head,
            {"sub-03/meg/sub-03_task-x_split-03_meg.fif": "sub-03/meg/sub-03_task-x_split-02_meg.fif"},
        )

    def test_split_members_for_returns_sorted_chain(self):
        head_files = {
            "sub-03/meg/sub-03_task-x_split-02_meg.fif",
            "sub-03/meg/sub-03_task-x_split-01_meg.fif",
            "sub-03/meg/sub-03_task-x_events.tsv",
            "sub-04/eeg/sub-04_task-y_eeg.set",
        }
        members = split_members_for("sub-03/meg/sub-03_task-x_split-01_meg.fif", head_files)
        self.assertEqual(
            members,
            [
                "sub-03/meg/sub-03_task-x_split-01_meg.fif",
                "sub-03/meg/sub-03_task-x_split-02_meg.fif",
            ],
        )
        # A non-split primary has no members.
        self.assertEqual(split_members_for("sub-04/eeg/sub-04_task-y_eeg.set", head_files), [])

    def test_full_converts_only_head_split(self):
        head = [
            "sub-03/meg/sub-03_task-x_split-01_meg.fif",
            "sub-03/meg/sub-03_task-x_split-02_meg.fif",
            "sub-03/meg/sub-03_task-x_events.tsv",
        ]
        convert, remove = compute_worklist(head, [], full=True)
        self.assertEqual(convert, ["sub-03/meg/sub-03_task-x_split-01_meg.fif"])
        self.assertEqual(remove, [])

    def test_modify_any_split_rebuilds_head(self):
        head = [
            "sub-03/meg/sub-03_task-x_split-01_meg.fif",
            "sub-03/meg/sub-03_task-x_split-02_meg.fif",
        ]
        for changed in (
            "sub-03/meg/sub-03_task-x_split-01_meg.fif",
            "sub-03/meg/sub-03_task-x_split-02_meg.fif",
        ):
            convert, remove = compute_worklist(head, [("M", changed)], full=False)
            self.assertEqual(convert, ["sub-03/meg/sub-03_task-x_split-01_meg.fif"])
            self.assertEqual(remove, [])

    def test_events_change_rebuilds_split_head(self):
        head = [
            "sub-03/meg/sub-03_task-x_split-01_meg.fif",
            "sub-03/meg/sub-03_task-x_split-02_meg.fif",
            "sub-03/meg/sub-03_task-x_events.tsv",
        ]
        convert, _ = compute_worklist(
            head, [("M", "sub-03/meg/sub-03_task-x_events.tsv")], full=False
        )
        self.assertEqual(convert, ["sub-03/meg/sub-03_task-x_split-01_meg.fif"])

    def test_delete_non_head_split_rebuilds_head_not_remove(self):
        # split-02 removed but split-01 remains -> re-read the chain, no store drop.
        head = ["sub-03/meg/sub-03_task-x_split-01_meg.fif"]
        convert, remove = compute_worklist(
            head, [("D", "sub-03/meg/sub-03_task-x_split-02_meg.fif")], full=False
        )
        self.assertEqual(convert, ["sub-03/meg/sub-03_task-x_split-01_meg.fif"])
        self.assertEqual(remove, [])

    def test_delete_head_split_removes_its_store(self):
        # The whole recording is gone (both splits deleted) -> drop the head store.
        head: list[str] = []
        convert, remove = compute_worklist(
            head,
            [
                ("D", "sub-03/meg/sub-03_task-x_split-01_meg.fif"),
                ("D", "sub-03/meg/sub-03_task-x_split-02_meg.fif"),
            ],
            full=False,
        )
        self.assertEqual(convert, [])
        self.assertEqual(remove, ["sub-03/meg/sub-03_task-x_split-01_meg.zarr"])

    def test_head_split_reindex_drops_orphaned_old_store(self):
        # Old head split-01 deleted while split-02 survives as the new head: build the
        # new head store AND remove the orphaned old-head store (would otherwise linger).
        head = ["sub-03/meg/sub-03_task-x_split-02_meg.fif"]
        convert, remove = compute_worklist(
            head, [("D", "sub-03/meg/sub-03_task-x_split-01_meg.fif")], full=False
        )
        self.assertEqual(convert, ["sub-03/meg/sub-03_task-x_split-02_meg.fif"])
        self.assertEqual(remove, ["sub-03/meg/sub-03_task-x_split-01_meg.zarr"])

    def test_affected_primaries_non_head_split_maps_to_head(self):
        primaries = ["sub-03/meg/sub-03_task-x_split-01_meg.fif"]
        bd = by_dir(primaries)
        m2h = {
            "sub-03/meg/sub-03_task-x_split-02_meg.fif": "sub-03/meg/sub-03_task-x_split-01_meg.fif"
        }
        self.assertEqual(
            affected_primaries("sub-03/meg/sub-03_task-x_split-02_meg.fif", bd, m2h),
            {"sub-03/meg/sub-03_task-x_split-01_meg.fif"},
        )


class TestAnnexKeySize(unittest.TestCase):
    """The `-s<N>` size the download integrity check verifies a blob against."""

    def test_sha256e_size(self):
        self.assertEqual(
            annex_key_size("SHA256E-s628291820--64135e784fc1.con"), 628291820
        )

    def test_md5e_size(self):
        self.assertEqual(annex_key_size("MD5E-s12345--abcdef.edf"), 12345)

    def test_large_eeg_size(self):
        # The kind of >10 GB BrainVision blob that truncates without the check.
        self.assertEqual(
            annex_key_size("SHA256E-s12582746496--7ee1f5a2.eeg"), 12582746496
        )

    def test_no_size_field_returns_none(self):
        self.assertIsNone(annex_key_size("URL--https://example.org/x.edf"))
        self.assertIsNone(annex_key_size("WORM--whatever"))
        self.assertIsNone(annex_key_size(None))
        self.assertIsNone(annex_key_size(""))


class TestFailureReasons(unittest.TestCase):
    """Typed data failures (recordings the viewer should explain) are carried into
    index.json `failures`; infra failures are not. Mirrors biosigIO's error codes."""

    def test_reason_for_code_known_and_unknown(self):
        # Known codes get specific copy; None/unknown get the generic fallback.
        self.assertIn("derivative", reason_for_code("not_continuous").lower())
        self.assertIn("truncated", reason_for_code("corrupt_or_truncated").lower())
        generic = reason_for_code(None)
        self.assertEqual(reason_for_code("some_future_code"), generic)
        self.assertTrue(generic)

    def test_merge_index_records_failures(self):
        index = merge_index(
            None, "nm000104", "sha", [{"zarr": "a_eeg.zarr", "path": "a_eeg.set"}], [],
            "2026-06-13T00:00:00Z",
            [{"path": "b-ave.fif", "zarr": "b-ave.zarr", "code": "not_continuous",
              "reason": "derivative"}],
        )
        self.assertEqual(index["store_count"], 1)
        self.assertEqual(index["failure_count"], 1)
        self.assertEqual(index["failures"][0]["code"], "not_continuous")
        self.assertEqual(index["failures"][0]["path"], "b-ave.fif")

    def test_merge_index_failure_clears_when_path_converts(self):
        # A path that failed before but converts now drops out of `failures`.
        prior = {
            "source_commit": "old",
            "stores": [],
            "failures": [{"path": "x_eeg.set", "zarr": "x_eeg.zarr",
                          "code": "corrupt_or_truncated", "reason": "..."}],
        }
        index = merge_index(
            prior, "nm000104", "new", [{"zarr": "x_eeg.zarr", "path": "x_eeg.set"}], [],
            "2026-06-13T00:00:00Z", [],
        )
        self.assertEqual(index["failure_count"], 0)
        self.assertEqual(index["store_count"], 1)

    def test_merge_index_path_never_in_both_stores_and_failures(self):
        # A recording that newly fails drops its stale store entry.
        prior = {
            "source_commit": "old",
            "stores": [{"zarr": "x_eeg.zarr", "path": "x_eeg.set"}],
            "failures": [],
        }
        index = merge_index(
            prior, "nm000104", "new", [], [], "2026-06-13T00:00:00Z",
            [{"path": "x_eeg.set", "zarr": "x_eeg.zarr", "code": "not_continuous",
              "reason": "..."}],
        )
        store_paths = {s.get("path") for s in index["stores"]}
        fail_paths = {f["path"] for f in index["failures"]}
        self.assertEqual(store_paths & fail_paths, set())
        self.assertEqual(index["failure_count"], 1)
        self.assertEqual(index["store_count"], 0)

    def test_merge_index_drops_failure_for_removed_store(self):
        prior = {
            "source_commit": "old", "stores": [],
            "failures": [{"path": "gone_eeg.set", "zarr": "gone_eeg.zarr",
                          "code": "not_continuous", "reason": "..."}],
        }
        index = merge_index(
            prior, "nm000104", "new", [], ["gone_eeg.zarr"], "2026-06-13T00:00:00Z", [],
        )
        self.assertEqual(index["failure_count"], 0)


class TestAwsRunner(unittest.TestCase):
    """The wall-clock timeout + retry that stops a wedged aws op from hanging a
    worker -- or the whole run -- forever (the 2.5 h `aws s3 rm` spin on an empty
    prefix). Real subprocesses, no mocks; python3 stands in for `aws` (the
    appended --cli-* flags are harmlessly absorbed as argv)."""

    def test_timeout_kills_wedged_command(self):
        import time as _t

        start = _t.monotonic()
        with self.assertRaises(RuntimeError):
            # Sleeps 30 s; the 1 s wall-clock cap must kill it well before that.
            _aws([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1, retries=1)
        self.assertLess(_t.monotonic() - start, 10)  # killed, not run to completion

    def test_failing_command_retries_then_raises(self):
        with self.assertRaises(RuntimeError):
            _aws([sys.executable, "-c", "import sys; sys.exit(7)"], timeout=30, retries=2)

    def test_recursive_rm_timeout_far_exceeds_transfer_timeout(self):
        # A whole-prefix `aws s3 rm --recursive` (millions of chunk objects on a
        # big dataset) legitimately runs much longer than a single transfer; the
        # transfer cap was killing real wipes mid-delete.
        self.assertGreaterEqual(_AWS_RM_TIMEOUT, 4 * _AWS_OP_TIMEOUT)


class TestShouldStream(unittest.TestCase):
    """Which recordings take the bounded-memory streaming path. KIT .con loads
    fully in memory (~5x float64) and OOMs a worker well below the multi-GB mark,
    so it streams at a much lower threshold than BrainVision/FIF/CTF."""

    GB = 1024**3
    MB = 1024**2

    def test_kit_con_streams_above_low_threshold(self):
        # The ~620 MB task-2 .con that OOM'd the in-memory path must stream.
        self.assertTrue(should_stream("sub-01/meg/sub-01_task-x_meg.con", 620 * self.MB))
        self.assertTrue(should_stream("sub-01/meg/sub-01_task-x_meg.sqd", 900 * self.MB))

    def test_small_kit_stays_in_memory(self):
        # A small (~190 MB task-0) .con is cheap in memory -> faster path.
        self.assertFalse(should_stream("sub-01/meg/sub-01_task-x_meg.con", 190 * self.MB))

    def test_brainvision_fif_keep_multigb_threshold(self):
        # MNE-native formats only stream when genuinely large (the in-memory path
        # is fine for moderate sizes).
        self.assertFalse(should_stream("sub-01/ieeg/sub-01_task-x_ieeg.vhdr", 500 * self.MB))
        self.assertTrue(should_stream("sub-01/ieeg/sub-01_task-x_ieeg.vhdr", 3 * self.GB))
        self.assertTrue(should_stream("sub-01/meg/sub-01_task-x_meg.fif", 3 * self.GB))

    def test_eeglab_set_never_streams(self):
        # EEGLAB .set has no streaming reader -> always in-memory.
        self.assertFalse(should_stream("sub-01/eeg/sub-01_task-x_eeg.set", 5 * self.GB))

    def test_edf_streaming_gated_on_biosigio_capability(self):
        # EDF/BDF stream ONLY when the installed biosigIO does it via pyedflib
        # (>=1.2.0, #944); on an older lib they stay in-memory (MNE would rescale
        # EDF units and not match the in-memory path). The gate is a module global.
        import generate_zarr  # type: ignore[import-not-found]

        big = STREAM_EDF_MIN_BYTES + 1
        orig = generate_zarr._EDF_STREAMABLE
        try:
            generate_zarr._EDF_STREAMABLE = True
            self.assertTrue(should_stream("sub-01/eeg/sub-01_task-x_eeg.edf", big))
            self.assertTrue(should_stream("sub-01/emg/sub-01_task-x_emg.bdf", big))
            # A small EDF stays on the faster in-memory path.
            self.assertFalse(
                should_stream("sub-01/eeg/sub-01_task-x_eeg.edf", STREAM_EDF_MIN_BYTES - 1)
            )
            generate_zarr._EDF_STREAMABLE = False
            self.assertFalse(should_stream("sub-01/eeg/sub-01_task-x_eeg.edf", big))
            self.assertFalse(should_stream("sub-01/emg/sub-01_task-x_emg.bdf", big))
        finally:
            generate_zarr._EDF_STREAMABLE = orig


if __name__ == "__main__":
    unittest.main()
