#!/usr/bin/env python3
"""Persistent SQLite conversion queue for the NEMAR Hallu zarr cron (epic #684).

The Hallu cron drives Zarr conversion off this queue instead of re-deriving work
each run, so a long backfill (hundreds of datasets) drains gracefully, failures
retry with backoff, and the whole thing is **crash-safe**: the queue lives on
disk (WAL SQLite), and `reconcile` resets any `inprogress` job left behind by a
killed/rebooted run back to `pending`. As long as the machine is on, the cron
fires and the queue picks up exactly where it left off.

One row per dataset:
    jobs(dataset_id PK, latest_version, converted_version,
         status,            -- pending | inprogress | done | failed | data_failed
         attempts, last_error, next_retry_at (epoch),
         enqueued_at (ISO), updated_at (epoch))

`failed` is an INFRA failure that exhausted its bounded retries (transient: a
crashed worker / S3 blip — could be re-tried manually). `data_failed` is a
deterministic DATA failure (a recording biosigIO can't read, e.g. a MaxShield
MEG `.fif`); it fails identically every run, so it is terminal immediately and
is NOT re-queued by reconcile until a genuinely new dataset version appears
(nemarOrg/nemar-cli#774 — previously every reconcile re-queued failed rows from
scratch, which wedged the queue on a single unconvertible dataset).

Subcommands (all take --db):
    reconcile --api-base URL [--stale-seconds N]
        Pull GET /datasets, enqueue (status=pending) every public nm/on dataset
        whose latest_version != converted_version, and reset stale `inprogress`
        rows to `pending`. Idempotent.
    next [--max-attempts N]
        Atomically claim the oldest eligible job -> `inprogress`; print
        "<dataset_id>\t<latest_version>" (nothing if the queue is drained).
    done DATASET VERSION         mark converted at VERSION.
    fail DATASET "ERROR" [--max-attempts N] [--backoff-base S]
        attempts++; reschedule (pending + next_retry_at) until max-attempts, then
        terminal `failed`.
    stats                        counts by status (+ a few recent failures).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  dataset_id        TEXT PRIMARY KEY,
  latest_version    TEXT,
  converted_version TEXT,
  status            TEXT NOT NULL DEFAULT 'pending',
  attempts          INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  next_retry_at     INTEGER NOT NULL DEFAULT 0,
  enqueued_at       TEXT,
  updated_at        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

DATASET_ID_RE = re.compile(r"^(nm|on)[0-9]{6}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> int:
    return int(time.time())


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(SCHEMA)
    return conn


def backoff_seconds(attempts: int, base: int, cap: int = 6 * 3600) -> int:
    """Exponential backoff, capped: base, 2*base, 4*base, ... <= cap."""
    return min(base * (2 ** max(0, attempts - 1)), cap)


# --- transition helpers (exercised by the unit tests) -------------------------


def reconcile(conn: sqlite3.Connection, datasets: list[tuple[str, str]], stale_seconds: int) -> dict:
    """Enqueue datasets needing (re)conversion + recover stale inprogress rows.

    `datasets` is a list of (dataset_id, latest_version). A dataset is enqueued
    `pending` when it is new or its latest_version differs from the version we
    last converted. A row already `done`/`failed` at the same latest_version is
    left alone (a new version flips it back to pending). `inprogress` rows whose
    `updated_at` is older than `stale_seconds` are assumed crashed and reset to
    `pending` so they run again.
    """
    now_iso, now = _now_iso(), _now()
    enq = 0
    for dataset_id, latest in datasets:
        if not DATASET_ID_RE.match(dataset_id) or dataset_id == "nm099999":
            continue
        row = conn.execute(
            "SELECT status, converted_version, latest_version FROM jobs WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO jobs(dataset_id, latest_version, status, enqueued_at, updated_at)"
                " VALUES(?,?, 'pending', ?, ?)",
                (dataset_id, latest, now_iso, now),
            )
            enq += 1
            continue
        status = row["status"]
        if status == "done":
            # Re-convert only when a version newer than the one we converted
            # appears.
            if latest and latest != (row["converted_version"] or ""):
                conn.execute(
                    "UPDATE jobs SET latest_version=?, status='pending', attempts=0,"
                    " next_retry_at=0, updated_at=? WHERE dataset_id=?",
                    (latest, now, dataset_id),
                )
                enq += 1
        elif status in ("failed", "data_failed"):
            # Terminal for THIS version (#774). Only a genuinely NEW snapshot
            # (latest != the version we already gave up on) retries -- compare
            # against latest_version, NOT converted_version (which is NULL on a
            # failure, so the old check re-queued every reconcile and wedged the
            # queue on one unconvertible dataset).
            if latest and latest != (row["latest_version"] or ""):
                conn.execute(
                    "UPDATE jobs SET latest_version=?, status='pending', attempts=0,"
                    " next_retry_at=0, last_error=NULL, updated_at=? WHERE dataset_id=?",
                    (latest, now, dataset_id),
                )
                enq += 1
        else:
            # pending / inprogress -- refresh the target version only. Do NOT
            # touch updated_at: it is the inprogress heartbeat the stale-recovery
            # sweep below relies on.
            if latest and latest != (row["latest_version"] or ""):
                conn.execute(
                    "UPDATE jobs SET latest_version=? WHERE dataset_id=?",
                    (latest, dataset_id),
                )

    recovered = conn.execute(
        "UPDATE jobs SET status='pending', next_retry_at=0, updated_at=?"
        " WHERE status='inprogress' AND updated_at < ?",
        (now, now - stale_seconds),
    ).rowcount
    conn.commit()
    return {"enqueued": enq, "recovered_stale": recovered}


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Claim the oldest eligible pending job (respecting next_retry_at) and mark
    it inprogress. Atomic under the connection's write lock."""
    row = conn.execute(
        "SELECT dataset_id, latest_version FROM jobs"
        " WHERE status='pending' AND next_retry_at <= ?"
        " ORDER BY enqueued_at ASC LIMIT 1",
        (_now(),),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE jobs SET status='inprogress', updated_at=? WHERE dataset_id=?",
        (_now(), row["dataset_id"]),
    )
    conn.commit()
    return row


def mark_done(conn: sqlite3.Connection, dataset_id: str, version: str) -> None:
    conn.execute(
        "UPDATE jobs SET status='done', converted_version=?, last_error=NULL,"
        " next_retry_at=0, updated_at=? WHERE dataset_id=?",
        (version, _now(), dataset_id),
    )
    conn.commit()


def mark_fail(
    conn: sqlite3.Connection,
    dataset_id: str,
    error: str,
    max_attempts: int,
    backoff_base: int,
    deterministic: bool = False,
) -> str:
    """Record a failure and return the resulting status.

    `deterministic=True` (a typed DATA failure — biosigIO can't read the
    recording) is terminal **immediately** as `data_failed`: it would fail
    identically on every retry, so retrying only wedges the queue (#774). An
    infra failure reschedules (pending + backoff) until max_attempts, then
    terminal `failed`.
    """
    row = conn.execute("SELECT attempts FROM jobs WHERE dataset_id=?", (dataset_id,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    if deterministic:
        status, next_retry = "data_failed", 0
    elif attempts >= max_attempts:
        status, next_retry = "failed", 0
    else:
        status, next_retry = "pending", _now() + backoff_seconds(attempts, backoff_base)
    conn.execute(
        "UPDATE jobs SET status=?, attempts=?, last_error=?, next_retry_at=?, updated_at=?"
        " WHERE dataset_id=?",
        (status, attempts, error[:1000], next_retry, _now(), dataset_id),
    )
    conn.commit()
    return status


# --- I/O: fetch the dataset list ----------------------------------------------


def fetch_public_datasets(api_base: str) -> list[tuple[str, str]]:
    """Every public dataset as (dataset_id, latest_version).

    Paginates: `GET /datasets` caps a page at 200 regardless of `limit`, so we
    walk `offset` until `total_count`. A non-default User-Agent is required --
    api.nemar.org sits behind Cloudflare, which 403s the default Python-urllib UA
    as a bot.
    """
    base = api_base.rstrip("/")
    out: list[tuple[str, str]] = []
    offset, page = 0, 200
    while True:
        url = f"{base}/datasets?limit={page}&offset={offset}"
        req = urllib.request.Request(url, headers={"User-Agent": "nemar-zarr-cron/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - trusted NEMAR API
            payload = json.loads(resp.read().decode("utf-8"))
        rows = payload.get("datasets", []) or []
        for d in rows:
            if d.get("visibility") != "public":
                continue
            out.append((str(d.get("dataset_id", "")), str(d.get("latest_version") or "")))
        offset += len(rows)
        if not rows or offset >= int(payload.get("total_count", 0) or 0):
            break
    return out


# --- CLI ----------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="NEMAR zarr conversion queue (SQLite)")
    ap.add_argument("--db", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("reconcile")
    p.add_argument("--api-base", default="https://api.nemar.org")
    p.add_argument("--stale-seconds", type=int, default=6 * 3600)

    sub.add_parser("next")

    p = sub.add_parser("done")
    p.add_argument("dataset")
    p.add_argument("version")

    p = sub.add_parser("fail")
    p.add_argument("dataset")
    p.add_argument("error")
    p.add_argument("--max-attempts", type=int, default=5)
    p.add_argument("--backoff-base", type=int, default=1800)
    p.add_argument(
        "--deterministic",
        action="store_true",
        help="typed DATA failure: terminal `data_failed` now, no retry/requeue (#774)",
    )

    sub.add_parser("stats")

    args = ap.parse_args()
    conn = connect(args.db)

    if args.cmd == "reconcile":
        datasets = fetch_public_datasets(args.api_base)
        res = reconcile(conn, datasets, args.stale_seconds)
        print(
            f"reconcile: seen={len(datasets)} enqueued={res['enqueued']} "
            f"recovered_stale={res['recovered_stale']}"
        )
        return 0

    if args.cmd == "next":
        row = claim_next(conn)
        if row is not None:
            print(f"{row['dataset_id']}\t{row['latest_version']}")
        return 0

    if args.cmd == "done":
        mark_done(conn, args.dataset, args.version)
        return 0

    if args.cmd == "fail":
        status = mark_fail(
            conn,
            args.dataset,
            args.error,
            args.max_attempts,
            args.backoff_base,
            deterministic=args.deterministic,
        )
        print(f"{args.dataset} -> {status}")
        return 0

    if args.cmd == "stats":
        rows = conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        print("status: " + (", ".join(f"{r['status']}={r['n']}" for r in rows) or "(empty)"))
        fails = conn.execute(
            "SELECT dataset_id, status, attempts, last_error FROM jobs"
            " WHERE status IN ('failed', 'data_failed')"
            " ORDER BY updated_at DESC LIMIT 5"
        ).fetchall()
        for r in fails:
            print(
                f"  {r['status']} {r['dataset_id']} attempts={r['attempts']}:"
                f" {(r['last_error'] or '')[:120]}"
            )
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
