"""Routing protocol: one call per claim, plus a second opinion for neighbours."""
from __future__ import annotations

import json
import unittest

from pipeline_merge_question import question_layer as ql


def root(code: str, statement: str) -> dict:
    return {"id": f"answer:{code.lower()}", "code": code, "title_zh": code,
            "title_en": code, "statement": statement, "scope": "", "criteria": "",
            "answer_type": "method_route", "seed_claim_ids": []}


def node(node_id: str, text: str) -> dict:
    return {"id": node_id, "kind": "claim", "layer": "claims", "label": node_id[-2:],
            "details": {"content": text}}


def view() -> dict:
    return {
        "nodes": [
            node("knowledge:papers:paper_a::claim_1",
                 "Model calibration against measured flight data reduced prediction error."),
            node("knowledge:papers:paper_a::claim_2",
                 "A second paper_a claim about flight data calibration and prediction error."),
            node("knowledge:papers:paper_b::claim_1",
                 "Hardware-in-the-loop testing validated the controller before flight test."),
            node("knowledge:papers:paper_c::claim_1",
                 "Wing sizing under static load cases bounded the structural mass."),
        ],
        "edges": [],
    }


ROOTS = [
    root("R1", "Calibrate the simulation model against measured flight data."),
    root("R2", "Validate the simulation with hardware-in-the-loop testing."),
]


class RoutedProtocolTests(unittest.TestCase):
    def test_one_call_per_claim_and_claims_route_to_one_root(self) -> None:
        prompts: list[str] = []

        def judge(prompt: str) -> dict:
            prompts.append(prompt)
            if "hardware-in-the-loop" in prompt.lower():
                return {"links": [{"candidate": "R2", "relation": "support",
                                   "reason": "reports HIL validation"}]}
            return {"links": [{"candidate": "R1", "relation": "support",
                               "reason": "reports calibration"}]}

        links, calls = ql.link_roots_routed(view(), {"zh": "q", "en": "q"}, ROOTS,
                                           call_json=judge, multi_route=False, workers=1)
        claims = {claim["id"] for claim in view()["nodes"]}
        pooled = set()
        for code in ("R1", "R2"):
            pool = ql.candidate_pool(view(), next(r for r in ROOTS if r["code"] == code))
            pooled |= {item["id"] for item in pool}
        self.assertEqual(len(prompts), len(pooled & claims))
        self.assertEqual(calls[0]["decisions"], len(pooled & claims))
        # A claim is routed to exactly one root unless the second opinion adds one.
        for code, items in links.items():
            for item in items:
                self.assertIn(item["relation"], ql.LINK_RELATIONS)
        routed = {item["claim_id"] for items in links.values() for item in items}
        self.assertEqual(len(routed), len(pooled & claims))

    def test_audit_reports_multi_assignment_and_silence(self) -> None:
        # The protocol must not pretend competition is enforced: when a claim is
        # routed to two candidates, that is counted and visible.
        def judge(prompt: str) -> dict:
            return {"links": [
                {"candidate": "R1", "relation": "support", "reason": "a"},
                {"candidate": "R2", "relation": "support", "reason": "b"},
            ]}

        _, calls = ql.link_roots_routed(view(), {"zh": "", "en": ""}, ROOTS,
                                       call_json=judge, multi_route=False, workers=1)
        audit = calls[0]
        self.assertGreater(audit["claims_routed_to_several"], 0)
        self.assertGreaterEqual(audit["claims_routed_to_nothing"], 0)
        self.assertEqual(audit["claims"], audit["claims_routed_to_several"] + audit["claims_routed_to_nothing"])

    def test_empty_answer_means_no_link(self) -> None:
        def judge(prompt: str) -> dict:
            return {"links": []}

        links, _ = ql.link_roots_routed(view(), {"zh": "", "en": ""}, ROOTS,
                                       call_json=judge, multi_route=False, workers=1)
        self.assertEqual(links, {"R1": [], "R2": []})

    def test_unknown_candidate_is_dropped(self) -> None:
        def judge(prompt: str) -> dict:
            return {"links": [{"candidate": "R9", "relation": "support", "reason": "ghost"}]}

        links, _ = ql.link_roots_routed(view(), {"zh": "", "en": ""}, ROOTS,
                                       call_json=judge, multi_route=False, workers=1)
        self.assertEqual(sum(len(items) for items in links.values()), 0)

    def test_second_opinion_is_asked_for_neighbour_routes_only(self) -> None:
        asked: list[str] = []

        def judge(prompt: str) -> dict:
            asked.append(prompt)
            if "Judge ONE claim against EVERY" in prompt:
                # Everything routes to R1 on the first pass.
                return {"links": [{"candidate": "R1", "relation": "support",
                                   "reason": "reports calibration"}]}
            # The second opinion accepts support for the neighbour.
            return {"relation": "support", "reason": "also bears on HIL"}

        links, calls = ql.link_roots_routed(view(), {"zh": "", "en": ""}, ROOTS,
                                            call_json=judge, multi_route=True, workers=1)
        stages = {item["stage"]: item for item in calls}
        self.assertIn("second_opinion", stages)
        self.assertEqual(stages["second_opinion"]["asked"], stages["second_opinion"]["accepted"])
        self.assertTrue(links["R2"], "the accepted second opinion must appear as a link")
        self.assertTrue(all(item["relation"] == "support" for item in links["R2"]))
        self.assertEqual(len(asked), stages["route"]["claims"] + stages["second_opinion"]["asked"])

    def test_second_opinion_rejection_adds_nothing(self) -> None:
        def judge(prompt: str) -> dict:
            if "Judge ONE claim against EVERY" in prompt:
                return {"links": [{"candidate": "R1", "relation": "support", "reason": "calibration"}]}
            return {"relation": "none", "reason": "different route"}

        links, calls = ql.link_roots_routed(view(), {"zh": "", "en": ""}, ROOTS,
                                            call_json=judge, multi_route=True, workers=1)
        stages = {item["stage"]: item for item in calls}
        self.assertGreater(stages["second_opinion"]["asked"], 0)
        self.assertEqual(stages["second_opinion"]["accepted"], 0)
        self.assertEqual(links["R2"], [])


class ProtocolSelectionTests(unittest.TestCase):
    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ql.link_roots(view(), {"zh": "", "en": ""}, ROOTS,
                          call_json=lambda prompt: {"links": []}, protocol="nope")

    def test_budget_protocol_still_available(self) -> None:
        calls: list[str] = []

        def judge(prompt: str) -> dict:
            calls.append(prompt)
            return {"root_id": "R1", "links": [
                {"claim_id": "knowledge:papers:paper_a::claim_1", "relation": "support",
                 "reason": "calibration"}
            ]}

        links, raw = ql.link_roots(view(), {"zh": "", "en": ""}, ROOTS,
                                   call_json=judge, protocol="budget")
        self.assertEqual(len(calls), 2, "the legacy protocol calls once per candidate")
        self.assertIn("R1", links)
        self.assertEqual(raw[0]["root"], "R1")


class DominanceVerdictTests(unittest.TestCase):
    def _verdict(self, counts: dict[str, int]) -> str:
        nodes, links = [], []
        for paper, count in counts.items():
            for index in range(1, count + 1):
                node_id = f"knowledge:papers:{paper}::claim_{index}"
                nodes.append(node(node_id, f"claim {index} of {paper} about calibration"))
                links.append({"claim_id": node_id, "relation": "support", "reason": "x"})
        candidate = root("R1", "Calibrate the model.")
        layer = ql.build_answer_layer({"nodes": nodes, "edges": []},
                                      {"zh": "", "en": ""}, [candidate], {"R1": links})
        return layer["roots"][0]["verdict_class"]

    def test_a_dominant_single_paper_is_not_multi_paper(self) -> None:
        self.assertEqual(self._verdict({"paper_a": 44, "paper_b": 4}), "partial")

    def test_a_balanced_pair_is_multi_paper(self) -> None:
        self.assertEqual(self._verdict({"paper_a": 20, "paper_b": 9}), "primary")

    def test_a_token_second_paper_is_not_multi_paper(self) -> None:
        self.assertEqual(self._verdict({"paper_a": 9, "paper_b": 1}), "partial")


if __name__ == "__main__":
    unittest.main()
