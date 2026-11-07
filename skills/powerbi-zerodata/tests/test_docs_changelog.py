"""A bad change-log line is skipped and never echoed. Run: python tests/test_docs_changelog.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, docs, mirror  # noqa: E402

SECRET = "SECRET_CHANGELOG_LINE_9f3a"


class DocsChangelogTests(unittest.TestCase):
    def test_valid_bad_valid_keeps_both_and_hides_the_bad_line(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "ws")
        test_parity.build(root)
        ws = common.Workspace(root)
        ws.private = os.path.join(tmp.name, "private")
        os.makedirs(ws.private)
        project = ws.project()
        mirror.pull(ws, project)
        os.makedirs(ws.pbz_dir, exist_ok=True)
        with open(os.path.join(ws.pbz_dir, "push-log.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"project": "Parity", "stamp": "s1", "objects": ["measure [A]"]}) + "\n")
            f.write(SECRET + " not-json\n")
            f.write(json.dumps({"project": "Other", "stamp": "other", "objects": ["measure [Z]"]}) + "\n")
            f.write(json.dumps({"project": "Parity", "stamp": "s2", "objects": ["measure [B]"]}) + "\n")
        doc = docs.collect(ws, project)
        self.assertEqual([row["stamp"] for row in doc["changelog"]], ["s1", "s2"])
        self.assertEqual(doc["changelog_status"], "INCOMPLETE")
        self.assertEqual(doc["changelog_skipped"], 1)
        rendered = json.dumps(doc) + docs.render_md(doc) + docs.render_html(doc)
        self.assertIn("s1", rendered)
        self.assertIn("s2", rendered)
        self.assertIn("INCOMPLETE", rendered)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("not-json", rendered)


if __name__ == "__main__":
    unittest.main()
