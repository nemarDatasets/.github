#!/usr/bin/env python3
"""Unit tests for the SQLite conversion queue (scripts/zarr/zarr_queue.py).

Real SQLite (a temp db per test), no mocks: exercises the enqueue / claim /
done / fail transitions, retry-backoff, version-bump requeue, and the crash
recovery (stale `inprogress` -> `pending`). The HTTP fetch is not tested here
(it hits the live API, validated by the cron run).

Run: python3 scripts/zarr/test_zarr_queue.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zarr_queue import (  # type: ignore[import-not-found]  # noqa: E402
    backoff_seconds,
    claim_next,
    connect,
    mark_done,
    mark_fail,
    reconcile,
)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(os.path.join(self._tmp.name, "q.db"))

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def status(self, dataset_id):
        r = self.conn.execute(
            "SELECT status FROM jobs WHERE dataset_id=?", (dataset_id,)
        ).fetchone()
        return r["status"] if r else None

    def test_reconcile_enqueues_new(self):
        res = reconcile(self.conn, [("nm000001", "1.0.0"), ("on000002", "1.0.0")], 3600)
        self.assertEqual(res["enqueued"], 2)
        self.assertEqual(self.status("nm000001"), "pending")

    def test_reconcile_filters_invalid_and_test_id(self):
        reconcile(self.conn, [("nm099999", "1"), ("bad", "1"), ("nm000001", "1.0.0")], 3600)
        ids = [r["dataset_id"] for r in self.conn.execute("SELECT dataset_id FROM jobs").fetchall()]
        self.assertEqual(ids, ["nm000001"])

    def test_done_then_reconcile_skips_same_version(self):
        reconcile(self.conn, [("nm000001", "1.0.0")], 3600)
        claim_next(self.conn)
        mark_done(self.conn, "nm000001", "1.0.0")
        res = reconcile(self.conn, [("nm000001", "1.0.0")], 3600)
        self.assertEqual(res["enqueued"], 0)
        self.assertEqual(self.status("nm000001"), "done")

    def test_version_bump_requeues_done(self):
        reconcile(self.conn, [("nm000001", "1.0.0")], 3600)
        claim_next(self.conn)
        mark_done(self.conn, "nm000001", "1.0.0")
        res = reconcile(self.conn, [("nm000001", "1.1.0")], 3600)
        self.assertEqual(res["enqueued"], 1)
        self.assertEqual(self.status("nm000001"), "pending")

    def test_claim_marks_inprogress_then_empty(self):
        reconcile(self.conn, [("nm000001", "1.0.0")], 3600)
        row = claim_next(self.conn)
        self.assertEqual(row["dataset_id"], "nm000001")
        self.assertEqual(self.status("nm000001"), "inprogress")
        self.assertIsNone(claim_next(self.conn))

    def test_claim_orders_by_enqueued(self):
        reconcile(self.conn, [("nm000002", "1"), ("nm000001", "1")], 3600)
        self.conn.execute("UPDATE jobs SET enqueued_at='2026-01-01T00:00:01Z' WHERE dataset_id='nm000002'")
        self.conn.execute("UPDATE jobs SET enqueued_at='2026-01-01T00:00:00Z' WHERE dataset_id='nm000001'")
        self.conn.commit()
        self.assertEqual(claim_next(self.conn)["dataset_id"], "nm000001")
        self.assertEqual(claim_next(self.conn)["dataset_id"], "nm000002")

    def test_fail_reschedules_with_backoff_then_terminal(self):
        reconcile(self.conn, [("nm000001", "1")], 3600)
        claim_next(self.conn)
        self.assertEqual(mark_fail(self.conn, "nm000001", "boom", 3, 1800), "pending")
        # in backoff -> not yet claimable
        self.assertIsNone(claim_next(self.conn))
        for expect in ("pending", "failed"):
            self.conn.execute("UPDATE jobs SET next_retry_at=0 WHERE dataset_id='nm000001'")
            self.conn.commit()
            claim_next(self.conn)
            self.assertEqual(mark_fail(self.conn, "nm000001", "boom", 3, 1800), expect)
        self.assertEqual(self.status("nm000001"), "failed")

    def test_stale_inprogress_recovered(self):
        reconcile(self.conn, [("nm000001", "1")], 3600)
        claim_next(self.conn)  # -> inprogress
        self.conn.execute(
            "UPDATE jobs SET updated_at=? WHERE dataset_id='nm000001'", (int(time.time()) - 99999,)
        )
        self.conn.commit()
        res = reconcile(self.conn, [("nm000001", "1")], 3600)
        self.assertEqual(res["recovered_stale"], 1)
        self.assertEqual(self.status("nm000001"), "pending")

    def test_backoff_capped(self):
        self.assertEqual(backoff_seconds(1, 100), 100)
        self.assertEqual(backoff_seconds(2, 100), 200)
        self.assertEqual(backoff_seconds(3, 100), 400)
        self.assertEqual(backoff_seconds(100, 100), 6 * 3600)


if __name__ == "__main__":
    unittest.main()
