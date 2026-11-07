"""A present consumer registry that cannot be trusted refuses before any write.

A missing registry stays "none" and does not block removal. Run: python tests/test_consumers_refusals.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, consumers, unused  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SALES = "table Sales\n\n\tmeasure Keep = 1\n\n"
ORPHAN = "table Orphan\n\n\tmeasure Gone = 1\n\n"
MODEL = "model Model\n\tculture: en-US\n\nref table Sales\nref table Orphan\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"
VISUAL = {"visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
    {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Keep"}}}
]}}}}}


def _bytes(root: str) -> dict[str, bytes]:
    out = {}
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


class ConsumerRefusalTests(unittest.TestCase):
    def _project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        ws.private = os.path.join(root, "private")
        os.makedirs(ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), SALES)
        _write(os.path.join(mdef, "tables", "Orphan.tmdl"), ORPHAN)
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        page = os.path.join(project.mirror_report, "definition", "pages", "p1")
        _write(os.path.join(page, "page.json"), json.dumps({"name": "p1", "displayName": "Synthetic"}))
        _write(os.path.join(page, "visuals", "v1", "visual.json"), json.dumps(VISUAL))
        return ws, project

    def _reg(self, ws) -> str:
        return os.path.join(ws.root, ".pbz", "consumers.json")

    def test_missing_registry_stays_none_and_allows_remove(self):
        ws, project = self._project()
        rep = unused.report(ws, project)
        self.assertIn("none", rep["scope"]["external_coverage"])
        self.assertEqual(rep["analysis"], "complete")
        out = unused.remove(ws, project, only=["measure"])
        self.assertEqual(out["result"], "REMOVED")

    def test_malformed_json_structure_and_entry_refuse(self):
        cases = {
            "{": "malformed",
            b"\xff\xfe": "malformed",
            json.dumps([]): "structure",
            json.dumps({"version": 1}): "structure",
            json.dumps({"version": 1, "consumers": [{"id": "c1", "kind": "pbir"}]}): "entry",
            json.dumps({"version": 1, "consumers": ["c1"]}): "entry",
            json.dumps({"version": 1, "consumers": [{"id": "", "kind": "manual", "objects": []}]}): "entry",
            json.dumps({"version": 1, "consumers": [{"id": "c1", "kind": "nope", "path": "x"}]}): "entry",
        }
        for body, word in cases.items():
            with self.subTest(word=word):
                ws, project = self._project()
                _write(self._reg(ws), body)
                before = _bytes(ws.root)
                with self.assertRaises(PbzError) as caught:
                    consumers.add(ws, project, objects=["[Gone]"], label="elsewhere")
                self.assertEqual(caught.exception.code, "REFUSED")
                self.assertIn(word, caught.exception.detail)
                self.assertNotIn(ws.root, caught.exception.detail)
                self.assertEqual(_bytes(ws.root), before)

    def test_unreadable_registry_refuses_without_write(self):
        ws, project = self._project()
        _write(self._reg(ws), json.dumps({"version": 1, "consumers": []}))
        before = _bytes(ws.root)
        real = open

        def boom(path, *args, **kwargs):
            if str(path).replace("\\", "/").endswith(".pbz/consumers.json"):
                raise PermissionError("denied-secret")
            return real(path, *args, **kwargs)

        with patch("builtins.open", boom):
            with self.assertRaises(PbzError) as caught:
                consumers.load_registry(ws)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("unreadable", caught.exception.detail)
        self.assertNotIn("denied-secret", caught.exception.detail)
        self.assertNotIn("PermissionError", caught.exception.detail)
        self.assertEqual(_bytes(ws.root), before)

    def test_add_does_not_overwrite_a_bad_registry(self):
        ws, project = self._project()
        path = self._reg(ws)
        _write(path, "{not json")
        with open(path, "rb") as f:
            before = f.read()
        with self.assertRaises(PbzError):
            consumers.add(ws, project, objects=["[Gone]"], label="elsewhere")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_unused_remove_refuses_when_registry_is_bad(self):
        ws, project = self._project()
        _write(self._reg(ws), "{")
        before = _bytes(ws.root)
        with self.assertRaises(PbzError) as caught:
            unused.remove(ws, project, only=["measure"])
        self.assertEqual(caught.exception.code, "INCOMPLETE")
        self.assertEqual(_bytes(ws.root), before)
        rep = unused.report(ws, project)
        self.assertEqual(rep["analysis"], "incomplete")
        self.assertTrue(any(w.get("code") == "CONSUMER_REGISTRY" for w in rep.get("warnings", [])))

    def test_discover_hint_uses_register(self):
        ws, project = self._project()
        folder = os.path.join(ws.root, "share")
        _write(os.path.join(folder, "bound.rdl"), "<Report><ConnectString>Initial Catalog=Mini</ConnectString></Report>")
        out = consumers.discover(ws, project, folder)
        self.assertIn("--register", out.get("next", ""))
        self.assertNotIn("--add", out.get("next", ""))


if __name__ == "__main__":
    unittest.main()
