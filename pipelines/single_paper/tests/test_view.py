"""Regression tests for the V2 formalization view projection.

The viewer hides internal helper Knowledge nodes and rewires their incoming
edges onto their outgoing ones (``compactFormalGraph`` in ``viewer.html``).  A
synthesized helper such as Step 4's contradiction operand
``conjunction(claim_O03,claim_O04)`` therefore needs its operands projected as
input edges; with none, the helper is dropped together with its only outgoing
edge and the operator renders with a missing operand.
"""
from __future__ import annotations

import unittest

from agent_pipeline_v2.view import _flatten_helper_operands, _helper_operands


class HelperOperandTests(unittest.TestCase):
    def test_reads_operands_from_an_authoring_expression(self) -> None:
        kind, operands = _helper_operands(
            "conjunction(claim_O03,claim_O04)", {"claim_O03", "claim_O04"}
        )
        self.assertEqual("conjunction", kind)
        self.assertEqual(["claim_O03", "claim_O04"], operands)

    def test_reads_operands_from_a_compiler_expression(self) -> None:
        kind, operands = _helper_operands(
            "not_both_true(a,b)", {"a", "b"}
        )
        self.assertEqual("not_both_true", kind)
        self.assertEqual(["a", "b"], operands)

    def test_ignores_plain_claim_text_and_unknown_operands(self) -> None:
        self.assertEqual(("", []), _helper_operands("Some claim about accuracy.", {"a"}))
        self.assertEqual(("", []), _helper_operands(None, {"a"}))
        self.assertEqual(("conjunction", ["a"]), _helper_operands("conjunction(a,ghost)", {"a"}))
        # Whitespace around operands is tolerated.
        self.assertEqual(
            ("conjunction", ["a", "b"]), _helper_operands("conjunction( a , b )", {"a", "b"})
        )

    def test_flattens_nested_helper_chains_to_real_claims(self) -> None:
        expressions = {
            "helper_relation_1_1": "conjunction(claim_14,claim_15)",
            "helper_relation_1_2": "conjunction(helper_relation_1_1,claim_16)",
            "helper_relation_1_3": "conjunction(helper_relation_1_2,claim_17)",
            "helper_relation_1_4": "conjunction(helper_relation_1_3,claim_18)",
        }
        known = {"claim_14", "claim_15", "claim_16", "claim_17", "claim_18", *expressions}
        flattened = _flatten_helper_operands(expressions, known)
        self.assertEqual(
            ("conjunction", ["claim_14", "claim_15", "claim_16", "claim_17", "claim_18"]),
            flattened["helper_relation_1_4"],
        )
        # No helper may reference another helper: the viewer's contraction
        # rewires incoming x outgoing in node order, so a helper-to-helper edge
        # would make the result depend on that order.
        for kind, operands in flattened.values():
            self.assertEqual("conjunction", kind)
            for operand in operands:
                self.assertNotIn(operand, expressions)

    def test_helper_cycles_do_not_recurse_forever(self) -> None:
        expressions = {
            "helper_a": "conjunction(helper_b,claim_1)",
            "helper_b": "conjunction(helper_a,claim_2)",
        }
        known = {"claim_1", "claim_2", *expressions}
        flattened = _flatten_helper_operands(expressions, known)
        self.assertEqual(["claim_2", "claim_1"], flattened["helper_a"][1])
        self.assertEqual(["claim_1", "claim_2"], flattened["helper_b"][1])

    def test_reflexive_helper_yields_no_operands(self) -> None:
        expressions = {"helper_a": "conjunction(helper_a,claim_1)"}
        flattened = _flatten_helper_operands(expressions, {"claim_1", *expressions})
        self.assertEqual(("conjunction", ["claim_1"]), flattened["helper_a"])


if __name__ == "__main__":
    unittest.main()
