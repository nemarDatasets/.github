#!/usr/bin/env python3
"""
Real-process integration test for scripts/archive-size-check.sh (#1514).

No mocks. Every test invokes the real script as a subprocess with a real
environment. The two "callback failure" tests use a real HTTP server
(stdlib http.server, on 127.0.0.1) standing in for the Worker's
/webhooks/archive-status endpoint -- it receives and can be asserted on the
actual request the script sends, and it can be told to answer with a real
5xx to exercise the failure path. That is the same "HTTP response fixture"
pattern already used across this repo's test scripts; it is a real socket
and a real process boundary, not a stand-in for the script's own logic.

Run with either:
    python3 scripts/test_archive_size_check.py
    uv run python scripts/test_archive_size_check.py
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "archive-size-check.sh"

# The real nm000284 incident shape (ADR 0012 amendment, #1514): a declared
# size that exceeds the 100 GiB archive limit. 550239019072 / 1073741824 =
# 512.450...; both bash's awk and JS's toFixed(1)/toLocaleString() round
# this to "512.5", confirmed against the corrected production row.
NM000284_BYTES = 550239019072
EXPECTED_BYTES_REASON = "dataset 512.5 GB exceeds 100.0 GB archive limit; use direct download"

FILES_OVER = 214000
EXPECTED_FILES_REASON = "dataset 214,000 files exceeds 200,000 archive limit; use direct download"

# Keys the script reads; stripped from the inherited environment before each
# run so a variable leaking in from the calling shell can't change the
# outcome, then repopulated per test from an explicit dict.
SCRIPT_ENV_KEYS = (
    "DATASET_ID",
    "VERSION",
    "MANIFEST_BYTES",
    "MANIFEST_FILES",
    "PAYLOAD_TOTAL_BYTES",
    "PAYLOAD_TOTAL_FILES",
    "DERIVED_BYTES",
    "DERIVED_FILES",
    "CALLBACK_TOKEN",
    "CALLBACK_URL",
    "GITHUB_OUTPUT",
)

DEFAULT_IDS = {"DATASET_ID": "nm000284", "VERSION": "1.0.0"}


def run_script(overrides: dict[str, str], *, github_output: Path | None = None):
    """Invoke the real script with a controlled environment.

    `overrides` is applied on top of DEFAULT_IDS; pass an explicit empty
    string or omit a key to leave it unset. Returns the CompletedProcess.
    """
    env = os.environ.copy()
    for key in SCRIPT_ENV_KEYS:
        env.pop(key, None)
    env.update(DEFAULT_IDS)
    env.update(overrides)
    if github_output is not None:
        env["GITHUB_OUTPUT"] = str(github_output)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # exit codes (0 and 1) are asserted on by the caller
    )


def read_output(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


class CallbackServer:
    """A real HTTP server standing in for the /webhooks/archive-status
    callback, so the callback-failure and callback-success paths can be
    exercised against an actual socket rather than asserted about."""

    def __init__(self, status_code: int = 200, body: bytes = b'{"ok":true}'):
        self.status_code = status_code
        self.body = body
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # stdlib method name, not ours to rename
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                outer.requests.append(
                    {
                        "path": self.path,
                        "token": self.headers.get("X-Webhook-Token"),
                        "body": json.loads(raw) if raw else None,
                    }
                )
                self.send_response(outer.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, format: str, *args: object) -> None:
                # Silence stdlib access logging; signature must match the
                # base class (format: str, *args: object) -> None.
                return

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}/webhooks/archive-status"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class RequiredFieldsTests(unittest.TestCase):
    """DATASET_ID and VERSION are required; either missing must abort
    before any tier logic runs, exit 1, and never attempt a callback."""

    def test_missing_dataset_id_exits_1(self):
        proc = run_script({"DATASET_ID": ""})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Missing dataset_id/version", proc.stdout + proc.stderr)

    def test_missing_version_exits_1(self):
        proc = run_script({"VERSION": ""})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Missing dataset_id/version", proc.stdout + proc.stderr)


class NM000284ShapeAllTiersTests(unittest.TestCase):
    """The real incident shape (550,239,019,072 bytes, over the 100 GiB
    limit) must produce the identical decision -- skip=true and the exact
    backend-matching reason string -- from whichever tier resolves it."""

    def _assert_over_limit(self, proc: subprocess.CompletedProcess, source: str, out: Path):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"source: {source}", proc.stdout)
        self.assertIn(EXPECTED_BYTES_REASON, proc.stdout)
        self.assertEqual(read_output(out).get("skip"), "true")

    def test_manifest_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script(
                {"MANIFEST_BYTES": str(NM000284_BYTES)},
                github_output=out,
            )
            self._assert_over_limit(proc, "manifest", out)

    def test_dispatch_payload_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script(
                {"PAYLOAD_TOTAL_BYTES": str(NM000284_BYTES)},
                github_output=out,
            )
            self._assert_over_limit(proc, "dispatch payload", out)

    def test_annex_derivation_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script(
                {"DERIVED_BYTES": str(NM000284_BYTES)},
                github_output=out,
            )
            self._assert_over_limit(proc, "annex-key derivation", out)

    def test_manifest_tier_wins_over_lower_tiers_when_present(self):
        # Tier 1 short-circuits: a manifest value under the limit must be
        # used even though a higher tier is (deliberately) also over.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script(
                {
                    "MANIFEST_BYTES": "1000",
                    "PAYLOAD_TOTAL_BYTES": str(NM000284_BYTES),
                    "DERIVED_BYTES": str(NM000284_BYTES),
                },
                github_output=out,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("source: manifest", proc.stdout)
            self.assertEqual(read_output(out).get("skip"), "false")


class FileCountReasonTests(unittest.TestCase):
    """The file-count reason must match archive-policy.ts's
    toLocaleString() formatting exactly, including the thousands
    separator, even though bash has no locale-independent equivalent."""

    def test_files_over_limit_reason_matches_backend_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script(
                {"MANIFEST_BYTES": "1000", "MANIFEST_FILES": str(FILES_OVER)},
                github_output=out,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(EXPECTED_FILES_REASON, proc.stdout)
            self.assertEqual(read_output(out).get("skip"), "true")

    def test_bytes_reason_takes_priority_over_files_reason(self):
        # Both over limit at once: the script's bytes branch is checked
        # first (elif on files), so only the bytes reason should surface.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script(
                {
                    "MANIFEST_BYTES": str(NM000284_BYTES),
                    "MANIFEST_FILES": str(FILES_OVER),
                },
                github_output=out,
            )
            self.assertIn(EXPECTED_BYTES_REASON, proc.stdout)
            self.assertNotIn(EXPECTED_FILES_REASON, proc.stdout)


class UnderLimitTests(unittest.TestCase):
    def test_under_both_limits_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script(
                {"MANIFEST_BYTES": "1000", "MANIFEST_FILES": "5"},
                github_output=out,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Under archive limits", proc.stdout)
            self.assertEqual(read_output(out).get("skip"), "false")


class LiteralZeroTests(unittest.TestCase):
    """`is_uint` and the tier `[ -z "$BYTES" ]` "already resolved" checks
    test for an EMPTY string, not a falsy/zero one -- a genuinely empty
    dataset (0 bytes, 0 files) must be picked up as a real, present value
    from whichever tier reports it, not treated the same as "absent" and
    passed through to the next tier (NIT, #1514 review)."""

    def test_manifest_zero_bytes_and_files_reports_source_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script(
                {"MANIFEST_BYTES": "0", "MANIFEST_FILES": "0"},
                github_output=out,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("source: manifest", proc.stdout)
            self.assertIn("0 bytes, 0 files", proc.stdout)
            self.assertIn("Under archive limits", proc.stdout)
            self.assertEqual(read_output(out).get("skip"), "false")

    def test_payload_zero_bytes_is_used_not_skipped_to_derivation(self):
        # No MANIFEST_BYTES, so tier 1 is empty; PAYLOAD_TOTAL_BYTES=0 must
        # be treated as a resolved (zero) value from tier 2, not as absent
        # -- which would otherwise fall through to tier 3's derivation.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script(
                {"PAYLOAD_TOTAL_BYTES": "0", "DERIVED_BYTES": "999999999999"},
                github_output=out,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("source: dispatch payload", proc.stdout)
            self.assertNotIn("source: annex-key derivation", proc.stdout)
            self.assertEqual(read_output(out).get("skip"), "false")


class MalformedPayloadFallthroughTests(unittest.TestCase):
    """A malformed (non-digit) payload value must warn and fall through to
    the next tier rather than being trusted or aborting the script."""

    def test_non_digit_payload_falls_through_to_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script(
                {"PAYLOAD_TOTAL_BYTES": "not-a-number", "DERIVED_BYTES": "1000"},
                github_output=out,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("is not a plain integer", proc.stdout + proc.stderr)
            self.assertIn("source: annex-key derivation", proc.stdout)
            self.assertEqual(read_output(out).get("skip"), "false")

    def test_oversized_digit_string_is_invalid_not_trusted(self):
        # 19 digits: all-digit (would pass a naive regex) but past the
        # 18-digit bound is_uint enforces (#1514 item 8). Must be treated
        # as invalid -- fall through, never silently compared as in-policy.
        huge = "9" * 19
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script({"PAYLOAD_TOTAL_BYTES": huge}, github_output=out)
            # No lower tier resolves it either -> fail-open (unknown means
            # build), never a false "under limit" from the oversized value.
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("no size source available", proc.stdout + proc.stderr)
            self.assertEqual(read_output(out).get("skip"), "false")

    def test_18_digit_boundary_is_still_valid(self):
        # Exactly at the bound: must NOT be rejected (off-by-one guard).
        eighteen_nines = "9" * 18  # under MAX_BYTES is false, but must PARSE
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script({"MANIFEST_BYTES": eighteen_nines}, github_output=out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("source: manifest", proc.stdout)
            self.assertEqual(read_output(out).get("skip"), "true")


class NoSizeSourceFailOpenTests(unittest.TestCase):
    def test_all_tiers_absent_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            proc = run_script({}, github_output=out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("no size source available", proc.stdout + proc.stderr)
            self.assertEqual(read_output(out).get("skip"), "false")


class CallbackTests(unittest.TestCase):
    """The skip callback must succeed silently on 2xx, and -- because a
    dropped archive_skip_reason leaves the dataset a silent dead-end -- the
    job must FAIL (exit 1) when the callback does not come back 2xx."""

    def test_callback_success_posts_expected_body_and_exits_0(self):
        server = CallbackServer(status_code=200)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "gh_output"
                proc = run_script(
                    {
                        "MANIFEST_BYTES": str(NM000284_BYTES),
                        "CALLBACK_TOKEN": "test-token-abc",
                        "CALLBACK_URL": server.url,
                    },
                    github_output=out,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(read_output(out).get("skip"), "true")
                self.assertEqual(len(server.requests), 1)
                req = server.requests[0]
                self.assertEqual(req["token"], "test-token-abc")
                self.assertEqual(req["body"]["dataset_id"], "nm000284")
                self.assertEqual(req["body"]["version"], "1.0.0")
                self.assertEqual(req["body"]["status"], "skipped")
                self.assertEqual(req["body"]["reason"], EXPECTED_BYTES_REASON)
        finally:
            server.close()

    def test_callback_failure_exits_1(self):
        server = CallbackServer(status_code=500, body=b'{"error":"boom"}')
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "gh_output"
                proc = run_script(
                    {
                        "MANIFEST_BYTES": str(NM000284_BYTES),
                        "CALLBACK_TOKEN": "test-token-abc",
                        "CALLBACK_URL": server.url,
                    },
                    github_output=out,
                )
                self.assertEqual(proc.returncode, 1)
                self.assertIn("skip callback failed", proc.stdout + proc.stderr)
                # skip=true was already written before the callback ran.
                self.assertEqual(read_output(out).get("skip"), "true")
        finally:
            server.close()

    def test_missing_token_warns_and_exits_0_without_posting(self):
        server = CallbackServer(status_code=200)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "gh_output"
                proc = run_script(
                    {
                        "MANIFEST_BYTES": str(NM000284_BYTES),
                        "CALLBACK_URL": server.url,
                    },
                    github_output=out,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("NEMAR_WEBHOOK_TOKEN unset", proc.stdout + proc.stderr)
                self.assertEqual(server.requests, [])
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
