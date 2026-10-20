"""One operand set may carry at most one relation type.

The relation gate judges a candidate pair inside every overlapping Step 2 group
that contains it, so the same operand set can come back as `conjunction` from one
group and `disjunction` from another.  Each verdict is valid alone, and the type
is part of the operator id, so nothing upstream collapses them.  These tests pin
the reconciliation that removes the conflict without inventing a winner.
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline_harness.models import ArtifactRef
from pipeline_merge.relations import (
    EXCLUSIVE_OPERATOR_TYPES,
    operand_key,
    reconcile_operator_types,
    reconcile_rendered_arms,
)
from pipeline_merge.view import MergeViewAdapter

DIGEST = "a" * 64


def _operator(kind, variables, identifier=None):
    return {"id": identifier or f"operator_{kind}_{'_'.join(variables)}",
            "type": kind, "variables": list(variables), "conclusion": None, "metadata": {}}


class OperatorTypeReconciliationTests(unittest.TestCase):
    def test_same_operand_set_typed_twice_is_dropped_whole(self):
        operators = [_operator("conjunction", ["a", "b"]), _operator("disjunction", ["a", "b"])]
        kept, conflicts = reconcile_operator_types(operators)
        self.assertEqual(kept, [], "an undecided relation must not be asserted")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["variables"], ["a", "b"])
        self.assertEqual(conflicts[0]["types"], ["conjunction", "disjunction"])

    def test_all_three_exclusive_types_are_reported_together(self):
        operators = [_operator("equivalence", ["a", "b"]), _operator("conjunction", ["a", "b"]),
                     _operator("disjunction", ["a", "b"])]
        kept, conflicts = reconcile_operator_types(operators)
        self.assertEqual(kept, [])
        self.assertEqual(conflicts[0]["types"], ["conjunction", "disjunction", "equivalence"])

    def test_conflict_is_order_independent(self):
        """The operands are an unordered set: `[a, b]` and `[b, a]` are one pair."""
        operators = [_operator("conjunction", ["a", "b"]), _operator("disjunction", ["b", "a"])]
        kept, conflicts = reconcile_operator_types(operators)
        self.assertEqual(kept, [])
        self.assertEqual(len(conflicts), 1)

    def test_distinct_operand_sets_are_left_alone(self):
        """A binary and a ternary conjunction are different relations, not a conflict."""
        operators = [_operator("conjunction", ["a", "b"]), _operator("conjunction", ["a", "b", "c"]),
                     _operator("disjunction", ["c", "d"])]
        kept, conflicts = reconcile_operator_types(operators)
        self.assertEqual(conflicts, [])
        self.assertEqual([item["type"] for item in kept], ["conjunction", "conjunction", "disjunction"])

    def test_surviving_operators_keep_their_order(self):
        operators = [_operator("contradiction", ["a", "b"]), _operator("conjunction", ["c", "d"]),
                     _operator("disjunction", ["c", "d"])]
        kept, conflicts = reconcile_operator_types(operators)
        self.assertEqual([item["variables"] for item in kept], [["a", "b"]])
        self.assertEqual([conflict["variables"] for conflict in conflicts], [["c", "d"]])

    def test_negation_passes_through(self):
        """A single-operand operator cannot conflict and must not be dropped."""
        operators = [_operator("negation", ["a"]), _operator("conjunction", ["a", "b"])]
        kept, conflicts = reconcile_operator_types(operators)
        self.assertEqual([item["type"] for item in kept], ["negation", "conjunction"])
        self.assertEqual(conflicts, [])

    def test_one_operator_per_operand_set_survives(self):
        operators = [_operator("contradiction", ["a", "b"]), _operator("equivalence", ["c", "d"])]
        kept, conflicts = reconcile_operator_types(operators)
        self.assertEqual(len(kept), 2)
        self.assertEqual(conflicts, [])

    def test_duplicate_spellings_of_one_operator_collapse(self):
        operators = [_operator("conjunction", ["a", "b"], "operator_conjunction_x"),
                     _operator("conjunction", ["a", "b"], "operator_conjunction_x")]
        kept, conflicts = reconcile_operator_types(operators)
        self.assertEqual(len(kept), 1)
        self.assertEqual(conflicts, [])

    def test_operand_key_ignores_variable_order(self):
        self.assertEqual(operand_key(_operator("conjunction", ["a", "b"])),
                         operand_key(_operator("conjunction", ["b", "a"])))

    def test_exclusive_types_cover_the_operator_gate(self):
        self.assertEqual(EXCLUSIVE_OPERATOR_TYPES,
                         frozenset({"equivalence", "contradiction", "conjunction", "disjunction"}))
        self.assertNotIn("negation", EXCLUSIVE_OPERATOR_TYPES)


class RenderedArmReconciliationTests(unittest.TestCase):
    def test_a_pair_with_two_exclusive_labels_is_not_drawn(self):
        arms = [("a", "b", "conjunction"), ("a", "b", "disjunction")]
        kept, conflicts = reconcile_rendered_arms(arms)
        self.assertEqual(kept, [])
        self.assertEqual(conflicts, [{"pair": ["a", "b"], "labels": ["conjunction", "disjunction"]}])

    def test_a_drawn_pair_is_unordered(self):
        arms = [("a", "b", "conjunction"), ("b", "a", "equivalence")]
        kept, _ = reconcile_rendered_arms(arms)
        self.assertEqual(kept, [])

    def test_reasoning_arms_may_share_a_pair_with_an_operator(self):
        """`deduction` says how one operand follows; it is not a claim about the set."""
        arms = [("a", "b", "conjunction"), ("a", "b", "deduction")]
        kept, conflicts = reconcile_rendered_arms(arms)
        self.assertEqual(sorted(kept), [("a", "b", "conjunction"), ("a", "b", "deduction")])
        self.assertEqual(conflicts, [])

    def test_identical_arms_deduplicate(self):
        arms = [("a", "b", "conjunction"), ("a", "b", "conjunction")]
        kept, conflicts = reconcile_rendered_arms(arms)
        self.assertEqual(kept, [("a", "b", "conjunction")])
        self.assertEqual(conflicts, [])

    def test_two_operand_sets_landing_on_one_pair_are_dropped(self):
        """A ternary star and a binary operator can collapse onto the same drawn pair."""
        arms = [("a", "b", "conjunction"), ("a", "b", "disjunction")]
        kept, conflicts = reconcile_rendered_arms(arms)
        self.assertEqual(kept, [])
        self.assertEqual(len(conflicts), 1)


class _Store:
    """Minimal RunStore stand-in: the adapter only resolves artifact paths."""

    def __init__(self, paths):
        self._paths = paths

    def artifact_path(self, ref):
        return self._paths[ref.artifact_id]


class MergeViewConflictTests(unittest.TestCase):
    """A stale artifact must not render mutually exclusive parallel edges."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _project(self, integration_payload):
        documents, refs = {}, []
        papers = {"namespace": "papers", "package_name": "alpha",
                  "knowledges": [{"id": "papers:alpha::claim_1", "content": "a paper claim",
                                  "type": "claim", "metadata": {}, "source_anchor_ids": []}],
                  "operators": [], "strategies": []}
        for index, payload in enumerate([papers, integration_payload]):
            kind = "gaia.ir" if index == 0 else "formalization"
            stage = "input" if index == 0 else "step4_formalize_integration"
            artifact_id = f"artifact_{index}"
            path = self.root / f"{artifact_id}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            documents[artifact_id] = path
            refs.append(ArtifactRef(artifact_id, kind, f"work/{artifact_id}.json",
                                    DIGEST, "application/json", stage))
        return MergeViewAdapter().project(_Store(documents), refs)

    @staticmethod
    def _integration(operators):
        return {"package": {"namespace": "integration", "name": "demo"},
                "knowledges": {
                    f"integration:demo::candidate_A_{index}": {
                        "type": "claim", "content": {"canonical": f"premise {index}"},
                        "source_anchor_ids": [], "metadata": {}}
                    for index in (1, 2, 3)
                },
                "graph": {"operators": operators, "strategies": []}}

    def test_conflicting_operators_produce_no_parallel_edges(self):
        view = self._project(self._integration([
            _operator("conjunction", ["integration:demo::candidate_A_1",
                                      "integration:demo::candidate_A_2"]),
            _operator("disjunction", ["integration:demo::candidate_A_1",
                                      "integration:demo::candidate_A_2"]),
        ]))
        self.assertEqual(view.edges, [], "no edge may carry an undecided relation")
        self.assertEqual(view.metadata["operator_type_conflict_count"], 1)
        # The operand-set guard already removed it, so the arm guard has nothing
        # left to drop: the two counters measure different collapse points.
        self.assertEqual(view.metadata["rendered_edge_conflict_count"], 0)

    def test_two_operand_sets_collapsing_on_one_pair_are_dropped(self):
        """A ternary star and a binary operator can land on the same drawn pair."""
        view = self._project(self._integration([
            _operator("conjunction", ["integration:demo::candidate_A_1",
                                      "integration:demo::candidate_A_2",
                                      "integration:demo::candidate_A_3"]),
            _operator("disjunction", ["integration:demo::candidate_A_1",
                                      "integration:demo::candidate_A_2"]),
        ]))
        pairs = {frozenset({edge["source"], edge["target"]}) for edge in view.edges}
        labels = {edge["semantic_type"] for edge in view.edges}
        self.assertEqual(view.metadata["operator_type_conflict_count"], 0)
        self.assertEqual(view.metadata["rendered_edge_conflict_count"], 1)
        self.assertNotIn("disjunction", labels, "the contradictory pair is not drawn")
        self.assertEqual(len(pairs), 1, "only the uncontested conjunction arm survives")

    def test_a_single_operator_still_renders(self):
        view = self._project(self._integration([
            _operator("conjunction", ["integration:demo::candidate_A_1",
                                      "integration:demo::candidate_A_2"]),
        ]))
        self.assertEqual([edge["semantic_type"] for edge in view.edges], ["conjunction"])

    def test_no_pair_carries_two_exclusive_labels(self):
        """The invariant the viewer must hold, whatever the artifact contains."""
        view = self._project(self._integration([
            _operator("conjunction", ["integration:demo::candidate_A_1",
                                      "integration:demo::candidate_A_2"]),
            _operator("equivalence", ["integration:demo::candidate_A_1",
                                      "integration:demo::candidate_A_2"]),
            _operator("disjunction", ["integration:demo::candidate_A_1",
                                      "integration:demo::candidate_A_2"]),
        ]))
        by_pair = {}
        for edge in view.edges:
            pair = frozenset({edge["source"], edge["target"]})
            by_pair.setdefault(pair, set()).add(edge["semantic_type"])
        for pair, labels in by_pair.items():
            exclusive = labels & EXCLUSIVE_OPERATOR_TYPES
            self.assertLessEqual(len(exclusive), 1, f"{pair} carries {sorted(exclusive)}")


if __name__ == "__main__":
    unittest.main()
