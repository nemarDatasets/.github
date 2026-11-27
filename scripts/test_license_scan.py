#!/usr/bin/env python3
"""
Runner proof for the license matcher steps of .github/workflows/run-publication-review.yml
(nemar-cli ADR 0142).

No mocks: the real shell text of the "Install the license matcher", "Prepare the license scan"
and "Scan LICENSE in a sandbox" steps is read from the workflow by name and run with bash on a
GitHub-hosted runner, with the runner's own sudo, unshare and setpriv.

1. Install: the pinned CPython, ScanCode from nemar-cli's hashed requirements file, the version
   checks and the index warm-up. Its wall time is recorded.
2. Each scripts/fixtures/license-scan/<case>/ holds a public license text (LICENSE) and case.json
   ({"field": <License field>, "expected": <license_file fact>}). The case's LICENSE is committed
   to a real git repository at refs/remotes/origin/main, where the fetch step leaves the dataset;
   the prepare and scan steps run over it, and nemar-cli's scripts/ci/license-file-fact.ts (at the
   nemar-cli ref checked out into ./tool) turns the real result into the fact, which must equal
   the expected one. Each result.json is copied to the results directory for upload: these are
   the fixtures nemar-cli's unit tests read.
3. A symlinked LICENSE and a missing one are never extracted or scanned.
4. The sandbox: the scan step's own command, with ScanCode swapped for a Python probe, must run as
   uid 65534 as PID 1 of its namespace, with no ACTIONS_ID_TOKEN_REQUEST_* variable (they are in
   this job's environment), no socket to the network (this process connects, as a control),
   no write to _temp, _actions, the workspace or /opt/license-scan (and a write to its own out/
   directory, as a control); the license index is not rebuilt by any scan.
5. A tampered hash in the requirements file makes the install report `failed`, and the step
   still exits 0.

Run on a runner: uv run --with pyyaml python scripts/test_license_scan.py <results-dir>
(needs ./tool to be a nemar-cli checkout, bun on PATH, passwordless sudo and the OIDC request
variables, as in test-license-scan.yml)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/run-publication-review.yml"
FIXTURES = ROOT / "scripts/fixtures/license-scan"
TOOL = ROOT / "tool"
SCAN = Path("/tmp/license-scan")
MATCHER_ROOT = Path("/opt/license-scan")
MATCHER_VERSION = "scancode-32.5.0+spdx-3.27"
SCANCODE_CALL = '"$venv/bin/scancode"'
REQUIREMENTS = "-r scripts/ci/scancode-requirements.txt"
RUNNER_TEMP = Path(os.environ["RUNNER_TEMP"])
REPO = RUNNER_TEMP / "repo"

STEPS = {}
for job in yaml.safe_load(WORKFLOW.read_text())["jobs"].values():
    for step in job["steps"]:
        if "name" in step:
            STEPS[step["name"]] = step

failed = 0


def report(ok: bool, label: str, detail: str = "") -> None:
    global failed
    failed += not ok
    print(("ok   " if ok else "FAIL ") + label + ("  " + detail if detail else ""), flush=True)


def run_step(name: str, extra_env: dict, script: str | None = None, timeout: int = 300):
    """Run one workflow step's shell as Actions does (bash -e, pipefail), with its static env."""
    step = STEPS[name]
    static = {k: str(v) for k, v in (step.get("env") or {}).items() if "${{" not in str(v)}
    with tempfile.NamedTemporaryFile("r", delete=False) as out:
        out_path = out.name
    env = {**os.environ, **static, **extra_env, "GITHUB_OUTPUT": out_path}
    cwd = ROOT / step["working-directory"] if "working-directory" in step else ROOT
    started = time.monotonic()
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script or step["run"]],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False,
    )
    elapsed = time.monotonic() - started
    outputs = dict(
        line.split("=", 1) for line in Path(out_path).read_text().splitlines() if "=" in line
    )
    os.unlink(out_path)
    return proc, outputs, elapsed


def git(*args: str, cwd: Path = REPO) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_repo(license_bytes: bytes | None, symlink: bool = False) -> None:
    """A repository whose refs/remotes/origin/main holds LICENSE, as the fetch step leaves it."""
    shutil.rmtree(REPO, ignore_errors=True)
    REPO.mkdir(parents=True)
    git("init", "-q", ".")
    (REPO / "dataset_description.json").write_text('{"Name": "fixture"}\n')
    if symlink:
        os.symlink(".git/annex/objects/xx/yy/SHA256E-s1--abc/SHA256E-s1--abc", REPO / "LICENSE")
    elif license_bytes is not None:
        (REPO / "LICENSE").write_bytes(license_bytes)
    git("add", "-A")
    git("commit", "-q", "-m", "fixture")
    git("update-ref", "refs/remotes/origin/main", "HEAD")


def fact_of(field: str) -> dict:
    proc = subprocess.run(
        [
            "bun", "run", str(TOOL / "scripts/ci/license-file-fact.ts"),
            "--scan-dir", str(SCAN), "--license-field", field,
            "--license-file", str(SCAN / "in/LICENSE"),
            "--license-oid", (SCAN / "oid").read_text().strip(),
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def index_snapshot() -> list:
    found = sorted(MATCHER_ROOT.glob("venv/lib/python*/site-packages/licensedcode/data/cache"))
    if not found:
        return []
    return sorted(
        (str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in found[0].rglob("*") if p.is_file()
    )


PROBE = r'''
import json, os, socket, sys
out = sys.argv[sys.argv.index("--json") + 1]
r = {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(), "pid": os.getpid()}
r["env"] = sorted(os.environ)
def connect(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
        return "connected"
    except OSError as e:
        return type(e).__name__
r["connect_ip"] = connect("140.82.112.3", 443)
r["connect_name"] = connect("github.com", 443)
writes = {}
for d in json.loads(TARGETS):
    try:
        with open(os.path.join(d, "probe-%d" % os.getpid()), "x") as f:
            f.write("x")
        writes[d] = "written"
    except OSError as e:
        writes[d] = type(e).__name__
r["writes"] = writes
with open(out, "w") as f:
    json.dump(r, f)
'''


def main() -> int:
    results = Path(sys.argv[1]).resolve()
    results.mkdir(parents=True, exist_ok=True)
    timings = {}

    # 1. Install.
    proc, out, elapsed = run_step("Install the license matcher", {}, timeout=400)
    timings["install_seconds"] = round(elapsed, 1)
    report(proc.returncode == 0 and out.get("status") == "ok",
           "install: the pinned matcher installs and passes its checks",
           f"status={out.get('status')} {elapsed:.1f}s")
    if out.get("status") != "ok":
        print(proc.stdout[-4000:], proc.stderr[-4000:])
        return 1
    st = MATCHER_ROOT.stat()
    report(st.st_uid == 0 and not (st.st_mode & 0o022), "install: /opt/license-scan is root's, read-only")
    before = index_snapshot()
    report(len(before) > 0, "install: the license index was built at warm-up", f"{len(before)} files")

    # 2. Every fixture, scanned in the sandbox and identified by nemar-cli's module.
    scan_times = {}
    for case in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        cfg = json.loads((case / "case.json").read_text())
        make_repo((case / "LICENSE").read_bytes())
        proc, out, _ = run_step("Prepare the license scan", {"MATCHER_STATUS": "ok"})
        if proc.returncode != 0 or out.get("scan") != "true":
            report(False, f"{case.name}: prepare", proc.stdout + proc.stderr)
            continue
        proc, _, elapsed = run_step("Scan LICENSE in a sandbox", {}, timeout=200)
        scan_times[case.name] = round(elapsed, 1)
        status = (SCAN / "status").read_text().strip() if (SCAN / "status").exists() else None
        if status != "ok":
            report(False, f"{case.name}: scan", f"status={status} {proc.stdout}")
            continue
        dest = results / case.name
        dest.mkdir(exist_ok=True)
        shutil.copyfile(SCAN / "out/result.json", dest / "result.json")
        got = fact_of(cfg["field"])
        ok = got == {"license_file": cfg["expected"], "matcher": MATCHER_VERSION}
        report(ok, case.name, json.dumps(got) + f" {elapsed:.1f}s")
    timings["scan_seconds"] = scan_times

    # 3. A symlink (an annexed link) and a missing LICENSE are never extracted.
    for label, kwargs in (("symlinked LICENSE", {"license_bytes": None, "symlink": True}),
                          ("no LICENSE", {"license_bytes": None})):
        make_repo(**kwargs)
        proc, out, _ = run_step("Prepare the license scan", {"MATCHER_STATUS": "ok"})
        report(proc.returncode == 0 and out.get("scan") is None
               and not (SCAN / "in/LICENSE").exists() and not (SCAN / "oid").exists(),
               f"prepare: a {label} is not extracted")

    # 4. The sandbox, with the scan step's own command.
    oidc = [k for k in os.environ if k.startswith("ACTIONS_ID_TOKEN_REQUEST_")]
    report(len(oidc) == 2, "control: this job holds the OIDC request variables", ",".join(sorted(oidc)))
    try:
        socket.create_connection(("github.com", 443), timeout=10).close()
        outside = "connected"
    except OSError as e:
        outside = type(e).__name__
    report(outside == "connected", "control: this process can open a socket")
    index_dir = sorted(MATCHER_ROOT.glob("venv/lib/python*/site-packages/licensedcode/data/cache"))
    targets = [
        str(RUNNER_TEMP),
        str(RUNNER_TEMP.parent / "_actions"),
        os.environ.get("GITHUB_WORKSPACE", str(ROOT)),
        str(Path.home()),
        str(MATCHER_ROOT),
        str(MATCHER_ROOT / "venv"),
        *(str(p) for p in index_dir),
        str(SCAN),
        str(SCAN / "in"),
        str(SCAN / "out"),
    ]
    report(Path(targets[1]).is_dir(), "control: the _actions directory exists", targets[1])
    make_repo((FIXTURES / "cc-by-4.0/LICENSE").read_bytes())
    run_step("Prepare the license scan", {"MATCHER_STATUS": "ok"})
    script = STEPS["Scan LICENSE in a sandbox"]["run"]
    assert SCANCODE_CALL in script, "the scan command moved; update the probe"
    probe = PROBE.replace("TARGETS", repr(json.dumps(targets)))
    proc, _, _ = run_step(
        "Scan LICENSE in a sandbox", {"PROBE": probe},
        script=script.replace(SCANCODE_CALL, '"$venv/bin/python" -I -c "$PROBE"'),
    )
    r = json.loads((SCAN / "out/result.json").read_text())
    print("probe:", json.dumps(r), flush=True)
    report(r["uid"] == 65534 and r["gid"] == 65534 and r["groups"] in ([], [65534]),
           "sandbox: runs as uid 65534 with no supplementary group")
    report(r["pid"] == 1, "sandbox: PID 1 of its own PID namespace")
    report(not any(k.startswith("ACTIONS_") for k in r["env"])
           and set(r["env"]) <= {"PATH", "HOME", "TMPDIR", "SCANCODE_CACHE", "SCANCODE_TEMP", "LC_CTYPE"},
           "sandbox: no ACTIONS_ID_TOKEN_REQUEST_* and nothing beyond its paths", ",".join(r["env"]))
    report(r["connect_ip"] != "connected" and r["connect_name"] != "connected",
           "sandbox: no socket to the network", f"{r['connect_ip']} {r['connect_name']}")
    denied = {d: w for d, w in r["writes"].items() if d != str(SCAN / "out")}
    report(all(w != "written" for w in denied.values()),
           "sandbox: no write to _temp, _actions, the workspace, home, /opt/license-scan or the index",
           json.dumps(denied))
    report(r["writes"].get(str(SCAN / "out")) == "written", "control: the sandbox writes its own out/")
    report(index_snapshot() == before, "sandbox: no scan rebuilt the license index")

    # 5. A tampered hash: the install reports failed and the step exits 0.
    req = TOOL / "scripts/ci/scancode-requirements.txt"
    # Every hash of scancode-toolkit's own entry, so no distribution of it can still match.
    lines, inside = [], False
    for line in req.read_text().splitlines(keepends=True):
        if line and not line[0].isspace():
            inside = line.startswith("scancode-toolkit==")
        if inside and "--hash=sha256:" in line:
            i = line.index("--hash=sha256:") + len("--hash=sha256:")
            line = line[:i] + ("0" if line[i] != "0" else "1") + line[i + 1:]
        lines.append(line)
    tampered = RUNNER_TEMP / "tampered-requirements.txt"
    tampered.write_text("".join(lines))
    report(tampered.read_text() != req.read_text(), "control: the tampered file differs")
    script = STEPS["Install the license matcher"]["run"]
    assert REQUIREMENTS in script, "the requirements path moved; update the tamper check"
    proc, out, _ = run_step("Install the license matcher", {},
                            script=script.replace(REQUIREMENTS, f"-r {tampered}"), timeout=400)
    report(proc.returncode == 0 and out.get("status") == "failed",
           "install: a tampered hash is `failed`, and the step exits 0", f"status={out.get('status')}")

    (results / "timings.json").write_text(json.dumps(timings, indent=2) + "\n")
    print(json.dumps(timings), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
