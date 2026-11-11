"""Run the offline suites named in .github/workflows/tests.yml.

Git hooks and a local check use this so a commit or push is refused when CI would fail.
The suite list stays in the workflow; test_ci_registration.py keeps that list complete.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(SKILL))
YML = os.path.join(ROOT, ".github", "workflows", "tests.yml")


def suites(text: str) -> list[str]:
    match = re.search(r"\$suites = (.*?)\n\s+\$failed", text, re.S)
    if not match:
        raise SystemExit("suite list not found in .github/workflows/tests.yml")
    return re.findall(r'"([^"]+)"', match.group(1))


def main() -> int:
    os.chdir(SKILL)
    with open(YML, encoding="utf-8") as handle:
        names = suites(handle.read())
    failed: list[str] = []
    for name in names:
        print(f"::group::test_{name}", flush=True)
        result = subprocess.run([sys.executable, os.path.join("tests", f"test_{name}.py")])
        print("::endgroup::", flush=True)
        if result.returncode != 0:
            failed.append(name)
    print("::group::test_engine --offline", flush=True)
    engine = subprocess.run([sys.executable, os.path.join("tests", "test_engine.py"), "--offline"])
    print("::endgroup::", flush=True)
    if engine.returncode != 0:
        failed.append("engine-offline")
    if failed:
        print("failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"offline CI suites passed ({len(names)} + engine --offline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
