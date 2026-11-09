"""Round 18 Item 3. Run: python tests/test_guard_protected_mutations.py
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
from pbz.common import Workspace  # noqa: E402


class GuardProtectedMutations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(os.path.join(self.root, ".pbz"))
        self.policy = os.path.join(self.root, ".pbz", "policy.json")
        with open(self.policy, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1}))
        with open(os.path.join(self.root, "Demo.pbip"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        guard._reset() if hasattr(guard, "_reset") else None

    def _shell(self, cmd):
        if hasattr(guard, "_reset"):
            guard._reset()
        return guard.check_shell(cmd, self.root)

    def test_content_writes_denied(self):
        for cmd in (
            "Set-Content .pbz\\policy.json x",
            "Add-Content .pbz/policy.json x",
            'Set-Content -Path ".pbz\\policy.json" x',
            "Set-Content -Path:.pbz\\policy.json -Value x",
            "Add-Content .pbz/policy.json x",
            "Out-File .pbz\\policy.json",
            "Clear-Content .pbz\\policy.json",
            "Tee-Object .pbz\\policy.json",
            'echo x > ".pbz\\policy.json"',
            "echo x >.pbz/policy.json",
            "New-Item -Path notes.md -Value x .pbz\\policy.json",
            '"hello" | Set-Content .pbz\\policy.json',
            "Remove-Item .pbz\\policy.json",
            "Remove-Item ..\\.pbz\\policy.json",
            "$p='.pbz\\policy.json'; Set-Content $p x",
            "$p='.pbz\\policy.json'; Remove-Item $p",
            "echo x > \"C:\\work space\\.pbz\\policy.json\"",
            "$p='.pbz\\policy.json'; echo x > $p",
        ):
            self.assertEqual(self._shell(cmd), "policy", cmd)
        self.assertEqual(open(self.policy, encoding="utf-8").read()[:1], "{")

    def test_wildcard_content_writes_denied(self):
        for cmd in (
            "Set-Content -Path '.pb[z]\\policy.jso[n]' -Value '{}'",
            "Add-Content -Path '.pb[z]\\policy.jso[n]' -Value x",
            "Clear-Content -Path '.pb[z]\\policy.jso[n]'",
            "Set-Content .pb?\\policy.json x",
            "Out-File .pb*\\policy.json",
            "printf '{}' | tee .p{bz,unused}/pol{icy,unused}.json",
            "Set-Content .p{bz,unused}/pol{icy,unused}.json x",
            "echo x > .p{bz,unused}/pol{icy,unused}.json",
            "New-Item -ItemType File -Value '{}' .p{bz,unused}/pol{icy,unused}.json",
            "Remove-Item -Recurse -Force .pbz.",
            "rd /s /q .pbz.",
            "Set-Content .pbz\\policy.json. x",
            "Set-Content .pbz\\policy.json::$DATA x",
        ):
            self.assertEqual(self._shell(cmd), "policy", cmd)
        self.assertEqual(open(self.policy, encoding="utf-8").read()[:1], "{")

    def test_short_names_denied(self):
        for cmd in (
            "Set-Content PBZ~1\\POLICY~1.JSO '{}'",
            "Remove-Item -Recurse -Force PBZ~1",
            "rd /s /q PBZ~1",
            "Move-Item PBZ~1 ..\\x",
            "echo x > PBZ~1\\POLICY~1.JSO",
        ):
            self.assertEqual(self._shell(cmd), "policy", cmd)
        if hasattr(guard, "_reset"):
            guard._reset()
        inside = os.path.join(self.root, ".pbz")
        self.assertEqual(guard.check_shell("Set-Content POLICY~1.JSO x", inside), "policy")
        self.assertIsNone(self._shell("Set-Content notes.txt x"))
        self.assertEqual(open(self.policy, encoding="utf-8").read()[:1], "{")

    def test_stream_write_inside_pbz_denied(self):
        inside = os.path.join(self.root, ".pbz")
        if hasattr(guard, "_reset"):
            guard._reset()
        self.assertEqual(guard.check_shell("Set-Content policy.json::$DATA x", inside), "policy")

    def test_wildcard_from_inside_pbz(self):
        inside = os.path.join(self.root, ".pbz")
        for cmd in (
            "Set-Content -Path '.\\pol*.json' -Value '{}'",
            "Set-Content pol*.json x",
            "Add-Content policy.jso? x",
            "Clear-Content .\\pol*.json",
        ):
            if hasattr(guard, "_reset"):
                guard._reset()
            self.assertEqual(guard.check_shell(cmd, inside), "policy", cmd)
        if hasattr(guard, "_reset"):
            guard._reset()
        self.assertIsNone(guard.check_shell("Set-Content notes.txt x", inside))

    def test_notes_write_allowed(self):
        self.assertIsNone(self._shell("Set-Content notes.md hello"))
        self.assertIsNone(self._shell("Set-Content .pbzignore hello"))
        self.assertIsNone(self._shell("Set-Content .pbzignore. hello"))
        self.assertIsNone(self._shell("Set-Content notes.md. hello"))
        self.assertIsNone(self._shell("Add-Content notes.md more"))
        self.assertIsNone(self._shell("tee notes{1,2}.txt"))
        self.assertIsNone(self._shell("New-Item notes{1,2}.txt"))


if __name__ == "__main__":
    unittest.main()
