#!/usr/bin/env python3
"""
Tiny helper for the failure-callback step of generate-manifest.yml.

Writes a best-effort failure-notification body to a file. Pulled out of
the workflow's run-script to avoid heredoc indentation issues inside the
YAML block scalar.

The endpoint (/webhooks/manifest-failed) lands with Stream B; a 404 is
tolerated in the interim, so this body shape is intentionally minimal.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset-id", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--workflow-run-id", required=True)
    p.add_argument("--workflow-run-url", required=True)
    p.add_argument("--failed-step", default="unknown")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    # The /webhooks/manifest-failed handler reads body.error_message and
    # writes it to manifest_jobs.error_message. We pack the failed step
    # name into this field so the DB row records which workflow step
    # died, not the unhelpful sentinel "unknown error".
    body = {
        "dataset_id": args.dataset_id,
        "version": args.version,
        "workflow_run_id": args.workflow_run_id,
        "workflow_run_url": args.workflow_run_url,
        "error_message": f"failed at step: {args.failed_step}",
    }
    Path(args.out).write_text(json.dumps(body))
    print(f"[build_failure_body] wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
