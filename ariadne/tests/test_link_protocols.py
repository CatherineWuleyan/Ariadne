"""The two link protocols, measured against each other without a model call."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ariadne.link_protocols import (
    agreement,
    claim_all_prompt,
    evaluate,
    pair_prompt,
    parse_candidate_all,
    parse_claim_all,
    parse_pair,
    run,
)


def view() -> dict:
    return {
        "nodes": [
            {"id": "knowledge:papers:paper_a::claim_1", "kind": "claim", "layer": "claims",
             "label": "P1", "details": {"content": "Calibration against measured flight data reduced error."}},
            {"id": "knowledge:papers:paper_a::claim_2", "kind": "claim", "layer": "claims",
             "label": "P2", "details": {"content": "Wing sizing under +3g and -1g load cases."}},
            {"id": "knowledge:papers:paper_b::claim_1", "kind": "claim", "layer": "claims",
             "label": "P3", "details": {"content": "Route fuel burn for three missions was compared."}},
        ],
        "edges": [],
    }


def answer_layer() -> dict:
    return {
        "question": {"zh": "问题", "en": "question"},
        "roots": [
            {"code": "R1", "id": "answer:r1", "statement": "Calibrate the model from flight data."},
            {"code": "R2", "id": "answer:r2", "statement": "Size the wing under static load cases."},
        ],
        "links": None,
    }


def with_links() -> dict:
    layer = answer_layer()
    layer["roots"][0]["links"] = [
        {"claim_id": "knowledge:papers:paper_a::claim_1", "relation": "support"},
    ]
    return layer


class PromptTests(unittest.TestCase):
    def test_pair_prompt_contains_exactly_one_claim_and_one_candidate(self) -> None:
        prompt = pair_prompt({"id": "c1", "code": "P1", "content": "text"},
                             {"code": "R1", "statement": "calibrate"})
        self.assertIn("P1", prompt)
        self.assertIn("R1", prompt)
        self.assertNotIn("R2", prompt)
        self.assertIn("none", prompt.lower())

    def test_claim_all_prompt_lists_every_candidate(self) -> None:
        prompt = claim_all_prompt({"id": "c1", "code": "P1", "content": "text"},
                                  [{"code": "R1", "statement": "a"},
                                   {"code": "R2", "statement": "b"}])
        self.assertIn('"code": "R1"', prompt)
        self.assertIn('"code": "R2"', prompt)
        self.assertIn("compete", prompt)


class ParserTests(unittest.TestCase):
    def test_pair_accepts_a_relation_and_rejects_none(self) -> None:
        candidate = {"code": "R1", "id": "answer:r1"}
        claim = {"id": "c1", "code": "P1", "content": "t"}
        self.assertEqual(
            parse_pair({"relation": "support", "reason": "r"}, claim, candidate),
            {("R1", "c1"): {"relation": "support", "reason": "r"}},
        )
        self.assertEqual(parse_pair({"relation": "none"}, claim, candidate), {})
        self.assertEqual(parse_pair({"relation": "maybe"}, claim, candidate), {})

    def test_claim_all_ignores_unknown_candidates_and_duplicates(self) -> None:
        candidates = [{"code": "R1", "id": "answer:r1"}, {"code": "R2", "id": "answer:r2"}]
        claim = {"id": "c1", "code": "P1", "content": "t"}
        parsed = parse_claim_all({"links": [
            {"candidate": "R1", "relation": "support", "reason": "a"},
            {"candidate": "R9", "relation": "support", "reason": "ghost"},
            {"candidate": "R2", "relation": "oppose", "reason": "b"},
            {"candidate": "R2", "relation": "support", "reason": "second answer"},
        ]}, claim, candidates)
        self.assertEqual(set(parsed), {("R1", "c1")})
        self.assertEqual(parsed[("R1", "c1")]["relation"], "support")

    def test_candidate_all_keeps_one_decision_per_claim(self) -> None:
        candidate = {"code": "R1", "id": "answer:r1"}
        claims = [{"id": "c1", "code": "P1", "content": "t"},
                  {"id": "c2", "code": "P2", "content": "t"}]
        parsed = parse_candidate_all({"decisions": [
            {"claim_id": "c1", "relation": "support", "reason": "a"},
            {"claim_id": "c2", "relation": "none"},
            {"claim_id": "c9", "relation": "support"},
        ]}, candidate, claims)
        self.assertEqual(set(parsed), {("R1", "c1")})


class MetricTests(unittest.TestCase):
    def test_recall_counts_only_baseline_support_that_is_recovered(self) -> None:
        baseline = {("R1", "c1"): {"relation": "support"},
                    ("R1", "c2"): {"relation": "support"}}
        decisions = {("R1", "c1"): {"relation": "support"},
                     ("R1", "c2"): {"relation": "oppose"},
                     ("R1", "c3"): {"relation": "support"}}
        stats = evaluate(decisions, baseline, controls=["c3"], candidates=["R1"])
        self.assertEqual(stats["baseline_support"], 2)
        self.assertEqual(stats["recall_vs_baseline"], 0.5)
        self.assertEqual(stats["new_support_not_in_baseline"], 1)
        self.assertEqual(stats["control_false_positives"], 1)

    def test_agreement_counts_relations_not_just_pairs(self) -> None:
        left = {("R1", "c1"): {"relation": "support"}, ("R1", "c2"): {"relation": "support"}}
        right = {("R1", "c1"): {"relation": "support"}, ("R1", "c2"): {"relation": "oppose"}}
        result = agreement(left, right)
        self.assertEqual(result["pairs"], 2)
        self.assertEqual(result["same_relation"], 1)
        self.assertEqual(result["agreement"], 0.5)


class SampleTests(unittest.TestCase):
    def test_an_explicit_sample_is_not_truncated(self) -> None:
        # A 311-claim run once reported numbers from 20 claims because the
        # default limit silently applied to an explicit sample.
        def call(prompt: str) -> dict:
            return {"relation": "none"}

        with tempfile.TemporaryDirectory() as tmp:
            view_path = Path(tmp) / "view.json"
            layer_path = Path(tmp) / "layer.json"
            view_path.write_text(json.dumps(view()), encoding="utf-8")
            layer_path.write_text(json.dumps(with_links()), encoding="utf-8")
            report = run(view_path, layer_path, call_json=call, protocols=["claim_all"],
                         sample=["knowledge:papers:paper_a::claim_1",
                                 "knowledge:papers:paper_a::claim_2",
                                 "knowledge:papers:paper_b::claim_1"],
                         limit_claims=2, workers=1)
        self.assertEqual(report["claim_count"], 3)
        self.assertEqual(report["protocols"]["claim_all"]["calls"], 3)


class RunTests(unittest.TestCase):
    def test_two_protocols_run_and_are_compared(self) -> None:
        seen: list[str] = []

        def call(prompt: str) -> dict:
            seen.append(prompt)
            if "EVERY candidate" in prompt:
                return {"links": [{"candidate": "R1", "relation": "support", "reason": "x"}]}
            return {"relation": "support", "reason": "x"}

        with tempfile.TemporaryDirectory() as tmp:
            view_path = Path(tmp) / "view.json"
            layer_path = Path(tmp) / "layer.json"
            view_path.write_text(json.dumps(view()), encoding="utf-8")
            layer_path.write_text(json.dumps(with_links()), encoding="utf-8")
            report = run(view_path, layer_path, call_json=call, protocols=["pairwise", "claim_all"],
                         limit_claims=3, workers=1, out=Path(tmp))
            self.assertTrue((Path(tmp) / "protocols.md").is_file())
        self.assertEqual(set(report["protocols"]), {"pairwise", "claim_all"})
        # pairwise: claims x candidates; claim_all: one call per claim
        self.assertEqual(report["protocols"]["pairwise"]["calls"], 3 * 2)
        self.assertEqual(report["protocols"]["claim_all"]["calls"], 3)
        self.assertIn("pairwise vs claim_all", report["agreement"])


if __name__ == "__main__":
    unittest.main()
