"""Offline suites must be named in CI. test_engine.py --offline counts; e2e does not.
Run: python tests/test_ci_registration.py
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YML = os.path.join(ROOT, ".github", "workflows", "tests.yml")
LIVE_ONLY = {"e2e"}
OFFLINE_ENGINE = "python tests/test_engine.py --offline"


def parse(text: str):
    match = re.search(r"\$suites = (.*?)\n\s+\$failed", text, re.S)
    if not match:
        raise AssertionError("suite list not found")
    return set(re.findall(r'"([^"]+)"', match.group(1))), OFFLINE_ENGINE in text


def missing(text: str) -> list[str]:
    suites, offline = parse(text)
    bad = []
    tests = os.path.join(SKILL, "tests")
    for name in sorted(os.listdir(tests)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        stem = name[len("test_"):-len(".py")]
        if stem in LIVE_ONLY:
            continue
        if stem == "engine":
            if not offline:
                bad.append(stem)
            continue
        if stem not in suites:
            bad.append(stem)
    return bad


class CiRegistrationTests(unittest.TestCase):
    def test_checked_in_workflow_registers_offline_suites(self):
        text = open(YML, encoding="utf-8").read()
        self.assertEqual(missing(text), [])
        _suites, offline = parse(text)
        self.assertTrue(offline)
        self.assertNotIn("engine", LIVE_ONLY)

    def test_unregistered_offline_suite_fails_the_guard(self):
        text = open(YML, encoding="utf-8").read().replace('"ci_registration"', '"renamed_away"')
        self.assertIn("ci_registration", missing(text))

    def test_engine_offline_invocation_is_registration(self):
        text = open(YML, encoding="utf-8").read()
        self.assertNotIn("engine", missing(text))
        stripped = text.replace(OFFLINE_ENGINE, "python tests/test_engine.py")
        self.assertIn("engine", missing(stripped))


if __name__ == "__main__":
    unittest.main()
