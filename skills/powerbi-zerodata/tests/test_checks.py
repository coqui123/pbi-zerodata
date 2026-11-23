"""Probe mirror allowlist regressions: python tests/test_checks.py."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from pbz import checks, engine, synthtest
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


class ExactInt64EqualTests(unittest.TestCase):
    def test_exact_int64_at_tol_zero(self):
        # Numbers exceeding 2^53 (9007199254740992) lose precision when cast to double
        a = 9007199254740993
        b = 9007199254740992
        self.assertFalse(synthtest._same(a, b, tol=0.0))
        self.assertTrue(synthtest._same(a, a, tol=0.0))

    def test_exact_int64_vs_float_at_tol_zero(self):
        # Int vs float at tol == 0
        self.assertTrue(synthtest._same(1, 1.0, tol=0.0))
        self.assertFalse(synthtest._same(1, 1.5, tol=0.0))
        self.assertFalse(synthtest._same(9007199254740993, 9007199254740992.0, tol=0.0))
        self.assertTrue(synthtest._same(9007199254740992, 9007199254740992.0, tol=0.0))

    def test_large_int_vs_float_no_overflow(self):
        # Extremely large ints beyond float range must not raise OverflowError
        self.assertFalse(synthtest._same(10**400, 1.0, tol=0.0))
        self.assertFalse(synthtest._same(1.0, 10**400, tol=0.0))

    def test_signed_zero_at_tol_zero(self):
        # +0.0 and -0.0 compare equal
        self.assertTrue(synthtest._same(0.0, -0.0, tol=0.0))
        self.assertTrue(synthtest._same(0, -0.0, tol=0.0))

    def test_nan_at_tol_zero(self):
        # NaN is never equal to NaN or any other value
        nan = float("nan")
        self.assertFalse(synthtest._same(nan, nan, tol=0.0))
        self.assertFalse(synthtest._same(nan, 0.0, tol=0.0))
        self.assertFalse(synthtest._same(0.0, nan, tol=0.0))

    def test_regression_tol_greater_than_zero_unchanged(self):
        # For tol > 0, relative tolerance comparison is preserved
        a = 9007199254740993
        b = 9007199254740992
        self.assertTrue(synthtest._same(a, b, tol=1e-9))
        self.assertTrue(synthtest._same(100.0, 100.01, tol=0.01))
        self.assertFalse(synthtest._same(100.0, 100.01, tol=0.00001))


if __name__ == "__main__":
    unittest.main()
