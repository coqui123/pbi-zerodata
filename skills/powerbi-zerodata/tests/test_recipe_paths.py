"""Recipe snapshot and restore refuse aliased mirror trees.

Run: python tests/test_recipe_paths.py
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
sys.path.insert(0, os.path.join(SKILL, "tests"))

from _junction import make_junction  # noqa: E402
from pbz import common, recipes  # noqa: E402
from pbz.common import PbzError  # noqa: E402

CANARY = b"CANARY-LITERAL-99"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _spec() -> str:
    return json.dumps({"recipe": "t", "version": 1, "steps": [{"verb": "validate", "args": []}]})


class RecipePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["PBZ_PRIVATE_ROOT"] = os.path.join(self.tmp.name, "private")
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        root = self.tmp.name
        self.ws = common.Workspace(root)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        self.project = common.Project(self.ws, "Mini")
        model = os.path.join(self.project.mirror_model, "definition")
        _write(os.path.join(model, "model.tmdl"), "model Model\n")
        report = os.path.join(self.project.mirror_report, "definition")
        _write(os.path.join(report, "report.json"), "{}\n")
        self.pages = os.path.join(report, "pages")
        self.visual = os.path.join(self.pages, "p1", "visuals", "v1", "visual.json")
        self.outside = os.path.join(root, "outside")
        os.makedirs(self.outside, exist_ok=True)
        self.canary = os.path.join(self.outside, "secret.txt")
        with open(self.canary, "wb") as handle:
            handle.write(CANARY)

    def _reads(self, extra: str | None = None):
        hits: list[str] = []
        real = open
        watched = {os.path.normcase(os.path.abspath(self.canary))}
        prefixes = [os.path.normcase(os.path.abspath(self.outside)) + os.sep]
        if extra:
            watched.add(os.path.normcase(os.path.abspath(extra)))
            prefixes.append(os.path.normcase(os.path.abspath(extra)) + os.sep)

        def spy(path, *args, **kwargs):
            try:
                text = os.path.normcase(os.path.abspath(str(path)))
            except (TypeError, ValueError):
                text = ""
            if text in watched or any(text.startswith(prefix) for prefix in prefixes):
                hits.append(text)
            return real(path, *args, **kwargs)

        return hits, spy

    def test_junction_under_pages_refuses_with_zero_steps(self):
        os.makedirs(self.pages, exist_ok=True)
        link = os.path.join(self.pages, "p1")
        make_junction(self, link, self.outside)
        hits, spy = self._reads(link)
        calls = []
        with patch("pbz.recipes._call", side_effect=lambda *args: calls.append(args)):
            with patch("builtins.open", spy):
                with self.assertRaises(PbzError) as caught:
                    recipes.run(self.ws, self.project, _spec())
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn(b"CANARY", str(caught.exception).encode())
        self.assertEqual(calls, [])
        self.assertEqual(hits, [])
        self.assertEqual(open(self.canary, "rb").read(), CANARY)

    def test_hardlink_under_visuals_refuses_with_zero_steps(self):
        os.makedirs(os.path.dirname(self.visual), exist_ok=True)
        try:
            os.link(self.canary, self.visual)
        except OSError:
            self.skipTest("hardlink is unavailable")
        hits, spy = self._reads(self.visual)
        calls = []
        with patch("pbz.recipes._call", side_effect=lambda *args: calls.append(args)):
            with patch("builtins.open", spy):
                with self.assertRaises(PbzError) as caught:
                    recipes.run(self.ws, self.project, _spec())
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertEqual(calls, [])
        self.assertEqual(hits, [])
        self.assertEqual(open(self.canary, "rb").read(), CANARY)

    def test_mid_run_junction_is_recovery_required(self):
        os.makedirs(self.pages, exist_ok=True)
        link = os.path.join(self.pages, "p1")

        def plant(*_args):
            make_junction(self, link, self.outside)
            return 0, {"result": "OK"}

        hits, spy = self._reads(link)
        with patch("pbz.recipes._call", plant):
            with patch("builtins.open", spy):
                with self.assertRaises(PbzError) as caught:
                    recipes.run(self.ws, self.project, _spec())
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual(caught.exception.detail, "the recipe could not be restored")
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertNotIn(self.outside, str(caught.exception))
        self.assertEqual(hits, [])
        self.assertEqual(open(self.canary, "rb").read(), CANARY)


if __name__ == "__main__":
    unittest.main()
