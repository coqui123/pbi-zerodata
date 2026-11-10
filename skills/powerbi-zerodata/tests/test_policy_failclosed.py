"""Present policy fails closed in the broker and the guard.

Run: python tests/test_policy_failclosed.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

from pbz.common import PbzError, Workspace  # noqa: E402
from pbz import guard, policy  # noqa: E402
from pbz.cli import admin  # noqa: E402

MALFORMED = "policy is malformed; fix or remove .pbz/policy.json by hand in your own terminal"


def _reset():
    guard._ROOT.clear()
    guard._SYN.clear()
    guard._POL.clear()


def _same_path(left, right) -> bool:
    """True when both strings name one existing file, including 8.3 vs long spellings."""
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


class PolicyFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["PBZ_PRIVATE_ROOT"] = os.path.join(self.tmp.name, "private")
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        self.root = os.path.join(self.tmp.name, "ws")
        self.ws = self._workspace(self.root)
        self.outside = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside.cleanup)
        _reset()

    def _workspace(self, root, body=None, pbip="X.pbip"):
        os.makedirs(root, exist_ok=True)
        open(os.path.join(root, pbip), "w", encoding="utf-8").close()
        mirror = os.path.join(root, "pbz-workspace", "X.SemanticModel", "definition")
        os.makedirs(mirror, exist_ok=True)
        with open(os.path.join(mirror, "model.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("table T\n")
        real = os.path.join(root, "X.SemanticModel", "definition")
        os.makedirs(real, exist_ok=True)
        with open(os.path.join(real, "model.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("table Secret\n")
        if body is not None:
            os.makedirs(os.path.join(root, ".pbz"), exist_ok=True)
            path = os.path.join(root, ".pbz", "policy.json")
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            with open(path, "wb") as handle:
                handle.write(data)
        return Workspace(root)

    def _shell(self, cmd, cwd):
        _reset()
        return guard.check_shell(cmd, cwd)

    def _deny(self, cmd, cwd, reason="policy"):
        self.assertEqual(self._shell(cmd, cwd), reason, cmd)

    def _allow(self, cmd, cwd):
        self.assertIsNone(self._shell(cmd, cwd), cmd)

    def test_missing_defaults_and_valid_shapes_load(self):
        loaded = policy.load(self.ws)
        self.assertEqual(loaded["disclosure"], "shape")
        self.assertFalse(loaded["synthetic"])
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("")
        _reset()
        self.assertEqual(policy.load(self.ws)["disclosure"], "shape")
        self.assertIsNone(guard.classify_path(os.path.join(self.ws.mirror, "X.SemanticModel", "definition", "model.tmdl")))
        for body in ({}, {"synthetic": True}, {"version": 1, "note": "kept"}):
            _reset()
            policy.save(self.ws, {"version": 1, **body} if body else {"version": 1})
            if body == {}:
                with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
                    handle.write("{}")
            elif body == {"synthetic": True}:
                with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({"synthetic": True}))
            else:
                with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(body))
            _reset()
            loaded = policy.load(self.ws)
            self.assertTrue(policy.security_values_ok(json.loads(open(self.ws.policy_path, encoding="utf-8").read())))
            self.assertTrue(guard.security_values_ok(json.loads(open(self.ws.policy_path, encoding="utf-8").read())))
            if "note" in body:
                self.assertEqual(loaded["note"], "kept")

    def test_invalid_values_refuse_and_lock_down(self):
        samples = [
            {"disclosure": "open"},
            {"allow": "*"},
            {"deny": 5},
            {"version": True},
            {"version": 1.0},
            {"version": 1, "budgets": []},
            {"version": 1, "budgets": {"session_checks": -1}},
            b"\xff\xfe",
        ]
        mirror = os.path.join(self.ws.mirror, "X.SemanticModel", "definition", "model.tmdl")
        for sample in samples:
            _reset()
            os.makedirs(self.ws.pbz_dir, exist_ok=True)
            with open(self.ws.policy_path, "wb") as handle:
                handle.write(sample if isinstance(sample, bytes) else json.dumps(sample).encode())
            with self.assertRaises(PbzError) as caught:
                policy.load(self.ws)
            self.assertEqual(caught.exception.code, "REFUSED")
            self.assertEqual(caught.exception.detail, MALFORMED)
            self.assertNotIn(self.root, caught.exception.detail)
            _reset()
            self.assertEqual(guard.classify_path(mirror), "error")

    def test_bom_policy_agrees(self):
        body = b"\xef\xbb\xbf" + json.dumps({"version": 1, "disclosure": "strict"}).encode()
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "wb") as handle:
            handle.write(body)
        self.assertEqual(policy.load(self.ws)["disclosure"], "strict")
        _reset()
        data = guard.read_policy_file(self.ws.root)
        self.assertEqual(data["disclosure"], "strict")
        self.assertNotIn("_pbz_lockdown", data)

    def test_permission_error_is_refused(self):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write('{"version": 1}')
        real = open

        def boom(path, *args, **kwargs):
            if str(path).endswith("policy.json"):
                raise PermissionError("denied")
            return real(path, *args, **kwargs)

        with patch("builtins.open", side_effect=boom):
            with self.assertRaises(PbzError) as caught:
                policy.load(self.ws)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotEqual(caught.exception.code, "LOCAL_STATE_UNAVAILABLE")

    def test_links_are_unsafe_and_lock_down(self):
        from _junction import make_junction
        external = self.outside.name
        with open(os.path.join(external, "policy.json"), "wb") as handle:
            handle.write(b"EXTERNAL")
        make_junction(self, self.ws.pbz_dir, external)
        with self.assertRaises(PbzError) as caught:
            policy.load(self.ws)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(os.path.join(external, "policy.json"), "rb").read(), b"EXTERNAL")
        _reset()
        mirror = os.path.join(self.root, "pbz-workspace", "X.SemanticModel", "definition", "model.tmdl")
        # The junction replaces .pbz; the mirror still lives under the workspace.
        if os.path.isfile(mirror):
            self.assertEqual(guard.classify_path(mirror), "error")

    def test_pbz_lstat_permission_error_locks_down(self):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "wb") as handle:
            handle.write(b'{"version": 1, "synthetic": true}')
        reads = {"n": 0}
        real_open = open
        real_lstat = os.lstat
        pbz = self.ws.pbz_dir

        def watch(target, *args, **kwargs):
            handle = real_open(target, *args, **kwargs)
            if _same_path(target, self.ws.policy_path):
                raw = handle.read

                def counted(*a, **k):
                    reads["n"] += 1
                    return raw(*a, **k)

                handle.read = counted
            return handle

        def lstat(path, *args, **kwargs):
            if _same_path(path, pbz):
                raise PermissionError("denied")
            return real_lstat(path, *args, **kwargs)

        _reset()
        with patch("os.lstat", side_effect=lstat), patch("builtins.open", side_effect=watch):
            data = guard.read_policy_file(self.ws.root)
        self.assertTrue(data.get("_pbz_lockdown"))
        self.assertNotEqual(data.get("synthetic"), True)
        self.assertEqual(reads["n"], 0)
        _reset()
        with patch("os.lstat", side_effect=lstat):
            mirror = os.path.join(self.ws.mirror, "X.SemanticModel", "definition", "model.tmdl")
            self.assertEqual(guard.classify_path(mirror), "error")

    def test_pbz_swapped_after_dir_check_locks_down(self):
        from _junction import make_junction
        import shutil
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "wb") as handle:
            handle.write(b'{"version": 1}')
        external = self.outside.name
        secret = os.path.join(external, "policy.json")
        with open(secret, "wb") as handle:
            handle.write(b'{"version": 1, "synthetic": true}')
        reads = {"n": 0}
        real_open = open
        real_lstat = os.lstat
        swapped = {"done": False}

        class Guard:
            def __init__(self, raw):
                self.raw = raw

            def read(self, *args, **kwargs):
                reads["n"] += 1
                return self.raw.read(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.raw.close()

            def fileno(self):
                return self.raw.fileno()

            def close(self):
                self.raw.close()

        def watch(target, *args, **kwargs):
            handle = real_open(target, *args, **kwargs)
            # Production opens the lexical workspace policy path; after the swap that
            # resolves to the external file. Count any read through that lexical path.
            if _same_path(target, self.ws.policy_path):
                return Guard(handle)
            if _same_path(target, secret):
                return Guard(handle)
            return handle

        def lstat(path, *args, **kwargs):
            if _same_path(path, self.ws.pbz_dir) and not swapped["done"]:
                info = real_lstat(path, *args, **kwargs)
                swapped["done"] = True
                shutil.rmtree(self.ws.pbz_dir)
                make_junction(self, self.ws.pbz_dir, external)
                return info
            return real_lstat(path, *args, **kwargs)

        _reset()
        with patch("os.lstat", side_effect=lstat), patch("builtins.open", side_effect=watch):
            data = guard.read_policy_file(self.ws.root)
        self.assertTrue(data.get("_pbz_lockdown"))
        self.assertNotEqual(data.get("synthetic"), True)
        self.assertEqual(reads["n"], 0)
        self.assertEqual(open(secret, "rb").read(), b'{"version": 1, "synthetic": true}')
        _reset()
        mirror = os.path.join(self.ws.mirror, "X.SemanticModel", "definition", "model.tmdl")
        self.assertEqual(guard.classify_path(mirror), "error")

    def test_substituted_policy_handle_is_not_read(self):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        path = self.ws.policy_path
        with open(path, "wb") as handle:
            handle.write(b'{"version": 1}')
        secret = os.path.join(self.outside.name, "other-policy.json")
        with open(secret, "wb") as handle:
            handle.write(b"CANARY-POLICY")
        reads = {"n": 0}
        real_open = open
        state = os.path.join(self.ws.private, "state.json")
        os.makedirs(self.ws.private, exist_ok=True)
        with open(state, "w", encoding="utf-8") as handle:
            handle.write('{"budget": 9}')
        before = open(state, "rb").read()

        class Guard:
            def __init__(self, raw):
                self.raw = raw

            def read(self, *args, **kwargs):
                reads["n"] += 1
                return self.raw.read(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.raw.close()

            def fileno(self):
                return self.raw.fileno()

            def close(self):
                self.raw.close()

        def swapped(target, *args, **kwargs):
            if not _same_path(target, path):
                return real_open(target, *args, **kwargs)
            return Guard(real_open(secret, *args, **kwargs))

        with patch("builtins.open", swapped):
            with self.assertRaises(PbzError) as caught:
                policy.load(self.ws)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            args = type("A", (), {"action": "budget-reset", "value": None})()
            with patch("pbz.cli._is_human", return_value=True):
                with self.assertRaises(PbzError) as admin_err:
                    admin(self.ws, args)
            self.assertIn(admin_err.exception.code, ("UNSAFE_PATH", "REFUSED"))
        self.assertEqual(reads["n"], 0)
        self.assertEqual(open(secret, "rb").read(), b"CANARY-POLICY")
        self.assertEqual(open(state, "rb").read(), before)
        _reset()
        mirror = os.path.join(self.ws.mirror, "X.SemanticModel", "definition", "model.tmdl")
        with patch("builtins.open", swapped):
            self.assertEqual(guard.classify_path(mirror), "error")
        self.assertEqual(reads["n"], 0)

    def test_dangling_policy_link(self):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        try:
            os.symlink(os.path.join(self.outside.name, "missing.json"), self.ws.policy_path)
        except OSError:
            self.skipTest("symlink is unavailable")
        with self.assertRaises(PbzError) as caught:
            policy.load(self.ws)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        _reset()
        self.assertTrue(guard.read_policy_file(self.ws.root).get("_pbz_lockdown"))

    def test_admin_refuses_before_budget_or_consumer_change(self):
        os.makedirs(self.ws.private, exist_ok=True)
        state = os.path.join(self.ws.private, "state.json")
        with open(state, "w", encoding="utf-8") as handle:
            handle.write('{"budget": 1}')
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write("{")
        args = type("A", (), {"action": "budget-reset", "value": None})()
        with patch("pbz.cli._is_human", return_value=True):
            with self.assertRaises(PbzError) as caught:
                admin(self.ws, args)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(open(state, encoding="utf-8").read(), '{"budget": 1}')
        with patch("pbz.cli._is_human", return_value=False):
            with self.assertRaises(PbzError) as human:
                admin(self.ws, args)
        self.assertEqual(human.exception.code, "HUMAN_ONLY")
        os.remove(self.ws.policy_path)
        with patch("pbz.cli._is_human", return_value=True):
            self.assertEqual(admin(self.ws, args)["budget"], "reset")
        self.assertFalse(os.path.exists(state))

    def test_pull_refuses_malformed_policy(self):
        from pbz import mirror
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write("{")
        project = self.ws.project()
        with self.assertRaises(PbzError) as caught:
            mirror.pull(self.ws, project)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, MALFORMED)

    def test_lockdown_under_synthetic_parent_denies_mirror(self):
        parent = os.path.join(self.outside.name, "parent")
        os.makedirs(os.path.join(parent, ".pbz"), exist_ok=True)
        with open(os.path.join(parent, ".pbz", "policy.json"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"synthetic": True}))
        child = os.path.join(parent, "child")
        ws = self._workspace(child, body="{")
        _reset()
        mirror = os.path.join(child, "pbz-workspace", "X.SemanticModel", "definition", "model.tmdl")
        self.assertFalse(guard.synthetic(child))
        self.assertEqual(guard.classify_path(mirror), "error")
        self.assertIs(ws.policy_path, ws.policy_path)

    def test_synthetic_child_without_policy_stays_exempt(self):
        parent = os.path.join(self.outside.name, "syn-parent")
        os.makedirs(os.path.join(parent, ".pbz"), exist_ok=True)
        with open(os.path.join(parent, ".pbz", "policy.json"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1, "synthetic": True}))
        child = os.path.join(parent, "fixture")
        self._workspace(child)
        _reset()
        self.assertTrue(guard.synthetic(child))
        self.assertIsNone(self._shell("python -c \"import adomd\"", child))

    def test_shell_policy_rules(self):
        cwd = self.ws.root
        parent = os.path.dirname(self.ws.root)
        os.makedirs(os.path.join(cwd, ".pbz"), exist_ok=True)
        os.makedirs(os.path.join(cwd, "sub"), exist_ok=True)
        os.makedirs(os.path.join(cwd, "data"), exist_ok=True)
        os.makedirs(os.path.join(cwd, "tmp"), exist_ok=True)
        report = os.path.join(cwd, "pbz-workspace", "X.Report", "definition", "pages", "p1", "visuals", "v1")
        os.makedirs(report, exist_ok=True)
        denied = [
            "rm .pbz/policy.json",
            "Remove-Item -Recurse .pbz",
            "echo {} > .pbz\\policy.json",
            "Set-Content ..\\.pbz\\policy.json x",
            "pwsh -Command \"Set-Content .pbz/policy.json x\"",
            "python -c \"open('.pbz/policy.json','w')\"",
            "cd .pbz && Set-Content policy.json '{\"version\":1,\"synthetic\":true}'",
            "Set-Content .pbz/./policy.json x",
            "Set-Content .pbz\\polic?.json x",
            "echo x > .pbz/pol*.json",
            f"Set-Content {cwd}\\.pbz\\policy.json x",
            "Copy-Item policy.json ..\\.p*",
            "mv policy.json ../.p*",
            "Remove-Item -Recurse ..\\.p*",
            "Rename-Item ..\\.pbz\\x.json policy.json",
            "Rename-Item .pbz\\x.json policy.json",
            "cmd /c copy policy.json ..\\.pbz",
            "cmd /c ren .pbz\\x.json policy.json",
            "xcopy policy.json ..\\.pbz\\",
            "robocopy . ..\\.pbz policy.json",
            "pwsh -Command \"Copy-Item policy.json ..\\.pbz\"",
            "Remove-Item -LiteralPath:.pbz\\policy.json",
            "Move-Item x.json -Destination:.pbz\\policy.json",
            "mv .pbz/policy.json x",
            "Rename-Item .pbz .pbz-old",
            "Copy-Item loose.json .pbz/policy.json",
            "mv loose.json .pbz/policy.json",
            "Move-Item x.json .pbz\\policy.json",
            "rm .pbz/*",
            "Remove-Item .pbz\\pol*",
            "mv tmp/pol data/.pbz",
            "Copy-Item tmp\\policy.json data\\.pbz\\",
            "Move-Item x data\\.pbz",
            "rm *",
            "rm .p*",
            "rm */policy.json",
            "Remove-Item .pb?\\policy.json",
            "rm .p*/pol*",
            "New-Item -ItemType SymbolicLink -Path .pbz\\policy.json -Target x -Force",
            "ni -Type HardLink",
            "New-Item -it SymbolicLink",
            "New-Item -ItemType:Junction",
            "New-Item -ItemType HardLink -Path pbz-workspace\\a.tmdl -Target:..\\other\\x.txt",
            'New-Item -ItemType:"HardLink" -Path pbz-workspace\\a.tmdl -Target X.SemanticModel\\definition\\model.tmdl',
            "New-Item -Type:'HardLink'",
            "cd . > .pbz\\policy.json",
        ]
        for cmd in denied:
            self._deny(cmd, cwd)
        self._deny("cd . >policy.json", os.path.join(cwd, ".pbz"))

        self._allow("pbz status", cwd)
        self._allow("ls *", cwd)
        self._allow("dir *", cwd)
        self._allow("Get-Content .pbz\\evidence.jsonl", cwd)
        self._allow("cd .pbz && Get-Content evidence.jsonl", cwd)
        self._allow("rm sub/*.json", cwd)
        self._allow("rm *.log", cwd)
        self._allow(f"Remove-Item pbz-workspace\\X.Report\\definition\\pages\\p1\\visuals\\v1\\*", cwd)
        self._allow("rm sub/*", cwd)
        os.makedirs(os.path.join(cwd, "sub", ".pbz"), exist_ok=True)
        self._deny("rm sub/*", cwd)
        self._deny("Set-Content policy.json x", os.path.join(cwd, ".pbz"))
        self._deny(f"Set-Content {cwd}\\.pbz\\policy.json x", parent)
        model = os.path.join(cwd, "X.SemanticModel", "definition", "model.tmdl")
        self._deny(f"Get-Content -LiteralPath:{model}", cwd, "definition")
        self._deny("Get-Content -Path:..\\X.SemanticModel\\definition\\model.tmdl", cwd, "definition")
        self._allow("Get-Content -Path:pbz-workspace\\X.SemanticModel\\definition\\model.tmdl", cwd)
        # Explicit allow on a real definition must survive shell + Read; lockdown still denies.
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1, "allow": ["x.semanticmodel/definition/model.tmdl"]}))
        _reset()
        self.assertIsNone(guard.classify_path(guard.norm(model)))
        self._allow(f"Get-Content -LiteralPath:{model}", cwd)
        self._allow("Get-Content X.SemanticModel\\definition\\model.tmdl", cwd)
        _reset()
        event = {"tool_name": "Read", "cwd": cwd, "tool_input": {"file_path": model}}
        self.assertIsNone(guard.decide(event))
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write("{")
        _reset()
        self.assertEqual(guard.classify_path(guard.norm(model)), "error")
        self._deny(f"Get-Content -LiteralPath:{model}", cwd, "error")
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1, "allow": []}))
        _reset()
        # Absolute wildcard forms, including when the shell cwd is outside the workspace.
        self._deny(f"Remove-Item -Recurse {cwd}\\.p*", parent)
        self._deny(f"Remove-Item -Recurse {cwd}\\.p*", cwd)
        self._deny(f"Copy-Item x.json {cwd}\\.p*", parent)
        self._deny(f"Move-Item x.json -Destination {cwd}\\.pb?", parent)
        # Named-parameter renames into policy.json (no existing policy file required).
        for cmd in (
            "Rename-Item -Path .pbz\\x.json -NewName policy.json",
            "Rename-Item -LiteralPath .pbz\\x.json -NewName policy.json",
            "Rename-Item -NewName:policy.json -Path:.pbz\\x.json",
            "Rename-Item -NewName:policy.json -LiteralPath:.pbz\\x.json",
            'Rename-Item -Path:.pbz\\x.json -NewName:"policy.json"',
            'Rename-Item -NewName:"policy.json" -Path:.pbz\\x.json',
            'Rename-Item -LiteralPath:.pbz\\x.json -NewName:"policy.json"',
            "pwsh -Command \"Rename-Item -Path .pbz\\x.json -NewName policy.json\"",
            "pwsh -Command \"Rename-Item -NewName:policy.json -Path:.pbz\\x.json\"",
        ):
            self._deny(cmd, cwd)
        self._deny(f"Rename-Item -Path {cwd}\\.pbz\\x.json -NewName policy.json", parent)
        self._deny(f"Rename-Item -LiteralPath {cwd}\\.pbz\\x.json -NewName policy.json", parent)
        self._deny(f"Rename-Item -NewName:policy.json -Path:{cwd}\\.pbz\\x.json", parent)
        self._deny(f'Rename-Item -Path:{cwd}\\.pbz\\x.json -NewName:"policy.json"', parent)
        self._deny(f'Rename-Item -NewName:"policy.json" -Path:{cwd}\\.pbz\\x.json', parent)
        self.assertFalse(guard.synthetic(cwd))
        self._allow("Rename-Item -Path sub\\a.json -NewName b.json", cwd)
        self._allow("Rename-Item -NewName:b.json -Path:sub\\a.json", cwd)
        self._allow("Rename-Item notes.txt notes.bak", cwd)

    def test_nested_array_policy_refuses_and_locks_down(self):
        nested = ("[" * 10000 + "0" + "]" * 10000).encode()
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "wb") as handle:
            handle.write(nested)
        with self.assertRaises(PbzError) as caught:
            policy.load(self.ws)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, MALFORMED)
        self.assertNotIn("[[[[", caught.exception.detail)
        _reset()
        mirror = os.path.join(self.ws.mirror, "X.SemanticModel", "definition", "model.tmdl")
        self.assertEqual(guard.classify_path(mirror), "error")

    def test_admin_consumer_remove_refuses_malformed_policy(self):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        ledger = os.path.join(self.ws.pbz_dir, "consumers.json")
        with open(ledger, "w", encoding="utf-8") as handle:
            handle.write('{"consumers":[{"id":"c1"}]}')
        before = open(ledger, "rb").read()
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write("{")
        args = type("A", (), {"action": "consumer-remove", "value": "c1"})()
        with patch("pbz.cli._is_human", return_value=True):
            with self.assertRaises(PbzError) as caught:
                admin(self.ws, args)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(open(ledger, "rb").read(), before)

    def test_parent_session_cannot_plant_policy(self):
        parent = os.path.dirname(self.ws.root)
        _reset()
        event = {"tool_name": "Write", "cwd": parent, "tool_input": {
            "file_path": os.path.join(parent, ".pbz", "policy.json"),
            "content": json.dumps({"version": 1, "synthetic": True})}}
        self.assertEqual(guard.decide(event), "policy")
        self.assertFalse(guard.synthetic(self.ws.root))
        for cmd in ("Copy-Item C:\\tmp\\policy.json .pbz", "Rename-Item .pbz\\x.json policy.json",
                    "cmd /c copy policy.json .pbz", "mv policy.json .pbz/"):
            self._deny(cmd, parent)
        self.assertFalse(guard.synthetic(self.ws.root))

    def test_write_invalid_policy_is_denied(self):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1, "disclosure": "strict"}))
        _reset()
        for body in ('{"version": 1, "disclosure": "open"}', '{"version": 1, "budgets": {"session_checks": -1}}'):
            event = {"tool_name": "Write", "cwd": self.ws.root, "tool_input": {
                "file_path": self.ws.policy_path, "content": body}}
            self.assertEqual(guard.decide(event), "policy")
        _reset()
        self.assertEqual(guard.check_write(guard.norm(self.ws.policy_path), "Write",
                                           {"content": "{"}, guard.norm(self.ws.root)), "policy")

    def test_runtime_error_in_reader_denies(self):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 1}))
        real = open

        def boom(path, *args, **kwargs):
            if str(path).endswith("policy.json"):
                raise RuntimeError("injected")
            return real(path, *args, **kwargs)

        event = {"hook_event_name": "PreToolUse", "tool_name": "Read", "cwd": self.ws.root,
                 "tool_input": {"file_path": os.path.join(self.ws.mirror, "X.SemanticModel", "definition", "model.tmdl")}}
        buf = __import__("io").StringIO()
        with patch("builtins.open", side_effect=boom), patch("sys.stdin", __import__("io").StringIO(json.dumps(event))), \
             patch("sys.stdout", buf):
            _reset()
            self.assertEqual(guard.main(), 0)
        text = buf.getvalue()
        self.assertIn("deny", text)
        self.assertNotIn("Traceback", text)

    def test_isolated_guard_process_denies_without_traceback(self):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(self.ws.policy_path, "w", encoding="utf-8") as handle:
            handle.write("{")
        event = {"hook_event_name": "PreToolUse", "tool_name": "Read", "cwd": self.ws.root,
                 "tool_input": {"file_path": os.path.join(self.ws.mirror, "X.SemanticModel", "definition", "model.tmdl")}}
        proc = subprocess.run([sys.executable, "-I", "-S", os.path.join(SKILL, "pbz", "guard.py"), "--agent", "test"],
                              input=json.dumps(event), text=True, capture_output=True, cwd=self.ws.root)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("deny", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("Traceback", proc.stdout)

    def test_validator_parity(self):
        samples = [{}, {"synthetic": True}, {"version": True}, {"disclosure": "open"}, {"allow": "*"},
                   {"literals": "strict"}, {"literals": "shape"}, {"category_max": 0}, {"category_max": True}]
        for sample in samples:
            self.assertEqual(policy.security_values_ok(sample), guard.security_values_ok(sample))


if __name__ == "__main__":
    unittest.main()
