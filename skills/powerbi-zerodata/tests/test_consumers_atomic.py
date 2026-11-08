"""Consumer registry and cache land in one private commit.

Run: python tests/test_consumers_atomic.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, consumers, mirror  # noqa: E402
from pbz.common import PbzError  # noqa: E402


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


class ConsumersAtomicTests(unittest.TestCase):
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

    def _reg(self) -> bytes | None:
        path = consumers._reg_path(self.ws)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def test_cache_write_failure_leaves_registry(self):
        consumers.add(self.ws, self.project, objects=["[Total Sales]"], label="one")
        before = self._reg()
        real = common.write_text

        def boom(path, text, bom=b"", *, root=None, **_kw):
            if path.endswith("c2.json"):
                raise OSError("cache")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                consumers.add(self.ws, self.project, objects=["Sales"], label="two")
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(self._reg(), before)
        self.assertFalse(os.path.exists(consumers._cache_path(self.ws, "c2")))
        self.assertEqual(list(Path(self.ws.root).rglob("*.pbz-tmp")), [])

    def test_cache_delete_failure_restores_registry(self):
        consumers.add(self.ws, self.project, objects=["[Total Sales]"], label="one")
        before = self._reg()
        real = os.remove

        def boom(path):
            if str(path).endswith("c1.json"):
                raise OSError("delete")
            return real(path)

        with patch("os.remove", boom):
            with self.assertRaises(PbzError) as caught:
                consumers.remove(self.ws, "c1")
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(self._reg(), before)
        self.assertTrue(os.path.isfile(consumers._cache_path(self.ws, "c1")))

    def test_discover_register_is_one_commit(self):
        folder = os.path.join(self.tmp.name, "found")
        for name in ("A", "B"):
            report = os.path.join(folder, name + ".Report")
            os.makedirs(report)
            with open(os.path.join(report, "definition.pbir"), "w", encoding="utf-8") as f:
                json.dump({"datasetReference": {"byConnection": {
                    "connectionString": "Data Source=localhost;Initial Catalog=Parity"}}}, f)
        real = common.write_text

        def boom(path, text, bom=b"", *, root=None, **_kw):
            if path.endswith("c2.json"):
                raise OSError("second")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                consumers.discover(self.ws, self.project, folder, add_matches=True)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertIsNone(self._reg())
        self.assertEqual(_bytes(os.path.join(self.ws.private, "consumers")), {})

    def test_signature_oserror_is_missing(self):
        path = os.path.join(self.tmp.name, "q.dax")
        with open(path, "w", encoding="utf-8") as f:
            f.write("EVALUATE ROW()")
        with patch("os.stat", side_effect=OSError("stat")):
            self.assertEqual(consumers._signature({"kind": "queries", "path": path}), "missing")


if __name__ == "__main__":
    unittest.main()
