"""Push captures candidates before validation and keeps PARTIAL after a reload failure.
Run: python tests/test_push_boundary_honesty.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import cli, common, mcp, mirror, outcomes  # noqa: E402


def file_map(root):
    out = {}
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            with open(path, "rb") as f:
                out[rel] = f.read()
    return out


class PushBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        os.environ["PBZ_PRIVATE_ROOT"] = os.path.join(self.tmp.name, "private")
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        self.ws = common.Workspace(root)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)

    def _touch_mirror(self, suffix):
        rel = next(rel for rel, _real in mirror.project_files(self.project) if suffix in rel)
        path = os.path.join(self.ws.mirror, *rel.split("/"))
        with open(path, "ab") as f:
            f.write(b"\n")
        return rel, path

    def test_change_during_validation_is_conflict(self):
        rel, _path = self._touch_mirror(".SemanticModel/")
        real = mirror._real_path(self.ws, rel)
        manifest = file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))

        def validate(ws, project, stage, tm):
            with open(real, "ab") as f:
                f.write(b"\n")
            with open(os.path.join(self.ws.mirror, *rel.split("/")), "ab") as f:
                f.write(b"x")
            return []

        with patch("pbz.mirror.validate.run_stage", validate), patch("pbz.mirror.desktop.find_instance", lambda p: None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "CONFLICT")
        self.assertEqual(file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))), manifest)

    def test_addition_appearing_during_validation_is_not_adopted(self):
        rel = f"{self.project.name}.SemanticModel/definition/tables/Added.tmdl"
        path = os.path.join(self.ws.mirror, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("table Added\n")
        real = mirror._real_path(self.ws, rel)

        def validate(ws, project, stage, tm):
            os.makedirs(os.path.dirname(real), exist_ok=True)
            with open(real, "w", encoding="utf-8", newline="\n") as f:
                f.write("CANARY-ADDED")
            return []

        with patch("pbz.mirror.validate.run_stage", validate), patch("pbz.mirror.desktop.find_instance", lambda p: None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "CONFLICT")
        self.assertEqual(open(real, encoding="utf-8").read(), "CANARY-ADDED")

    def test_excluded_directory_addition_is_refused(self):
        folder = os.path.join(self.ws.mirror, self.project.name + ".SemanticModel", ".pbi")
        os.makedirs(folder)
        with open(os.path.join(folder, "localSettings.json"), "w", encoding="utf-8") as f:
            f.write('{"secret":"CANARY-PBI"}')
        real = os.path.join(self.project.model_dir, ".pbi", "localSettings.json")
        with patch("pbz.mirror.desktop.find_instance", lambda p: None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "REFUSED")
        self.assertFalse(os.path.exists(real))
        self.assertNotIn("CANARY-PBI", json.dumps(out))

    def test_reload_failure_is_partial_on_cli_and_mcp(self):
        def boom(project):
            raise RuntimeError("reload secret")

        patches = (
            patch("pbz.mirror.validate.run_stage", lambda *a, **k: []),
            patch("pbz.mirror.desktop.find_instance", lambda p: {"unsaved": False}),
            patch("pbz.mirror.desktop.reload", boom),
        )
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in patches])

        self._touch_mirror(".Report/")
        out = mirror.push(self.ws, self.project, reload=True)
        env, rc = outcomes.envelope(out)
        self.assertEqual(out["result"], "PUSHED")
        self.assertNotEqual(out.get("reload"), "RELOADED")
        self.assertEqual(rc, 5)
        self.assertEqual(env["outcome"]["status"], "PARTIAL")

        self._touch_mirror(".Report/")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_rc = cli.main(["--workspace", self.ws.root, "--project", "Parity", "push", "--reload"])
        body = json.loads(buf.getvalue())
        self.assertEqual(cli_rc, 5)
        self.assertEqual(body["outcome"]["exit_code"], 5)
        self.assertEqual(body["result"], "PUSHED")

        self._touch_mirror(".Report/")

        def fake_run(argv, **kwargs):
            out_buf = io.StringIO()
            with redirect_stdout(out_buf):
                code = cli.main(argv[3:])

            class Proc:
                returncode = code
                stdout = out_buf.getvalue().encode("utf-8")
                stderr = b""
            return Proc()

        with patch("pbz.mcp.subprocess.run", fake_run):
            mcp_out = mcp.call_tool("push", {"reload": True, "workspace": self.ws.root, "project": "Parity"}, None, None)
        mcp_body = json.loads(mcp_out["content"][0]["text"])
        self.assertTrue(mcp_out["isError"])
        self.assertEqual(mcp_body["outcome"]["exit_code"], 5)
        self.assertEqual(mcp_body["result"], "PUSHED")
        self.assertNotIn("reload secret", buf.getvalue() + mcp_out["content"][0]["text"])

    def test_definition_junction_is_not_walked(self):
        outside = os.path.join(self.tmp.name, "outside-def")
        os.makedirs(outside)
        with open(os.path.join(outside, "CANARY-NAME.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        link = os.path.join(self.ws.mirror, self.project.name + ".Report", "definition", "outside")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        try:
            made = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside], capture_output=True)
        except OSError:
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            self.skipTest("creating a junction is unavailable")
        with patch("pbz.mirror.desktop.find_instance", lambda p: None):
            out = mirror.preflight(self.ws, self.project)
        rel = self.project.name + ".Report/definition/outside/"
        self.assertEqual(out["files"], [rel])
        self.assertNotIn("CANARY-NAME", json.dumps(out))

    def test_cross_project_alias_is_refused(self):
        other = os.path.join(self.ws.mirror, "Other.Report", "definition")
        os.makedirs(other)
        canary = os.path.join(other, "CANARY-OTHER.json")
        with open(canary, "w", encoding="utf-8") as f:
            f.write('{"secret": true}')
        target = os.path.join(
            self.ws.mirror, self.project.name + ".Report", "definition", "pages", "linked.json")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            os.symlink(canary, target)
        except OSError:
            self.skipTest("file symlink is unavailable")
        man = mirror.load_manifest(self.ws, self.project)
        rel = f"{self.project.name}.Report/definition/pages/linked.json"
        man.setdefault("files", {})[rel] = {"real": None, "mirror": "0" * 64}
        mirror.save_manifest(self.ws, self.project, man)
        with patch("pbz.mirror.desktop.find_instance", lambda p: None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "REFUSED")
        self.assertIn(rel, out.get("files", []))
        self.assertNotIn("CANARY-OTHER", json.dumps(out))
        texts = mirror.mirror_texts(self.ws, self.project)
        self.assertNotIn(rel, texts)

    def test_cross_project_directory_junction_is_not_walked(self):
        other = os.path.join(self.ws.mirror, "Other.Report", "definition", "secret")
        os.makedirs(other)
        with open(os.path.join(other, "CANARY-NAME.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        link = os.path.join(self.ws.mirror, self.project.name + ".Report", "definition", "outside")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        try:
            made = subprocess.run(["cmd", "/c", "mklink", "/J", link, other], capture_output=True)
        except OSError:
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            self.skipTest("creating a junction is unavailable")
        with patch("pbz.mirror.desktop.find_instance", lambda p: None):
            pre = mirror.preflight(self.ws, self.project)
            dry = mirror.push(self.ws, self.project, dry_run=True)
            out = mirror.push(self.ws, self.project)
        rel = self.project.name + ".Report/definition/outside/"
        for result in (pre, dry, out):
            self.assertEqual(result.get("files"), [rel])
            self.assertNotIn("CANARY-NAME", json.dumps(result))

    def test_project_report_root_alias_is_refused(self):
        other = os.path.join(self.ws.mirror, "Other.Report")
        os.makedirs(os.path.join(other, "definition"))
        with open(os.path.join(other, "definition", "CANARY-NAME.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        report = os.path.join(self.ws.mirror, self.project.name + ".Report")
        held = report + ".held"
        os.rename(report, held)
        try:
            made = subprocess.run(["cmd", "/c", "mklink", "/J", report, other], capture_output=True)
        except OSError:
            os.rename(held, report)
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            os.rename(held, report)
            self.skipTest("creating a junction is unavailable")
        try:
            with patch("pbz.mirror.desktop.find_instance", lambda p: None):
                pre = mirror.preflight(self.ws, self.project)
                dry = mirror.push(self.ws, self.project, dry_run=True)
                out = mirror.push(self.ws, self.project)
            rel = self.project.name + ".Report/"
            for result in (pre, dry, out):
                self.assertEqual(result.get("result"), "REFUSED")
                self.assertEqual(result.get("files"), [rel])
                self.assertNotIn("CANARY-NAME", json.dumps(result))
            self.assertNotIn(
                f"{self.project.name}.Report/definition/CANARY-NAME.json",
                mirror.mirror_texts(self.ws, self.project))
        finally:
            os.rmdir(report)
            os.rename(held, report)


if __name__ == "__main__":
    unittest.main()
