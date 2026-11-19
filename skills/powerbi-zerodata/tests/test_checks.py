"""Probe mirror allowlist regressions: python tests/test_checks.py."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from pbz import checks, engine
from pbz.common import PbzError


class ProbeChecksTests(unittest.TestCase):
    def test_default_columns_use_exact_then_casefold_match(self):
        ws = SimpleNamespace(private="private")
        project = SimpleNamespace(mirror_model="mirror/Canary.SemanticModel")
        table = SimpleNamespace(name="Sales", columns={"Amount": SimpleNamespace(name="Amount")})
        other = SimpleNamespace(name="SALES", columns={"Other": SimpleNamespace(name="Other")})
        model = SimpleNamespace(tables={"Sales": table, "SALES": other})
        def call(verb, req, timeout):
            response = {"ok": True, "verb": "probe", "table": req["table"],
                        "columns": [{"column": c, "type": "double", "has_blanks": True} for c in req["columns"]]}
            return engine._project(verb, response, req)
        with patch.object(checks.policy_mod, "load", return_value={"disclosure": "strict"}), \
                patch.object(checks, "spend", return_value=True), \
                patch.object(checks.desktop, "require_port", return_value=1234), \
                patch.object(checks.tmdl, "load_model", return_value=model), \
                patch.object(checks, "engine_call", side_effect=call) as called:
            for name, column in (("sales", "Amount"), ("SALES", "Other")):
                out = checks.probe(ws, project, name, None)
                self.assertEqual(out["columns"], [{"column": column, "type": "double", "has_blanks": True}])
                self.assertEqual(called.call_args.args[1]["disclosure"], "strict")
            before = called.call_count
            with self.assertRaises(PbzError) as caught:
                checks.probe(ws, project, "unknown", None)
            self.assertEqual(caught.exception.code, "NOT_FOUND")
            self.assertEqual(called.call_count, before)
            self.assertEqual(checks.probe(ws, project, "unknown", ["Explicit"])["columns"][0]["column"], "Explicit")


if __name__ == "__main__":
    unittest.main()
