import unittest
from unittest.mock import patch

from pipeline_merge.step4 import (
    _abduction_premises,
    _equivalence_aliases,
    _materialize_candidates,
    _rewrite_operator,
    _rewrite_weakpoint,
    _strategies_from_weakpoint,
    _validate_acyclic,
)


def _record(qid, package, category="O", kind="claim"):
    return {"qid": qid, "package": package, "content": qid, "type": kind,
            "category": category, "metadata": {}, "source_anchor_ids": [f"anchor:{qid}"]}


class Step4Tests(unittest.TestCase):
    def test_same_category_equivalence_merges_before_rewriting_every_reference(self):
        records = {"a": _record("a", "p:a"), "b": _record("b", "p:b")}
        operators = [{"id": "eq", "type": "equivalence", "variables": ["a", "b"], "conclusion": None}]
        aliases, merges = _equivalence_aliases(operators, records)
        self.assertEqual({"a": "a", "b": "a"}, aliases)
        self.assertEqual(["anchor:a", "anchor:b"], merges[0]["source_anchor_ids"])
        self.assertIsNone(_rewrite_operator(operators[0], aliases))
        weakpoint = {"id": "w", "payload": {"evidence_claim_ids": ["b"], "target_claim_id": ["k"],
                     "reasoning_type": "deduction", "evidence_anchor_ids": [], "expression": "[b] 推出 [k]"}}
        rewritten = _rewrite_weakpoint(weakpoint, aliases)
        self.assertEqual(["a"], rewritten["payload"]["evidence_claim_ids"])
        self.assertEqual("[a] 推出 [k]", rewritten["payload"]["expression"])

    def test_different_categories_keep_equivalence_operator(self):
        records = {"a": _record("a", "p:a", "O"), "b": _record("b", "p:b", "E")}
        operator = {"id": "eq", "type": "equivalence", "variables": ["a", "b"], "conclusion": None}
        aliases, merges = _equivalence_aliases([operator], records)
        self.assertEqual([], merges)
        self.assertEqual(operator, _rewrite_operator(operator, aliases))

    def test_abduction_uses_expression_to_order_observation_and_alternative(self):
        payload = {"evidence_claim_ids": ["alt", "obs"], "target_claim_id": ["hyp"],
                   "reasoning_type": "abduction", "expression": "([hyp] 或 [alt]) 等价 [obs]"}
        self.assertEqual(["obs", "alt"], _abduction_premises(payload, {}))
        strategies = _strategies_from_weakpoint({"payload": payload}, {})
        self.assertEqual(["obs", "alt"], strategies[0]["premises"])

    def test_abduction_rejects_multiple_uncombined_alternatives(self):
        payload = {"evidence_claim_ids": ["obs", "h1", "h2"], "target_claim_id": ["hyp"],
                   "reasoning_type": "abduction", "expression": "([hyp] 或 [h1] 或 [h2]) 等价 [obs]"}
        with self.assertRaises(ValueError):
            _abduction_premises(payload, {})

    def test_abduction_allows_multiple_observation_premises(self):
        payload = {"evidence_claim_ids": ["obs1", "obs2"], "target_claim_id": ["hyp"],
                   "reasoning_type": "abduction", "expression": "[hyp] 等价 ([obs1] 且 [obs2])"}
        self.assertEqual(["obs1", "obs2"], _abduction_premises(payload, {}))

    def test_abduction_orders_multiple_observations_before_one_alternative(self):
        payload = {"evidence_claim_ids": ["alt", "obs1", "obs2"], "target_claim_id": ["hyp"],
                   "reasoning_type": "abduction",
                   "expression": "([hyp] 或 [alt]) 等价 ([obs1] 且 [obs2])"}
        self.assertEqual(["obs1", "obs2", "alt"], _abduction_premises(payload, {}))

    def test_analogy_without_target_condition_note_is_not_expanded(self):
        weakpoint = {"payload": {"evidence_claim_ids": ["law", "bridge"], "target_claim_id": ["target"],
                     "reasoning_type": "analogy", "expression": "[law] 通过 [bridge] 类比 [target]"}}
        with self.assertRaisesRegex(ValueError, "target-condition note"):
            _strategies_from_weakpoint(weakpoint, {})

    def test_candidate_k_materialization_is_bounded_to_known_candidate(self):
        candidate = {"id": "candidate_K_1", "kind": "candidate_claim", "content": None,
                     "status": "placeholder", "provenance_qids": ["a"]}
        weakpoint = {"id": "w", "payload": {"evidence_claim_ids": ["a"], "target_claim_id": ["candidate_K_1"],
                     "reasoning_type": "deduction", "expression": "[a] 推出 [candidate_K_1]"}}
        with patch("pipeline_merge.step4._post_json", return_value={
            "candidate_id": "candidate_K_1", "canonical_content": "Bounded summary of a."
        }):
            result = _materialize_candidates([candidate], [weakpoint], {"a": _record("a", "p:a")})
        self.assertEqual({"candidate_K_1": "Bounded summary of a."}, result)

    def test_cycle_is_rejected(self):
        strategies = [
            {"premises": ["a"], "conclusion": "b"},
            {"premises": ["b"], "conclusion": "a"},
        ]
        with self.assertRaisesRegex(ValueError, "cycle"):
            _validate_acyclic(strategies)


if __name__ == "__main__":
    unittest.main()
