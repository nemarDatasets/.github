#!/usr/bin/env python3
"""
Fixture test for the "Check validation result" step of
.github/workflows/run-bids-validation.yml (annex filtering).

No mocks: the step's real shell text is extracted from the workflow and run
with bash and jq against real validator output. Each directory under
scripts/fixtures/bids-annex/ holds:
  validation.json        error entries from `bids-validator 3.0.2 --json`
  annex-files.txt        annex pointers (symlinks and pointer files), trimmed
                         to the issue locations plus every .json/.tsv/... pointer
  annex-unresolved.txt   pointers whose content was unreadable (empty when the
                         git-annex branch was fetched and content resolved)
  expected.env           real_errors / annex_errors / annex_located_errors
Fixtures come from on008446, on006673, on002799, on007463, on004551; the
"-nocontent" runs had no git-annex branch, the "-content" runs had it.

Run: python3 scripts/test_bids_annex_filter.py   (needs bash, jq, PyYAML)
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


def step_script(name: str) -> str:
    wf = yaml.safe_load(WORKFLOW.read_text())
    for job in wf["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == name:
                return step["run"]
    raise SystemExit(f"step not found: {name}")


def run_case(case: Path, script: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        work = tmp / "work"
        (work / ".nemar").mkdir(parents=True)
        (work / ".nemar/validation.json").write_text((case / "validation.json").read_text())
        annex = tmp / "annex"
        annex.mkdir()
        (annex / "annex-files.txt").write_text((case / "annex-files.txt").read_text())
        (annex / "annex-unresolved.txt").write_text((case / "annex-unresolved.txt").read_text())
        out = tmp / "github_output"
        out.touch()
        env = {**os.environ, "ANNEX_DIR": str(annex), "GITHUB_OUTPUT": str(out)}
        subprocess.run(["bash", "-c", script], cwd=work, env=env, check=True, capture_output=True)
        return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


def main() -> int:
    update = "--update" in sys.argv
    script = step_script("Check validation result")
    failed = 0
    for case in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        got = run_case(case, script)
        actual = "".join(f"{k}={got[k]}\n" for k in KEYS)
        exp_file = case / "expected.env"
        if update:
            exp_file.write_text(actual)
        ok = exp_file.read_text() == actual
        failed += not ok
        print(("ok   " if ok else "FAIL ") + case.name + "  " + actual.replace("\n", " "))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
