#!/usr/bin/env python3
"""
Tiny helper for .github/workflows/generate-manifest.yml.

Reads the totals.json that emit_manifest.py wrote and produces the JSON
body that the workflow POSTs to /webhooks/manifest-ready. Pulled out of
the workflow's run-script to avoid heredoc indentation gotchas and to
keep the YAML readable.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset-id", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--manifest-url", required=True)
    p.add_argument("--summary-url", required=True)
    p.add_argument("--totals-path", required=True)
    p.add_argument("--workflow-run-id", required=True)
    # canary_skipped lets the Worker disambiguate "canary verified the
    # raw.githubusercontent.com path" from "canary was skipped because the
    # caller declared the repo private". Without this the Worker would
    # silently treat both as equivalent.
    p.add_argument(
        "--canary-skipped",
        default="false",
        choices=("true", "false"),
        help="Whether the canary HEAD checks were skipped (true|false)",
    )
    p.add_argument("--out", required=True)
    args = p.parse_args()

    totals = json.loads(Path(args.totals_path).read_text())
    body = {
        "dataset_id": args.dataset_id,
        "version": args.version,
        "manifest_url": args.manifest_url,
        "summary_url": args.summary_url,
        "totals": totals,
        "workflow_run_id": args.workflow_run_id,
        "canary_skipped": args.canary_skipped == "true",
    }
    Path(args.out).write_text(json.dumps(body))
    print(f"[build_callback_body] wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
