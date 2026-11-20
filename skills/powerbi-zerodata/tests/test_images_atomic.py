"""Image add/remove is one commit, including orphan pending files, and recovery stays closed.

Run: python tests/test_images_atomic.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import cli, common, images, mcp  # noqa: E402
from pbz.common import PbzError  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
SECRET = "SECRET_EXC_TEXT_9f3a"


def _bytes(root: str) -> dict[str, bytes]:
    out = {}
    if not os.path.isdir(root):
        return out
    for cur, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(cur, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = f.read()
    return out


def _write(path: str, data: str | bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f:
            f.write(data)
        return
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)


class ImageAtomicTests(unittest.TestCase):
    def _project(self, *, report="{}", visual=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        ws.private = os.path.join(root, "private")
        os.makedirs(ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        os.makedirs(project.report_dir, exist_ok=True)
        _write(os.path.join(project.mirror_report, "definition", "report.json"), report)
        if visual is not None:
            _write(os.path.join(project.mirror_report, "definition", "pages", "p1", "visuals", "v1", "visual.json"), visual)
        png = os.path.join(root, "dot.png")
        _write(png, PNG)
        return ws, project, png

    def _maps(self, ws, project):
        return _bytes(ws.mirror), _bytes(images._pending_dir(ws, project))

    def test_malformed_report_and_visual_refuse_before_staging(self):
        ws, project, png = self._project(report="{")
        orphan = os.path.join(images._pending_dir(ws, project), "orphan.png")
        _write(orphan, b"ORPHAN-BYTES")
        before = self._maps(ws, project)
        with self.assertRaises(PbzError) as caught:
            images.add(ws, project, png)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("report.json", caught.exception.detail)
        self.assertNotIn("Expecting", caught.exception.detail)
        self.assertNotIn(PNG.decode("latin1"), caught.exception.detail)
        self.assertEqual(self._maps(ws, project), before)

        ws, project, png = self._project(report='{"resourcePackages":[]}\n', visual="{")
        before = self._maps(ws, project)
        images.add(ws, project, png)
        item = images.registered(common.read_text(images._report_json(project.mirror_report))[0])
        self.assertEqual(len(item), 1)
        name = next(iter(item))
        with self.assertRaises(PbzError) as caught:
            images.remove(ws, project, name)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("visual.json", caught.exception.detail.replace("\\", "/"))
        self.assertNotIn("Expecting", caught.exception.detail)
        self.assertTrue(os.path.isfile(os.path.join(images._pending_dir(ws, project), name)))

    def test_mid_commit_restores_mirror_and_pending_including_orphan(self):
        ws, project, png = self._project()
        pend = images._pending_dir(ws, project)
        orphan = os.path.join(pend, "left-behind.png")
        _write(orphan, b"ORPHAN-BYTES")
        before = self._maps(ws, project)
        real = common.write_text

        def boom(path, text, bom=b"", *, root=None, **_kw):
            raise OSError("disk")

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                images.add(ws, project, png)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(self._maps(ws, project), before)
        self.assertEqual(open(orphan, "rb").read(), b"ORPHAN-BYTES")
        self.assertEqual(list(Path(ws.root).rglob("*.pbz-tmp")), [])
        self.assertIs(real, common.write_text)

    def test_recovery_output_has_no_paths_bytes_or_exception_text(self):
        for via_mcp in (False, True):
            with self.subTest(via_mcp=via_mcp):
                ws, project, png = self._project()
                pending = images._pending_dir(ws, project)
                report = images._report_json(project.mirror_report)
                calls = []
                real_remove = os.remove

                def boom(path, text, bom=b"", *, root=None, **_kw):
                    calls.append(("write_text", path))
                    raise OSError(SECRET)

                def bad_remove(path, *args, **kwargs):
                    if os.path.commonpath([os.path.abspath(path), pending]) == pending:
                        calls.append(("pending_remove", os.fspath(path)))
                        raise OSError(SECRET)
                    return real_remove(path, *args, **kwargs)

                def fake_bounded(command, cwd=None, env=None, timeout=None, mid=0):
                    fake_bounded.calls.append(command)
                    out = io.StringIO()
                    with redirect_stdout(out):
                        code = cli.main(command[3:])
                    return code, out.getvalue().encode("utf-8"), None
                fake_bounded.calls = []

                argv = ["--workspace", ws.root, "--project", "Mini", "image", "add", png]
                with patch("pbz.cli.find_workspace", return_value=ws), \
                        patch("pbz.common.write_text", boom), patch("os.remove", bad_remove):
                    if via_mcp:
                        with patch("pbz.mcp._bounded_output", fake_bounded):
                            mcp_out = mcp.call_tool("image", {"action": "add", "target": png,
                                                    "workspace": ws.root, "project": "Mini"}, ws.root, None)
                        self.assertTrue(fake_bounded.calls)
                        self.assertTrue(mcp_out["isError"])
                        text = json.dumps(mcp_out)
                    else:
                        buf = io.StringIO()
                        with redirect_stdout(buf):
                            rc = cli.main(argv)
                        self.assertNotEqual(rc, 0)
                        text = buf.getvalue()
                self.assertIn(("write_text", report), calls)
                self.assertTrue(any(kind == "pending_remove" for kind, _path in calls))
                self.assertIn("RECOVERY_REQUIRED", text)
                for banned in (SECRET, ws.private, ws.root, png, "ORPHAN", PNG.decode("latin1"), "OSError", ".pbz-restore"):
                    self.assertNotIn(banned, text)
                self.assertEqual(list(Path(ws.root).rglob("*.pbz-tmp")), [])


if __name__ == "__main__":
    unittest.main()
