from __future__ import annotations

import copy
import unittest

from pipeline_harness.domain.models import build_knowledge_index, build_validation_report, validate_snapshot
from pipeline_harness.domain.tools import ToolCallRequest, ToolCallResponse, validate_tool_response


def valid_snapshot() -> dict:
    return {
        "schema_name": "gaia.formalization.snapshot",
        "schema_version": "1.0.0",
        "step": {"number": 4, "name": "step4_formalize_reasoning"},
        "snapshot_id": "snapshot_test_step_4",
        "parent_artifact_id": "artifact_parent",
        "inputs": [],
        "knowledge": [
            {"id": "A", "type": "claim", "content": {"canonical": "A is true"}, "self_contained": True, "origin": "extracted", "visibility": "public", "source_anchor_ids": [], "external_ids": [], "epistemic": {"prior_status": "unset"}},
            {"id": "B", "type": "claim", "content": {"canonical": "B is true"}, "self_contained": True, "origin": "extracted", "visibility": "public", "source_anchor_ids": [], "external_ids": [], "epistemic": {"prior_status": "unset"}},
            {"id": "H", "type": "claim", "content": {"canonical": "implies(A,B)"}, "self_contained": True, "origin": "generated_helper", "visibility": "formal_internal", "source_anchor_ids": [], "external_ids": [], "epistemic": {"prior_status": "not_applicable"}},
        ],
        "reasoning_units": [{"id": "S", "type": "deduction", "form": "formal", "premises": ["A"], "conclusion": "B", "background": [], "formal": {"interface_claims": ["A", "B"], "private_claims": ["H"], "operator_ids": ["O"]}, "coarse": None}],
        "operators": [{"id": "O", "type": "implication", "variables": ["A", "B"], "conclusion": "H"}],
        "non_reasoning_links": [],
        "source_anchors": [],
        "changes": {"added": [], "modified": [], "removed": [], "replaced": []},
        "formalization_level": "fine",
        "methodology": {"name": "05-formalization-methodology", "version": "1"},
    }


class DomainModelTests(unittest.TestCase):
    def test_validates_and_indexes_formalization(self) -> None:
        snapshot = valid_snapshot()
        validate_snapshot(snapshot, expected_step=4)
        index = build_knowledge_index(snapshot)
        self.assertEqual(["S"], next(item for item in index["entries"] if item["knowledge_id"] == "B")["incoming_reasoning"])

    def test_rejects_private_helper_prior(self) -> None:
        snapshot = valid_snapshot()
        snapshot["knowledge"][2]["epistemic"]["prior_status"] = "assigned"
        with self.assertRaisesRegex(ValueError, "cannot carry"):
            validate_snapshot(snapshot)

    def test_rejects_soft_implication_in_fine_snapshot(self) -> None:
        snapshot = valid_snapshot()
        snapshot["reasoning_units"][0] = {"id": "S", "type": "support", "form": "coarse", "premises": ["A"], "conclusion": "B", "background": [], "coarse": {"soft_implication": {"p1": 0.8, "p2": 0.5}}}
        with self.assertRaisesRegex(ValueError, "fine formalization"):
            validate_snapshot(snapshot)

    def test_tool_contract_preserves_raw_normalized_and_error_boundary(self) -> None:
        request = ToolCallRequest("call_1", "semantic-review", "1", "classify", [{"artifact_id": "a1"}])
        response = ToolCallResponse("call_1", "succeeded", {"vendor": "raw"}, {"reasoning_units": []})
        validate_tool_response(request, response)
        self.assertEqual({"vendor": "raw"}, response.to_dict()["raw"])
        with self.assertRaisesRegex(ValueError, "requires an error"):
            ToolCallResponse("call_1", "failed", {"vendor": "raw"})

    def test_validation_report_identifies_specific_broken_edge(self) -> None:
        snapshot = valid_snapshot()
        snapshot["non_reasoning_links"] = [{"id": "edge_missing_target", "link_type": "related", "source": "A", "target": "MISSING", "reasoning": False}]
        report = build_validation_report(snapshot, expected_step=4)
        self.assertEqual("failed", report["summary"]["status"])
        finding = next(item for item in report["findings"] if item["target"]["id"] == "edge_missing_target")
        self.assertEqual("edge", finding["target"]["type"])
        self.assertEqual("edge.references_closed", finding["rule"])
        self.assertEqual("/non_reasoning_links/0/target", finding["location"]["json_pointer"])


if __name__ == "__main__":
    unittest.main()
