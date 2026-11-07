"""Pull refuses a late decode before any write. Run: python tests/test_pull_refusal.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, mirror  # noqa: E402


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


def no_tmp(root):
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.endswith(".pbz-tmp"):
                return False
    return True


class PullRefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)

    def test_bad_utf8_later_file_changes_nothing(self):
        files = mirror.project_files(self.project)
        rel, real = files[-1]
        with open(real, "wb") as f:
            f.write(b"\xff\xfe not utf-8")
        before_m = file_map(self.ws.mirror)
        before_man = file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))
        with self.assertRaises(common.PbzError) as caught:
            mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn(rel, caught.exception.detail)
        self.assertNotIn("utf", caught.exception.detail.lower())
        self.assertEqual(file_map(self.ws.mirror), before_m)
        self.assertEqual(file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))), before_man)
        self.assertTrue(no_tmp(self.ws.root))
        self.assertTrue(no_tmp(self.ws.private))

    def test_late_rebase_base_decode_is_closed(self):
        files = mirror.project_files(self.project)
        rel, real = files[-1]
        mirror_path = os.path.join(self.ws.mirror, *rel.split("/"))
        base = os.path.join(mirror.base_dir(self.ws, self.project), *rel.split("/"))
        with open(mirror_path, "ab") as f:
            f.write(b"\n")
        text, _bom = common.read_text(real)
        with open(real, "wb") as f:
            f.write(text.encode("utf-8") + b"\n")
        with open(base, "wb") as f:
            f.write(b"\xff rebase-secret")
        before = (file_map(self.ws.mirror), file_map(mirror.base_dir(self.ws, self.project)),
                  file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))))
        with self.assertRaises(common.PbzError) as caught:
            mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, rel)
        self.assertNotIn("rebase-secret", str(caught.exception))
        self.assertEqual((file_map(self.ws.mirror), file_map(mirror.base_dir(self.ws, self.project)),
                          file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))), before)
        self.assertTrue(no_tmp(self.ws.root))
        self.assertTrue(no_tmp(self.ws.private))


if __name__ == "__main__":
    unittest.main()
