"""Offline regression tests for unused-variable scope binding.

Run: python3 tests/test_rewrite_scopes.py
"""
from __future__ import annotations

import os
import sys
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.dirname(__file__))

from pbz import rewrite, suggest  # noqa: E402


class RewriteScopeTests(unittest.TestCase):
    def test_sibling_scope_live_var_survives(self):
        expr = '(VAR x = 1 VAR a = 2 RETURN x + a) + (VAR x = 3 VAR b = 4 RETURN b)'
        expected = '(VAR x = 1 VAR a = 2 RETURN x + a) + (VAR b = 4 RETURN b)'
        self.assertEqual(rewrite._unused(expr), expected)

    def test_dependency_chain_reduces_to_fixpoint(self):
        expr = 'VAR x = 1 VAR y = x RETURN 5'
        self.assertEqual(rewrite._unused(expr), '5')

    def test_nested_initializer_declaration_span(self):
        expr = 'VAR a = (VAR b = 1 RETURN b) VAR c = 2 RETURN c'
        self.assertEqual(rewrite._unused(expr), 'VAR c = 2 RETURN c')

    def test_second_pass_is_a_noop(self):
        for output in (
            '(VAR x = 1 VAR a = 2 RETURN x + a) + (VAR b = 4 RETURN b)',
            '5',
            'VAR c = 2 RETURN c',
        ):
            with self.subTest(output=output):
                self.assertIn(rewrite._unused(output), (None, output))

    def test_shadowed_inner_var_kept_when_used(self):
        expr = 'VAR x = 1 VAR y = (VAR x = 2 RETURN x) RETURN y + x'
        self.assertIn(rewrite._unused(expr), (None, expr))

    def test_unused_vars_reports_spelling_for_display(self):
        expr = '(VAR x = 1 VAR a = 2 RETURN x + a) + (VAR x = 3 VAR b = 4 RETURN b)'
        self.assertEqual(suggest._unused_vars(expr), ['x'])


if __name__ == '__main__':
    unittest.main()
