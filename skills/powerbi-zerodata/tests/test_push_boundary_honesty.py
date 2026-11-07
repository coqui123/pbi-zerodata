"""Push captures candidates before validation and keeps PARTIAL after a reload failure.
Run: python tests/test_push_boundary_honesty.py
"""
from __future__ import annotations

import io
import json
import os
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


if __name__ == "__main__":
    unittest.main()
