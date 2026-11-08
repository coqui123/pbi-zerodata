"""Offline checks that unused --remove / --restore commit one round at a time.

A failed round leaves that round's mirror and trash byte-identical. A later cascade
round that cannot finish reports PARTIAL and keeps a restore point for pre-command bytes.

Run: python tests/test_unused_atomic.py
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

from pbz import common, unused, usage  # noqa: E402
from pbz.common import PbzError  # noqa: E402
from pbz.outcomes import envelope  # noqa: E402

SALES = "table Sales\n\n\tmeasure Keep = 1\n\n\tmeasure Mid = [Leaf]\n\n\tmeasure Leaf = 1\n\n"
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


def _write(path: str, data: str | bytes, *, bom: bool = False, newline: str = "\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f:
            f.write(data)
        return
    raw = data.encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    if newline != "\n":
        raw = raw.replace(b"\n", newline.encode("ascii"))
    with open(path, "wb") as f:
        f.write(raw)


class UnusedAtomicTests(unittest.TestCase):
    def _project(self, *, broken=False, bom=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        ws.private = os.path.join(root, "private")
        os.makedirs(ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        sales = SALES.replace("\n", "\r\n") if bom else SALES
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), sales, bom=bom)
        _write(os.path.join(mdef, "tables", "Orphan.tmdl"), ORPHAN)
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        report = project.mirror_report
        _write(os.path.join(report, "definition.pbir"),
               json.dumps({"datasetReference": {"byPath": {"path": "../Mini.SemanticModel"}}}))
        page = os.path.join(report, "definition", "pages", "p1")
        _write(os.path.join(page, "page.json"), json.dumps({"name": "p1", "displayName": "Synthetic"}))
        _write(os.path.join(page, "visuals", "v1", "visual.json"), "{" if broken else json.dumps(VISUAL))
        return ws, project

    def _sales(self, ws, project) -> str:
        return os.path.join(project.mirror_model, "definition", "tables", "Sales.tmdl")

    def test_failed_outcome_is_exit_1(self):
        out, rc = envelope({"ok": False, "error": "FAILED",
                            "detail": "write failed; mirror rolled back, no file changed"})
        self.assertEqual(rc, 1)
        self.assertEqual(out["outcome"]["status"], "FAILED")
        self.assertEqual(out["outcome"]["code"], "FAILED")

    def test_mid_write_rolls_back_the_round(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text
        calls = {"n": 0}

        def boom(path, text, bom=b"", *, root=None, **_kw):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("injected")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                unused.remove(ws, project, only=["measure"])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertIn("no file changed", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_manifest_write_rolls_back_the_round(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text

        def boom(path, text, bom=b"", *, root=None, **_kw):
            if str(path).replace("\\", "/").endswith("manifest.json"):
                raise OSError("manifest")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                unused.remove(ws, project, only=["measure"])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_mid_delete_rolls_back_the_round(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text

        def boom(path, text, bom=b"", *, root=None, **_kw):
            if str(path).replace("\\", "/").endswith("model.tmdl"):
                raise OSError("ref")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                unused.remove(ws, project, only=["table"])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertTrue(os.path.isfile(os.path.join(project.mirror_model, "definition", "tables", "Orphan.tmdl")))
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_restore_failure_rolls_back(self):
        ws, project = self._project()
        out = unused.remove(ws, project, only=["measure"])
        removed = _bytes(ws.mirror)
        real = common.write_text

        def boom(path, text, bom=b"", *, root=None, **_kw):
            raise OSError("restore")

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                unused.restore(ws, project, out["restore_point"])
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(ws.mirror), removed)

    def test_conflict_refuses_before_write(self):
        ws, project = self._project()
        out = unused.remove(ws, project, only=["measure"])
        path = self._sales(ws, project)
        with open(path, "ab") as f:
            f.write(b"\n")
        changed = _bytes(ws.mirror)
        with patch("pbz.common.write_text", side_effect=AssertionError("wrote")):
            result = unused.restore(ws, project, out["restore_point"])
        self.assertEqual(result["result"], "CONFLICT")
        self.assertEqual(_bytes(ws.mirror), changed)

    def test_dry_run_writes_nothing(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        out = unused.remove(ws, project, only=["measure"], dry_run=True)
        self.assertEqual(out["result"], "DRY_RUN")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertFalse(os.path.isdir(os.path.join(ws.mirror, ".pbz-trash")))

    def test_bom_crlf_round_trip(self):
        ws, project = self._project(bom=True)
        path = self._sales(ws, project)
        original = open(path, "rb").read()
        self.assertTrue(original.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", original)
        out = unused.remove(ws, project, only=["measure"])
        restored = unused.restore(ws, project, out["restore_point"])
        self.assertEqual(restored["result"], "RESTORED")
        self.assertEqual(open(path, "rb").read(), original)

    def test_incomplete_refuses_before_write(self):
        ws, project = self._project(broken=True)
        before = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            unused.remove(ws, project, only=["measure"])
        self.assertEqual(caught.exception.code, "INCOMPLETE")
        self.assertIn("nothing removed", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_bad_stamp_and_corrupt_backup_refuse(self):
        ws, project = self._project()
        with self.assertRaises(PbzError) as caught:
            unused.restore(ws, project, "../stamp")
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        out = unused.remove(ws, project, only=["measure"])
        removed = _bytes(ws.mirror)
        stamp = out["restore_point"]
        backups = list(Path(ws.mirror, ".pbz-trash", stamp, "files").rglob("*.tmdl"))
        self.assertTrue(backups)
        backups[0].write_bytes(b"not-the-backup")
        mirror_now = _bytes(ws.mirror)
        with self.assertRaises(PbzError) as caught:
            unused.restore(ws, project, stamp)
        self.assertEqual(caught.exception.code, "INVALID")
        self.assertEqual(_bytes(ws.mirror), mirror_now)

    def _cascade(self, fail):
        ws, project = self._project()
        original = open(self._sales(ws, project), "rb").read()
        real_build = usage.build
        calls = {"n": 0}

        def build(*args, **kwargs):
            graph = real_build(*args, **kwargs)
            calls["n"] += 1
            if calls["n"] >= 2 and fail == "incomplete":
                graph.incomplete = True
            return graph

        real_commit = common.commit_bytes

        def commit(ops):
            if fail == "write" and calls["n"] >= 2:
                def boom(path, text, bom=b"", *, root=None, **_kw):
                    raise OSError("round2")
                with patch("pbz.common.write_text", boom):
                    return real_commit(ops)
            return real_commit(ops)

        with patch("pbz.usage.build", build), patch("pbz.unused.commit_bytes", commit):
            out = unused.remove(ws, project, only=["measure"], cascade=True)
        self.assertEqual(out["result"], "PARTIAL")
        self.assertNotIn("nothing removed", json.dumps(out))
        self.assertNotIn("no file changed", json.dumps(out))
        self.assertTrue(out["restore_point"])
        sales = open(self._sales(ws, project), "rb").read()
        self.assertNotIn(b"measure Mid", sales)
        self.assertIn(b"measure Leaf", sales)
        restored = unused.restore(ws, project, out["restore_point"])
        self.assertEqual(restored["result"], "RESTORED")
        self.assertEqual(open(self._sales(ws, project), "rb").read(), original)

    def test_cascade_incomplete_is_partial(self):
        self._cascade("incomplete")

    def test_cascade_write_failure_is_partial(self):
        self._cascade("write")


if __name__ == "__main__":
    unittest.main()
