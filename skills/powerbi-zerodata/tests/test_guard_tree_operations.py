"""Round 18 Item 4. Run: python tests/test_guard_tree_operations.py
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


class GuardTreeOperations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(os.path.join(self.root, ".pbz"))
        with open(os.path.join(self.root, ".pbz", "policy.json"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1}))
        with open(os.path.join(self.root, "Demo.pbip"), "w", encoding="utf-8") as handle:
            handle.write("{}")

    def _shell(self, cmd, cwd=None):
        if hasattr(guard, "_reset"):
            guard._reset()
        return guard.check_shell(cmd, cwd or self.root)

    def test_recursive_pbz_copy_and_root_delete_denied(self):
        self.assertEqual(self._shell("Copy-Item -Recurse .pbz ..\\backup"), "policy")
        self.assertEqual(self._shell("Copy-Item . ..\\backup -Recurse"), "policy")
        self.assertEqual(self._shell("Move-Item -Recurse .pbz ..\\old"), "policy")
        self.assertEqual(self._shell("Remove-Item -Recurse ."), "policy")
        self.assertEqual(self._shell("Remove-Item * -Recurse"), "policy")
        self.assertEqual(self._shell("Remove-Item -Recurse .\\*"), "policy")
        self.assertEqual(self._shell("rm -rf ."), "policy")
        self.assertEqual(self._shell("rm -fr ."), "policy")
        self.assertEqual(self._shell("Remove-Item C:\\ -Recurse"), "policy")
        self.assertEqual(self._shell("Remove-Item C:\\* -Recurse"), "policy")
        child = os.path.join(self.root, "sub")
        os.makedirs(child, exist_ok=True)
        self.assertEqual(self._shell("Remove-Item -Recurse /", child), "policy")
        rooted = "/" + self.root.split(":", 1)[1].replace("\\", "/").lstrip("/")
        self.assertEqual(self._shell(f"Remove-Item -Recurse -Force {rooted}", child), "policy")
        drive = os.path.splitdrive(self.root)[0][0].lower()
        self.assertEqual(self._shell(f"rm -rf /{drive}{rooted}", child), "policy")
        self.assertEqual(self._shell(f"Move-Item {self.root} {os.path.join(os.path.dirname(self.root), 'ws-old')}"), "policy")
        self.assertEqual(self._shell(f"robocopy {self.root} C:\\empty /MIR"), "policy")
        parent = os.path.dirname(self.root)
        self.assertEqual(self._shell(f"Remove-Item -Recurse {parent}"), "policy")
        child = os.path.join(self.root, "sub")
        os.makedirs(child, exist_ok=True)
        self.assertEqual(self._shell("Remove-Item -Recurse ..", child), "policy")
        self.assertEqual(self._shell("Copy-Item -Recurse .. ..\\backup", child), "policy")
        sibling = os.path.join(parent, "workspace-old")
        self.assertIsNone(self._shell(f"robocopy {sibling} C:\\empty /MIR"))

    def test_subtree_remove_allowed(self):
        os.makedirs(os.path.join(self.root, "pbz-workspace", "node_modules"), exist_ok=True)
        self.assertIsNone(self._shell("Remove-Item pbz-workspace\\node_modules\\pkg"))

    def test_msys_drive_root_and_recursive_forms(self):
        child = os.path.join(self.root, "sub")
        os.makedirs(child, exist_ok=True)
        drive = os.path.splitdrive(self.root)[0][0].lower()
        self.assertEqual(self._shell(f"rm -rf /{drive}/", child), "policy")
        self.assertEqual(self._shell(f"rm -rf /{drive}", child), "policy")
        self.assertEqual(self._shell(f"rm -rf /{drive}/ notes", self.root), "policy")
        top = self.root.split(":", 1)[1].lstrip("\\").split("\\")[0]
        self.assertEqual(self._shell(f"Remove-Item -Recurse -Force /{top}", child), "policy")
        self.assertEqual(self._shell("rm --recursive ."), "policy")
        self.assertEqual(self._shell("cp --recursive . C:\\empty"), "policy")
        parent = os.path.dirname(self.root)
        prefix = os.path.basename(self.root)[:1]
        self.assertEqual(self._shell(f"Remove-Item -Recurse {os.path.join(parent, prefix + '*')}"), "policy")
        self.assertIsNone(self._shell(f"Remove-Item -Recurse {os.path.join(parent, 'workspace-old*')}"))
        self.assertIsNone(self._shell("rd /s /q sub"))
        self.assertIsNone(self._shell("del /s /q sub\\file.txt"))
        sibling = os.path.join(parent, "workspace-old")
        self.assertIsNone(self._shell(f"robocopy {sibling} C:\\empty /MIR"))
        self.assertEqual(self._shell("robocopy C:\\empty . /PURGE"), "policy")
        self.assertEqual(self._shell("robocopy C:\\empty .. /PURGE", child), "policy")
        self.assertEqual(self._shell("robocopy C:\\empty . /E /PURGE"), "policy")
        self.assertIsNone(self._shell(f"robocopy {sibling} C:\\empty /PURGE"))
        self.assertIsNone(self._shell(f"robocopy {sibling} C:\\empty /E /PURGE"))
        drive_root, tail = os.path.splitdrive(self.root)
        parts = [part for part in tail.split("\\") if part]
        if len(parts) >= 2:
            ancestor = parts[-2]
            pattern = ancestor[:-1] + "?"
            self.assertEqual(
                self._shell("Remove-Item -Recurse -Force " + os.path.join(drive_root + "\\", *parts[:-2], pattern)),
                "policy")
        self.assertEqual(self._shell("cp -a . ..\\backup"), "policy")
        self.assertEqual(self._shell("cp --archive . ..\\backup"), "policy")
        self.assertEqual(self._shell("cp -a .. ..\\backup", child), "policy")
        os.makedirs(os.path.join(self.root, "pbz-workspace", "node_modules", "pkg"), exist_ok=True)
        self.assertIsNone(self._shell("cp -a pbz-workspace\\node_modules\\pkg C:\\empty"))
        top = self.root.split(":", 1)[1].lstrip("\\").split("\\")[0]
        self.assertEqual(self._shell(f"rd -Recurse -Force /{top}", child), "policy")
        self.assertEqual(self._shell(f"del -Recurse /{top}", child), "policy")
        self.assertEqual(self._shell(f"move /{top} D:\\x", child), "policy")
        parent = os.path.dirname(self.root)
        name = os.path.basename(self.root)
        bracket = name[:1] + "[" + name[1:] + "]"
        self.assertEqual(self._shell("Remove-Item -Recurse " + os.path.join(parent, bracket)), "policy")
        self.assertEqual(self._shell("Remove-Item -Recurse -Force -LiteralPath:..", child), "policy")
        self.assertEqual(self._shell("Copy-Item -Recurse -Path:.. -Destination:D:\\out", child), "policy")
        self.assertEqual(self._shell("Move-Item -LiteralPath:.. -Destination:D:\\out", child), "policy")
        self.assertIsNone(self._shell("Remove-Item -Recurse pbz-workspace\\node_modules\\pkg"))
        self.assertEqual(self._shell("Move-Item .pb? ..\\x"), "policy")
        self.assertEqual(self._shell("move .pb* D:\\x"), "policy")
        self.assertEqual(self._shell("Copy-Item -Recurse .pb[z] D:\\out"), "policy")
        self.assertEqual(self._shell("Remove-Item -Recurse -Force .pb*"), "policy")
        self.assertEqual(self._shell("rd /s /q .pb?"), "policy")
        self.assertIsNone(self._shell("Copy-Item .pbzignore D:\\out"))
        self.assertEqual(self._shell("Remove-Item -Recurse -Force ..,x", child), "policy")
        self.assertEqual(self._shell("Copy-Item -Recurse ..,x D:\\out", child), "policy")
        self.assertIsNone(self._shell("Remove-Item -Recurse a,b", child))
        self.assertEqual(self._shell("Remove-Item '.pb[z]\\policy.jso[n]'"), "policy")
        self.assertEqual(self._shell("del .pb?\\*.json"), "policy")
        self.assertIsNone(self._shell("rm sub/*.json"))
        self.assertEqual(self._shell("rm -rf ..{,}", child), "policy")
        self.assertEqual(self._shell("rm -rf {..,x}", child), "policy")
        self.assertIsNone(self._shell("rm -rf {a,b}", child))
        self.assertEqual(self._shell("rm -rf ..{,}{,}", child), "policy")
        self.assertEqual(self._shell("rm -rf {..,x}{,}", child), "policy")
        self.assertEqual(self._shell("rm -rf {.,a}{.,b}", child), "policy")
        self.assertEqual(self._shell("rm -rf {{..,x},y}", child), "policy")
        self.assertIsNone(self._shell("rm -rf {a,b}{c,d}", child))
        self.assertEqual(self._shell("rm -rf {a,b", child), "policy")
        self.assertIsNone(self._shell("rm -rf {notes}", child))
        self.assertEqual(self._shell("rm -rf {../../ws,{x}}", child), "policy")
        self.assertEqual(self._shell("rm -rf {{x},..}", child), "policy")
        self.assertEqual(self._shell("cp -a {{x},..} D:\\out", child), "policy")
        self.assertEqual(self._shell("mv {{x},..} D:\\out", child), "policy")
        self.assertEqual(self._shell("rm -rf .p{bz,unused}"), "policy")
        self.assertEqual(self._shell("rm -rf .pb{z,}"), "policy")
        self.assertEqual(self._shell("mv {.pbz,x} D:\\x"), "policy")
        self.assertEqual(self._shell("cp -r {.pbz,x} D:\\out"), "policy")
        self.assertEqual(self._shell("cp -a .p{bz,unused} ..\\archive"), "policy")
        self.assertEqual(self._shell("rd /s/q -x ..", child), "policy")
        self.assertEqual(self._shell("rd /s -x /c", child), "policy")
        self.assertEqual(self._shell("rd /s -x /c/", child), "policy")
        self.assertEqual(self._shell("del /s -x /c", child), "policy")
        self.assertEqual(self._shell("rd /s -x /c"), "policy")
        self.assertEqual(self._shell("rd /s -x /c/"), "policy")
        self.assertEqual(self._shell("del /s -x /c"), "policy")
        self.assertEqual(self._shell("Remove-Item -Recurse .`.", child), "policy")
        self.assertEqual(self._shell('Remove-Item -Recurse ".`."', child), "policy")
        self.assertEqual(self._shell("rd /s /q .^.", child), "policy")
        self.assertEqual(self._shell("Copy-Item -Recurse .`. D:\\out", child), "policy")
        self.assertIsNone(self._shell("Remove-Item -Recurse sub\\node_modules", child))

    def test_home_and_dotted_workspace_names(self):
        child = os.path.join(self.root, "sub")
        os.makedirs(child, exist_ok=True)
        parent = os.path.dirname(self.root)
        self.assertEqual(self._shell(f"Remove-Item -Recurse -Force {self.root}.", child), "policy")
        self.assertEqual(self._shell(f"Copy-Item -Recurse {self.root}. D:\\out", child), "policy")
        self.assertEqual(self._shell(f"Remove-Item -Recurse -Force {parent}.", child), "policy")
        self.assertIsNone(self._shell("Remove-Item -Recurse " + os.path.join(parent, "workspace-old.")))
        old_home = os.environ.get("HOME")
        old_profile = os.environ.get("USERPROFILE")
        os.environ["HOME"] = parent
        os.environ["USERPROFILE"] = parent
        try:
            self.assertEqual(self._shell("rm -rf ~", child), "policy")
            self.assertEqual(self._shell("Remove-Item -Recurse -Force ~", child), "policy")
            self.assertEqual(self._shell("rm -rf ~/*", child), "policy")
            self.assertEqual(self._shell("cp -r ~ D:\\x", child), "policy")
            self.assertIsNone(self._shell("rm -rf ~/workspace-old", child))
            self.assertEqual(self._shell("rm -rf $HOME", child), "policy")
            self.assertEqual(self._shell("Remove-Item -Recurse $env:USERPROFILE", child), "policy")
            self.assertEqual(self._shell("rm -rf %USERPROFILE%", child), "policy")
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            if old_profile is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = old_profile
        self.assertEqual(self._shell("Copy-Item -Rec:$true .. D:\\out", child), "policy")
        self.assertIsNone(self._shell("Copy-Item -Rec:$true sub\\notes D:\\out", child))
        drive = os.path.splitdrive(self.root)[0]
        self.assertEqual(self._shell(f"Remove-Item -Recurse -Force {drive}..", child), "policy")
        self.assertEqual(self._shell(f"rd /s /q {drive}..", child), "policy")
        self.assertEqual(self._shell(f"Remove-Item -Recurse -Force {drive}..\\..", child), "policy")
        self.assertEqual(self._shell(f"rd /s /q {drive}.", self.root), "policy")
        self.assertEqual(self._shell(f"Remove-Item -Recurse -Force {drive}.pbz"), "policy")
        self.assertIsNone(self._shell(f"Remove-Item -Recurse {drive}sub\\node_modules", child))
        self.assertEqual(self._shell("del /f/s/q -x *"), "policy")
        self.assertEqual(self._shell("cp -a ..{,}{,} D:\\out", child), "policy")
        self.assertEqual(self._shell("mv {..,x}{,} D:\\out", child), "policy")
        self.assertEqual(self._shell("rd /s/q ..", child), "policy")
        self.assertEqual(self._shell(f"RD /S/Q {self.root}", child), "policy")
        self.assertEqual(self._shell("del /f/s/q *"), "policy")
        self.assertIsNone(self._shell("rd /s/q sub", child))
        saved_cd = os.environ.pop("CD", None)
        try:
            self.assertEqual(self._shell("rd /s /q %CD%"), "policy")
            self.assertEqual(self._shell("rd /s /q %CD%\\..", child), "policy")
            self.assertEqual(self._shell("Remove-Item -Recurse !CD!", child), "policy")
        finally:
            if saved_cd is not None:
                os.environ["CD"] = saved_cd
        self.assertEqual(self._shell("rm -rf C:\\Users\\ALEXAN~1", child), "policy")
        self.assertEqual(self._shell("Remove-Item -Recurse sub\\pkg~1", child), "policy")
        self.assertEqual(self._shell("Remove-Item -Recurse -Force (Split-Path (Get-Location))", child), "policy")
        self.assertEqual(self._shell("Copy-Item -Recurse (Resolve-Path ..) D:\\x", child), "policy")
        self.assertEqual(self._shell("Remove-Item -Recurse (Get-Location).Path"), "policy")
        self.assertEqual(self._shell("Remove-Item -Recurse [IO.Path]::GetFullPath('..')", child), "policy")
        self.assertEqual(self._shell("TMP=..; rm -rf $TMP", child), "policy")
        self.assertEqual(self._shell('export APPDATA=..; rm -rf "$APPDATA"', child), "policy")
        self.assertEqual(self._shell("rm -rf $PWD/..", child), "policy")
        self.assertEqual(self._shell("HOME=..; rm -rf ~", child), "policy")
        self.assertIsNone(self._shell("rm -rf build", child))
        parent = os.path.dirname(self.root)
        base = os.path.basename(self.root)
        wild = os.path.join(parent, base[:-1] + "[" + base[-1] + "]")
        self.assertEqual(self._shell(f"Copy-Item -Recurse {wild}\\* D:\\out", child), "policy")
        self.assertEqual(self._shell(f"Remove-Item -Recurse {wild}\\*", child), "policy")
        self.assertIsNone(self._shell(f"Copy-Item -Recurse {os.path.join(parent, 'workspace-ol[d]')}\\* D:\\out", child))
        self.assertIsNone(self._shell(f"Copy-Item -Recurse {os.path.join(wild, 'node_modules')} D:\\out", child))
        self.assertEqual(self._shell("New-PSDrive -Name ws -PSProvider FileSystem -Root ..; Remove-Item -Recurse ws:\\", child), "policy")
        self.assertEqual(self._shell("Remove-Item -Recurse ws:", child), "policy")
        self.assertEqual(self._shell("Copy-Item -Recurse ws:\\ D:\\out", child), "policy")
        self.assertEqual(self._shell("rd /s /q W:\\", child), "policy")
        self.assertIsNone(self._shell("Copy-Item -Recurse sub D:\\out", child))
        self.assertEqual(self._shell("Move-Item * ..\\bak"), "policy")
        self.assertEqual(self._shell("Move-Item .\\* D:\\x"), "policy")
        self.assertEqual(self._shell("Move-Item ..\\* D:\\x", child), "policy")
        self.assertIsNone(self._shell("Move-Item notes.md notes2.md"))
        self.assertIsNone(self._shell("Move-Item sub\\* ..\\ws-old\\"))
        self.assertEqual(self._shell("rm -rf .pb{z..z}"), "policy")
        self.assertEqual(self._shell("cp -r .pb{z..z} backup"), "policy")
        self.assertEqual(self._shell("mv .pb{z..z} backup"), "policy")
        self.assertEqual(self._shell("printf x | tee .pb{z..z}/polic{y..y}.json"), "policy")
        self.assertEqual(self._shell("rm -rf ../../w{s..s}", child), "policy")
        self.assertIsNone(self._shell("rm -rf {notes}"))
        self.assertEqual(self._shell("del /s /q p*"), "policy")
        self.assertEqual(self._shell("del /s /q pol*.json"), "policy")
        self.assertEqual(self._shell("del /s /q policy.*"), "policy")
        self.assertEqual(self._shell("erase /s /q p*"), "policy")
        self.assertEqual(self._shell("del /s /q ..\\p*", child), "policy")
        self.assertIsNone(self._shell("del /s /q *.txt", child))
        self.assertIsNone(self._shell("del /s /q sub\\*.txt", child))
        self.assertIsNone(self._shell("Remove-Item -Recurse sub\\node_modules", child))

    def test_existing_short_prefix_is_not_a_policy_name(self):
        """An 8.3 profile directory (`RUNNER~1`) is not `.pbz`. A missing `pkg~1` still is refused."""
        import ctypes
        parent = tempfile.mkdtemp(prefix="longname_workspace_")
        self.addCleanup(lambda: __import__("shutil").rmtree(parent, ignore_errors=True))
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(parent, buf, len(buf))
        if n <= 0 or os.path.normcase(buf.value) == os.path.normcase(parent):
            self.skipTest("8.3 short names are unavailable")
        root = os.path.join(buf.value, "ws")
        os.makedirs(os.path.join(root, ".pbz"))
        with open(os.path.join(root, ".pbz", "policy.json"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1}))
        with open(os.path.join(root, "Demo.pbip"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        sibling = os.path.join(os.path.dirname(root), "workspace-old")
        self.assertIsNone(guard.check_shell(f"robocopy {sibling} C:\\empty /MIR", root))
        self.assertIsNone(guard.check_shell(
            "Remove-Item -Recurse " + os.path.join(os.path.dirname(root), "workspace-old."), root))
        self.assertEqual(guard.check_shell("Remove-Item -Recurse sub\\pkg~1", root), "policy")


if __name__ == "__main__":
    unittest.main()
