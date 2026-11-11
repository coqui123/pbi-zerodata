"""Round 18 Item 5: legitimate commands stay allowed. Run: python tests/test_guard_false_positives.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
from pbz import guard  # noqa: E402


class GuardFalsePositives(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(os.path.join(self.root, ".pbz"))
        os.makedirs(os.path.join(self.root, "pbz-workspace", "X.Report"), exist_ok=True)
        with open(os.path.join(self.root, ".pbz", "policy.json"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1, "allow": []}))
        with open(os.path.join(self.root, "Demo.pbip"), "w", encoding="utf-8") as handle:
            handle.write("{}")

    def test_allow_list(self):
        allowed = [
            "git status", "git diff", "git log -1", "git show HEAD", "git branch",
            "pbz status", "pbz validate", "pbz describe", "pbz docs", "pbz review",
            "Get-Content pbz-workspace\\X.Report\\page.json",
            "Get-Content .pbzignore",
            "Get-Content notes.md",
            "Select-String -Path notes.md -Pattern hello",
            "dir pbz-workspace", "ls pbz-workspace", "dir .",
            "Set-Content notes.md hello", "Add-Content notes.md more",
            "Out-File notes.md", "Clear-Content notes.md",
            "Remove-Item pbz-workspace\\node_modules\\pkg",
            "Remove-Item -Recurse pbz-workspace\\node_modules\\pkg",
            "rm -rf ~/workspace-old/node_modules",
            "Copy-Item notes.md backup.md",
            "Copy-Item -Recurse pbz-workspace\\X.Report D:\\out",
            "Move-Item notes.md notes2.md",
            "python -c \"print(1)\"", "echo hello", "echo policy",
            "cd pbz-workspace", "pwd", "Get-ChildItem",
            "Select-String notes.md pattern",
            "pbz interactions capabilities",
            "pbz check", "pbz handoff", "pbz freshness",
            "New-Item notes.md -ItemType File",
            "Rename-Item notes.md notes.bak",
            "Format-Hex notes.md",
        ]
        self.assertGreaterEqual(len(allowed), 40)
        for cmd in allowed:
            if hasattr(guard, "_reset"):
                guard._reset()
            self.assertIsNone(guard.check_shell(cmd, self.root), cmd)


if __name__ == "__main__":
    unittest.main()
