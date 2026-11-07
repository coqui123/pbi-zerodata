"""Recipes parse every step first, then roll the mirror back as one commit.

Run: python tests/test_recipe_rollback.py
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

from pbz import cli, common, recipes, validate  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SENTINEL = "SENTINEL_ARG_9f3a"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _bytes(root: str) -> dict[str, bytes]:
    out = {}
    for cur, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(cur, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = f.read()
    return out


def _spec(steps, **extra) -> str:
    body = {"recipe": "t", "version": 1, "steps": steps}
    body.update(extra)
    return json.dumps(body)


class RecipeRollbackTests(unittest.TestCase):
    def _project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        project = common.Project(ws, "Mini")
        mdef = os.path.join(project.mirror_model, "definition")
        _write(os.path.join(mdef, "model.tmdl"), "model Model\n\tculture: en-US\n\nref table Sales\n")
        _write(os.path.join(mdef, "database.tmdl"), "database\n\tcompatibilityLevel: 1400\n")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"),
               "table Sales\n\tmeasure Keep = 1\n\tpartition Sales = m\n\t\tmode: import\n\t\tsource = let x = 1 in x\n")
        _write(os.path.join(project.mirror_report, "definition.pbir"),
               '{"datasetReference": {"byPath": {"path": "../Mini.SemanticModel"}}}\n')
        _write(os.path.join(project.mirror_report, "definition", "report.json"), "{}\n")
        return ws, project

    def _evidence(self, ws) -> str:
        path = os.path.join(ws.pbz_dir, "evidence.jsonl")
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_dry_run_restores(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        spec = _spec([{"verb": "set", "args": ["[Keep]", "displayFolder=KPIs"]}])
        out = recipes.run(ws, project, spec, dry_run=True)
        self.assertEqual(out["result"], "DRY_RUN")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertNotIn("displayFolder", self._evidence(ws))

    def test_failed_step_rolls_back(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        spec = _spec([
            {"verb": "set", "args": ["[Keep]", "displayFolder=KPIs"]},
            {"verb": "relationship", "args": ["add", "'No'[Such]", "'No'[Other]"]},
        ])
        out = recipes.run(ws, project, spec)
        self.assertEqual(out["result"], "ROLLED_BACK")
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertEqual(out["files_changed"], [])

    def test_restore_failure_is_recovery_required(self):
        ws, project = self._project()
        spec = _spec([
            {"verb": "set", "args": ["[Keep]", "displayFolder=KPIs"]},
            {"verb": "relationship", "args": ["add", "'No'[Such]", "'No'[Other]"]},
        ])
        real = common.commit_bytes

        def boom(ops, private=False):
            if any(op[0] == "write_bytes" for op in ops):
                raise PbzError("FAILED", "SECRET_RESTORE_PATH")
            return real(ops, private=private)

        with patch("pbz.recipes.commit_bytes", boom):
            with self.assertRaises(PbzError) as caught:
                recipes.run(ws, project, spec)
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        self.assertNotIn("SECRET_RESTORE_PATH", caught.exception.detail)
        self.assertNotIn("OSError", caught.exception.detail)

    def test_forbidden_steps_execute_zero(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        calls = {"n": 0}
        real = recipes._call

        def spy(*a, **k):
            calls["n"] += 1
            return real(*a, **k)

        with patch("pbz.recipes._call", spy):
            with self.assertRaises(PbzError):
                recipes.run(ws, project, _spec([{"verb": "image", "args": ["list"]}]))
            with self.assertRaises(PbzError):
                recipes.run(ws, project, _spec([{"verb": "unused", "args": ["--verify-engine"]}]))
            with self.assertRaises(PbzError):
                recipes.run(ws, project, _spec([{"verb": "unused", "args": ["{flag}"]}],
                                               params={"flag": "--verify-engine"}), {"flag": "--verify-engine"})
        self.assertEqual(calls["n"], 0)
        self.assertEqual(_bytes(ws.mirror), before)

    def test_malformed_step_hides_sentinel(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        spec = _spec([
            {"verb": "set", "args": ["[Keep]", "displayFolder=KPIs"]},
            {"verb": "set", "args": ["--not-a-flag", SENTINEL]},
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(PbzError) as caught:
                recipes.run(ws, project, spec)
        self.assertIn("step 2", caught.exception.detail)
        self.assertNotIn(SENTINEL, caught.exception.detail + buf.getvalue())
        self.assertNotIn("unrecognized", caught.exception.detail.lower())
        self.assertEqual(_bytes(ws.mirror), before)

    def test_validation_exception_restores(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        spec = _spec([{"verb": "set", "args": ["[Keep]", "displayFolder=KPIs"]}])

        real = validate.run_mirror
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError(SENTINEL)
            return real(*a, **k)

        with patch("pbz.validate.run_mirror", boom):
            out = recipes.run(ws, project, spec)
        blob = json.dumps(out)
        self.assertEqual(out["result"], "ROLLED_BACK")
        self.assertNotIn(SENTINEL, blob)
        self.assertEqual(_bytes(ws.mirror), before)
        self.assertNotIn("displayFolder", self._evidence(ws))

    def test_cli_dry_run_records_no_step_success(self):
        ws, project = self._project()
        before = _bytes(ws.mirror)
        spec = _spec([{"verb": "validate", "args": []}])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--workspace", ws.root, "--project", project.name, "recipe", "run", spec, "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(_bytes(ws.mirror), before)
        text = buf.getvalue() + self._evidence(ws)
        self.assertIn("DRY_RUN", text)
        self.assertNotIn("PARSED", self._evidence(ws))


if __name__ == "__main__":
    unittest.main()
