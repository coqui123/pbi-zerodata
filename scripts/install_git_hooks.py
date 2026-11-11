"""Copy the CI git hooks into this clone. Does not change git config."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "scripts", "git-hooks")
NAMES = ("pre-commit", "pre-push")


def main() -> int:
    git_dir = subprocess.check_output(
        ["git", "rev-parse", "--git-path", "hooks"], cwd=ROOT, text=True
    ).strip()
    hooks = git_dir if os.path.isabs(git_dir) else os.path.join(ROOT, git_dir)
    os.makedirs(hooks, exist_ok=True)
    for name in NAMES:
        src = os.path.join(SOURCE, name)
        dest = os.path.join(hooks, name)
        shutil.copyfile(src, dest)
        os.chmod(dest, os.stat(dest).st_mode | stat.S_IEXEC)
        print(f"installed {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
