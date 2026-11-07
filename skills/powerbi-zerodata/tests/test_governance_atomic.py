"""Offline checks that governance add/remove and culture writers commit together.

Run: python tests/test_governance_atomic.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, governance  # noqa: E402
from pbz.common import PbzError  # noqa: E402

MODEL = "model Model\n\tculture: en-US\n\nref table Sales\n"
SALES = "table Sales\n\n\tmeasure Amount = 1\n\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"
CULTURE = (
    "cultureInfo en-US\n\n"
    "\ttranslations\n\n"
    "\t\tmodel Model\n\n"
    "\t\t\ttable Src\n\n"
    "\t\t\t\tmeasure Amount\n"
    "\t\t\t\t\tcaption: One\n"
)


def _bytes(root: str) -> dict[str, bytes]:
    out = {}
    for cur, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(cur, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = f.read()
    return out


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


class GovernanceAtomicTests(unittest.TestCase):
    def _project(self, *, model=True):
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
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        if model:
            _write(os.path.join(mdef, "model.tmdl"), MODEL)
        return ws, project, mdef

    def test_second_write_rolls_back(self):
        ws, project, mdef = self._project()
        before = _bytes(ws.mirror)
        real = common.write_text
        calls = {"n": 0}

        def boom(path, text, bom=b""):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("injected")
            return real(path, text, bom)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                governance.culture_add(ws, project, "fr-FR")
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertFalse(os.path.exists(os.path.join(mdef, "cultures", "fr-FR.tmdl")))
        self.assertEqual(list(Path(ws.mirror).rglob("*.pbz-tmp")), [])

    def test_missing_model_refuses_before_write(self):
        ws, project, mdef = self._project(model=False)
        before = _bytes(ws.mirror)
        with patch("pbz.common.write_text", side_effect=AssertionError("wrote")):
            with self.assertRaises(PbzError) as caught:
                governance.culture_add(ws, project, "fr-FR")
        self.assertEqual(caught.exception.code, "NO_MIRROR")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertFalse(os.path.isdir(os.path.join(mdef, "cultures")))

    def test_filename_collision_refuses(self):
        ws, project, mdef = self._project()
        _write(os.path.join(mdef, "perspectives", "A_B.tmdl"), "perspective A_B\n")
        text = common.read_text(os.path.join(mdef, "model.tmdl"))[0]
        common.write_text(os.path.join(mdef, "model.tmdl"), text + "ref perspective A_B\n")
        before = _bytes(ws.mirror)
        with patch("pbz.common.commit_bytes", side_effect=AssertionError("committed")):
            with self.assertRaises(PbzError) as caught:
                governance.perspective_add(ws, project, "A/B")
        self.assertEqual(caught.exception.code, "NAME_TAKEN")
        self.assertNotIn("A/B", caught.exception.detail)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_remove_reports_files(self):
        ws, project, _mdef = self._project()
        governance.culture_add(ws, project, "fr-FR")
        out = governance.culture_remove(ws, project, "fr-FR")
        self.assertIn("files", out)
        self.assertTrue(any(name.endswith("fr-FR.tmdl") for name in out["files"]))

    def test_culture_multi_file_rolls_back(self):
        _ws, _project, mdef = self._project()
        _write(os.path.join(mdef, "cultures", "en-US.tmdl"), CULTURE)
        _write(os.path.join(mdef, "cultures", "fr-FR.tmdl"), CULTURE.replace("en-US", "fr-FR").replace("One", "Un"))
        before = _bytes(mdef)
        real = common.write_text
        calls = {"n": 0}

        def boom(path, text, bom=b""):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("injected")
            return real(path, text, bom)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                governance.culture_edits_move(mdef, "Src", "Dst", "Amount")
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(_bytes(mdef), before)
        self.assertEqual(list(Path(mdef).rglob("*.pbz-tmp")), [])


if __name__ == "__main__":
    unittest.main()
