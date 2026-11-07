"""Preview plans every page, then commits once. Specs are an object or a mirror path.

Run: python tests/test_preview_atomic.py
"""
from __future__ import annotations

import hashlib
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

from pbz import cli, common, mcp, preview  # noqa: E402
from pbz.common import PbzError  # noqa: E402

PAGE = {"name": "p", "displayName": "Page", "width": 1280, "height": 720}


def _files(root: str) -> dict[str, bytes]:
    out = {}
    if not os.path.isdir(root):
        return out
    for cur, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(cur, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root).replace("\\", "/")] = f.read()
    return out


class PreviewAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.ws = common.Workspace(root)
        with open(os.path.join(root, "Mini.pbip"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.project = self.ws.project()
        for pid in ("p1", "p2"):
            folder = os.path.join(self.project.mirror_report, "definition", "pages", pid)
            os.makedirs(folder)
            with open(os.path.join(folder, "page.json"), "w", encoding="utf-8") as f:
                json.dump(dict(PAGE, name=pid, displayName=pid), f)

    def test_second_page_failure_restores_first(self):
        first = hashlib.sha256(b"p1").hexdigest()[:20]
        second = hashlib.sha256(b"p2").hexdigest()[:20]
        out = os.path.join(self.ws.mirror, "previews")
        os.makedirs(out)
        seeded = os.path.join(out, first + ".svg")
        with open(seeded, "wb") as f:
            f.write(b"OLD")
        before = _files(self.ws.mirror)
        real = common.write_text

        def boom(path, text, bom=b""):
            if second in path.replace("\\", "/"):
                raise OSError("second")
            return real(path, text, bom)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                preview.run(self.ws, self.project)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_files(self.ws.mirror), before)
        self.assertEqual(open(seeded, "rb").read(), b"OLD")
        self.assertEqual(list(Path(self.ws.mirror).rglob("*.pbz-tmp")), [])

    def test_inline_mcp_and_mirror_path_round_trip(self):
        spec = {"version": 1}
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--workspace", self.ws.root, "--project", "Mini", "preview", "--spec", json.dumps(spec)])
        self.assertEqual(rc, 0)
        self.assertIn("GENERATED", buf.getvalue())
        rel = os.path.join(self.ws.mirror, "spec.json")
        with open(rel, "w", encoding="utf-8") as f:
            json.dump(spec, f)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--workspace", self.ws.root, "--project", "Mini", "preview", "--spec", "spec.json"])
        self.assertEqual(rc, 0)
        mcp_out = mcp.call_tool("preview", {"spec": spec, "workspace": self.ws.root, "project": "Mini"}, None, None)
        self.assertFalse(mcp_out["isError"])
        self.assertIn("GENERATED", mcp_out["content"][0]["text"])

    def test_outside_mirror_path_is_refused(self):
        outside = os.path.join(self.tmp.name, "outside.json")
        nested = os.path.join(self.tmp.name, "nest")
        os.makedirs(nested)
        with open(os.path.join(nested, "Mini.pbip"), "w", encoding="utf-8") as f:
            f.write("{}")
        ws = common.Workspace(nested)
        folder = os.path.join(ws.project().mirror_report, "definition", "pages", "p1")
        os.makedirs(folder)
        with open(os.path.join(folder, "page.json"), "w", encoding="utf-8") as f:
            json.dump(dict(PAGE, name="p1"), f)
        secret = "SECRET_SPEC_BODY"
        with open(outside, "w", encoding="utf-8") as f:
            f.write(secret)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--workspace", nested, "--project", "Mini", "preview", "--spec", outside])
        text = buf.getvalue()
        self.assertNotEqual(rc, 0)
        self.assertIn("REFUSED", text)
        self.assertNotIn(secret, text)
        self.assertFalse(os.path.isdir(os.path.join(ws.mirror, "previews")))


if __name__ == "__main__":
    unittest.main()
