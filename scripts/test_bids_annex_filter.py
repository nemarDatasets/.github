#!/usr/bin/env python3
"""
Fixture tests for the annex filtering in .github/workflows/run-bids-validation.yml.

No mocks: the real shell text of the "Collect git-annex pointer files" and
"Check validation result" steps is extracted from the workflow and run with
bash, git and jq.

1. scripts/fixtures/bids-annex/<case>/ holds validator output and the annex
   lists the check step reads:
     validation.json        error entries (bids-validator 3.0.2 --json)
     annex-files.txt        annex pointers (trimmed for the real-dataset cases)
     annex-unresolved.txt   pointers whose content the validator could not read
     expected.env           real_errors / annex_errors / annex_located_errors
   The on* cases are real runs (on008446, on006673, on002799, on007463,
   on004551); the synthetic-* cases pin one rule each.
2. The collect step runs in a real temporary git repository with symlinks, an
   annex pointer file and a git-annex branch, and must list as unresolved
   exactly the pointer files and the symlinks whose key has no .log.rmet.
3. Mutations: weakening the path-only guard, the unreadable gate or the rmet
   gate must make a test fail.

Run: uv run --with pyyaml python scripts/test_bids_annex_filter.py
(needs bash, git, jq; the collect step needs GNU find, as on ubuntu runners)
Regenerate expected.env after an intended change with: ... --update
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/run-bids-validation.yml"
FIXTURES = ROOT / "scripts/fixtures/bids-annex"
KEYS = ("real_errors", "annex_errors", "annex_located_errors")
GATE_PATHONLY = '(($pathonly | index($c)) == null)'
GATE_UNREADABLE = 'elif ($U[$l] // false) | not then "annex_real"'
GATE_RMET = 'if (!(a[n] in have)) print $1'


def step_script(name: str) -> str:
    wf = yaml.safe_load(WORKFLOW.read_text())
    for job in wf["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == name:
                return step["run"]
    raise SystemExit(f"step not found: {name}")


def mutate(script: str, old: str, new: str) -> str:
    assert old in script, f"mutation target vanished: {old}"
    return script.replace(old, new)


def run_check(case: Path, script: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        work = tmp / "work"
        (work / ".nemar").mkdir(parents=True)
        (work / ".nemar/validation.json").write_text((case / "validation.json").read_text())
        annex = tmp / "annex"
        annex.mkdir()
        for name in ("annex-files.txt", "annex-unresolved.txt"):
            (annex / name).write_text((case / name).read_text())
        out = tmp / "github_output"
        out.touch()
        env = {**os.environ, "ANNEX_DIR": str(annex), "GITHUB_OUTPUT": str(out)}
        subprocess.run(["bash", "-c", script], cwd=work, env=env, check=True, capture_output=True)
        got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
        return "".join(f"{k}={got[k]}\n" for k in KEYS)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
        cwd=repo, check=True, capture_output=True,
    )


def run_collect(script: str, with_branch: bool = True) -> set:
    """Real repo: two symlinks (one key has an rmet log), one pointer file."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        (repo / "sub-1").mkdir(parents=True)
        obj = "../.git/annex/objects/aa/bb/{k}/{k}"
        for name, key in (("a.nii", "SHA256E-s1--aaa.nii"), ("b.nii", "SHA256E-s1--bbb.nii")):
            os.symlink("../" + obj.format(k=key), repo / "sub-1" / name)
        (repo / "sub-1/events.tsv").write_text("/annex/objects/SHA256E-s9--ccc.tsv\n")
        (repo / "plain.txt").write_text("not annexed\n")
        git(repo.parent, "init", "-q", "repo")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "data")
        main = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True,
                              capture_output=True, text=True).stdout.strip()
        if with_branch:
            git(repo, "checkout", "-q", "--orphan", "git-annex")
            git(repo, "rm", "-rq", "--cached", ".")
            for f in ("sub-1", "plain.txt"):
                subprocess.run(["rm", "-rf", str(repo / f)], check=True)
            logdir = repo / "123/456"
            logdir.mkdir(parents=True)
            (logdir / "SHA256E-s1--aaa.nii.log.rmet").write_text("x\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "annex")
            git(repo, "checkout", "-q", main)
        annex = Path(tmp) / "annex"
        annex.mkdir()
        env = {**os.environ, "ANNEX_DIR": str(annex)}
        subprocess.run(["bash", "-c", script], cwd=repo, env=env, check=True, capture_output=True)
        return set((annex / "annex-unresolved.txt").read_text().split())


def main() -> int:
    update = "--update" in sys.argv
    check = step_script("Check validation result")
    collect = step_script("Collect git-annex pointer files")
    failed = 0

    def report(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failed
        failed += not ok
        print(("ok   " if ok else "FAIL ") + label + ("  " + detail if detail else ""))

    cases = sorted(p for p in FIXTURES.iterdir() if p.is_dir())
    for case in cases:
        actual = run_check(case, check)
        exp = case / "expected.env"
        if update:
            exp.write_text(actual)
        report(exp.read_text() == actual, case.name, actual.replace("\n", " "))

    # Mutations must flip at least one fixture result.
    for label, old, new in (
        ("path-only guard removed", GATE_PATHONLY, "true"),
        ("unreadable gate removed", GATE_UNREADABLE, 'elif false then "annex_real"'),
    ):
        mutated = mutate(check, old, new)
        flipped = [c.name for c in cases if run_check(c, mutated) != (c / "expected.env").read_text()]
        report(bool(flipped), "mutation caught: " + label, ",".join(flipped))

    # Collect step: per-key resolution.
    want = {"/sub-1/b.nii", "/sub-1/events.tsv"}
    got = run_collect(collect)
    report(got == want, "collect: per-key rmet resolution", str(sorted(got)))
    got = run_collect(collect, with_branch=False)
    report(got == want | {"/sub-1/a.nii"}, "collect: no git-annex branch", str(sorted(got)))
    mutated = mutate(collect, GATE_RMET, "if (0) print $1")
    got = run_collect(mutated)
    report(got != want, "mutation caught: rmet gate removed", str(sorted(got)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
