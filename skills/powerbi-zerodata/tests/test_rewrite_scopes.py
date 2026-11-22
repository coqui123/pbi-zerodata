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
from pbz.common import PbzError  # noqa: E402


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

    def test_argument_boundary_binds_use_to_outer_declaration(self):
        expr = 'VAR x = 1 VAR k = 2 RETURN IF(k, VAR x = 2 RETURN x, x)'
        self.assertIsNone(rewrite._unused(expr))

    def test_argument_boundary_dead_inner_declaration_is_refused_not_corrupting(self):
        expr = 'VAR x = 1 VAR k = 2 RETURN IF(k, VAR x = 2 RETURN 5, x)'
        with self.assertRaises(PbzError) as caught:
            rewrite._unused(expr)
        self.assertEqual(caught.exception.code, 'REFUSED')

    def test_constructor_element_boundary_keeps_outer_declaration(self):
        expr = 'VAR x = 10 VAR k = 1 RETURN { VAR x = 2 RETURN x, x + k }'
        self.assertIsNone(rewrite._unused(expr))

    def test_nested_block_declaration_does_not_capture_later_use(self):
        expr = 'VAR x = 1 VAR a = VAR x = 2 RETURN x RETURN x + a'
        self.assertIsNone(rewrite._unused(expr))

    def test_nested_block_declaration_does_not_shadow_outer_value(self):
        # The later use binds the outer b, so the outer declaration is not
        # deleted. The dead inner b cannot be removed without emptying its
        # block, so the rewrite refuses instead of emitting a candidate.
        expr = 'VAR b = 5 VAR a = VAR b = 1 RETURN 7 VAR c = b RETURN a + c'
        with self.assertRaises(PbzError) as caught:
            rewrite._unused(expr)
        self.assertEqual(caught.exception.code, 'REFUSED')

    def test_nested_block_body_block_span_covers_whole_initializer(self):
        expr = 'VAR a = VAR b = 1 RETURN VAR c = b RETURN c VAR keep = 2 RETURN keep'
        self.assertEqual(rewrite._unused(expr), 'VAR keep = 2 RETURN keep')

    def test_quoted_table_name_ends_an_initializer(self):
        expr = "VAR a = VAR t = 'Sales' VAR n = COUNTROWS(t) RETURN n RETURN a"
        self.assertIsNone(rewrite._unused(expr))

    def test_nested_block_after_an_operator_is_consumed(self):
        expr = 'VAR a = 1 + VAR b = 2 RETURN b VAR c = 3 RETURN c'
        self.assertEqual(rewrite._unused(expr), 'VAR c = 3 RETURN c')

    def test_nested_block_after_a_word_operator_is_consumed(self):
        expr = 'VAR a = NOT VAR b = 1 RETURN b VAR c = 2 RETURN c'
        self.assertEqual(rewrite._unused(expr), 'VAR c = 2 RETURN c')

    def test_parenthesized_block_as_whole_expression_is_unchanged(self):
        self.assertIsNone(rewrite._unused('(VAR a = VAR b = 1 RETURN b) + 1'))

    def test_unparenthesized_nested_initializer_span_covers_block(self):
        expr = 'VAR a = VAR b = 1 RETURN b VAR c = 2 RETURN c'
        self.assertEqual(rewrite._unused(expr), 'VAR c = 2 RETURN c')

    def test_self_reference_in_initializer_binds_outer(self):
        expr = 'VAR x = 1 VAR y = (VAR x = x + 1 RETURN x) RETURN y'
        self.assertIsNone(rewrite._unused(expr))

    def test_display_spelling_contract_unchanged(self):
        self.assertEqual(suggest._unused_vars('VAR x = 1 VAR y = x RETURN 5'), ['y'])
        self.assertEqual(suggest._unused_vars('VAR x = 1'), ['x'])

    def test_fixpoint_reduction_that_would_strand_return_is_refused(self):
        expr = 'VAR x = 1 VAR y = x RETURN (VAR z = 2 RETURN z)'
        with self.assertRaises(PbzError) as caught:
            rewrite._unused(expr)
        self.assertEqual(caught.exception.code, 'REFUSED')


if __name__ == '__main__':
    unittest.main()
