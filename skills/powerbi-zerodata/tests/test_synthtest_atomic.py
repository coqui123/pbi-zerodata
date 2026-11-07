"""Synth test save and remove are one commit, and a stale save does not query.
Run: python tests/test_synthtest_atomic.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, mirror, synth, synthtest  # noqa: E402

SPEC = {"queries": [{"id": "q", "dax": "EVALUATE ROW(\"v\", 1)"}]}


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


class SynthtestAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["PBZ_SYNTH_ROOT"] = os.path.join(self.tmp.name, "synth")
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)
        self.calls = {"query": 0}

    def _mark(self, built):
        root = synth.synth_project(self.ws, self.project).ws.root
        os.makedirs(os.path.join(root, ".pbz"), exist_ok=True)
        with open(os.path.join(root, ".pbz", "policy.json"), "w", encoding="utf-8") as f:
            json.dump({"synthetic": True}, f)
        with open(os.path.join(root, ".pbz", "synth.json"), "w", encoding="utf-8") as f:
            json.dump({"source_project": "Parity", "built": built, "seed": 1, "rows": 20,
                       "tables": {}, "profile": "standard"}, f)

    def _query(self, *a, **k):
        self.calls["query"] += 1
        return {"columns": ["v"], "rows": [[1]]}

    def test_stale_save_queries_nothing(self):
        old = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        self._mark(old)
        for path in synthtest._model_files(self.project):
            os.utime(path, None)
        with patch("pbz.synthtest.QUERY", self._query):
            out = synthtest.save(self.ws, self.project, "stale", SPEC)
        self.assertEqual(out["result"], "NOT_COMPARABLE")
        self.assertEqual(out["error"], "STALE_EVIDENCE")
        self.assertEqual(out["queries"], [])
        self.assertEqual(self.calls["query"], 0)
        self.assertFalse(os.path.exists(synthtest._path(self.ws, "stale")))

    def test_mid_save_failure_restores(self):
        built = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        self._mark(built)
        path = synthtest._path(self.ws, "keep")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"name":"keep"}\n')
        before = file_map(os.path.dirname(path))
        with patch("pbz.synthtest.QUERY", self._query), patch("pbz.common.write_text", side_effect=OSError("disk")):
            with self.assertRaises(common.PbzError) as caught:
                synthtest.save(self.ws, self.project, "keep", SPEC, replace=True)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(file_map(os.path.dirname(path)), before)
        self.assertFalse(any(name.endswith(".pbz-tmp") for _d, _x, names in os.walk(self.ws.root) for name in names))

    def test_unreadable_is_refused_and_run_short_circuits(self):
        path = synthtest._path(self.ws, "gone")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        real = open

        def wrapped(file, *args, **kwargs):
            if isinstance(file, str) and file == path:
                raise PermissionError("secret-path")
            return real(file, *args, **kwargs)

        with patch("builtins.open", wrapped):
            with self.assertRaises(common.PbzError) as caught:
                synthtest._load(self.ws, "gone")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn("secret-path", str(caught.exception))
        old = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        self._mark(old)
        for model_path in synthtest._model_files(self.project):
            os.utime(model_path, None)
        doc = {"name": "when", "model_hash": synthtest.model_hash(self.project),
               "synth": {"seed": 1, "rows": 20, "profile": "standard"},
               "queries": [{"id": "q", "dax": "EVALUATE ROW()", "columns": ["v"], "rows": [[1]]}]}
        saved = synthtest._path(self.ws, "when")
        with open(saved, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        with patch("pbz.synthtest.QUERY", self._query):
            out = synthtest.run(self.ws, self.project, "when")
        self.assertEqual(out["result"], "NOT_COMPARABLE")
        self.assertEqual(out["queries"], [])
        self.assertIn("synth_stale", out.get("drift", {}))
        self.assertEqual(self.calls["query"], 0)


if __name__ == "__main__":
    unittest.main()
