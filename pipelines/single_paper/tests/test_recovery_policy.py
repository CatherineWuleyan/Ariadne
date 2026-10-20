from __future__ import annotations

import unittest
from unittest.mock import patch

from agent_pipeline_v2.step4 import WeakpointExpansionTool, _clean_additions, _validate_expansion


class RecoveryPolicyTests(unittest.TestCase):
    def test_abduction_allows_only_source_supported_links(self) -> None:
        parameters = {
            "weakpoint": {"payload": {
                "reasoning_type": "abduction",
                "evidence_claim_ids": ["claim_1"],
                "target_claim_id": ["claim_3"],
            }},
            "knowledges": {
                "claim_1": {"type": "claim", "content": {"canonical": "Observation one"}},
                "claim_2": {"type": "claim", "content": {"canonical": "Observation two"}},
                "claim_3": {"type": "claim", "content": {"canonical": "Hypothesis"}},
            },
            "source_excerpts": [],
        }
        _validate_expansion(parameters, {
            "knowledges": {},
            "strategies": [{
                "scope": "local", "type": "abduction", "premises": ["claim_1"],
                "conclusion": "claim_3", "background": [],
            }],
        })

    def test_cleaner_split_returns_group_candidates_for_rebuild(self) -> None:
        parameters = {"knowledges": {}, "weakpoint": {"payload": {}}, "source_excerpts": []}
        result = {
            "knowledges": {"new_claim": {
                "type": "claim", "content": {"canonical": "compound proposition"},
                "source_anchor_ids": [],
            }},
            "strategies": [],
        }
        cleaned = {
            "claim": [
                {"number": 1, "text": "first proposition"},
                {"number": 2, "text": "second proposition"},
            ],
            "note": [], "relation": [],
        }
        with patch("agent_pipeline_v2.step4._clean_proposition", return_value=(cleaned, {"path": "", "sha256": ""})):
            rebuilt = _clean_additions(parameters, result, [])
        self.assertEqual([], rebuilt["strategies"])
        self.assertEqual({"new_claim_cleaned_1", "new_claim_cleaned_2"}, set(rebuilt["knowledges"]))

    def test_repair_prompt_contains_feedback_and_group_rebuild_instruction(self) -> None:
        prompt = WeakpointExpansionTool.prompt({
            "repair_feedback": "missing conclusion",
            "rebuild_group": True,
            "weakpoint": {"payload": {
                "reasoning_type": "deduction",
                "evidence_claim_ids": ["claim_1"],
                "target_claim_id": ["claim_3"],
            }}, "knowledges": {}, "source_excerpts": [],
        })
        self.assertIn("missing conclusion", prompt)
        self.assertIn("post-cleaning Group rebuild", prompt)


if __name__ == "__main__":
    unittest.main()
