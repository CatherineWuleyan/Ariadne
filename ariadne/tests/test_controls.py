"""Controls for the link judge, tested without any model call."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ariadne.controls import (
    NEGATIONS,
    candidate_roots,
    cross_domain_controls,
    expanded_pool,
    in_domain_controls,
    preview,
    run,
)


def view() -> dict:
    return {
        "title": "toy",
        "nodes": [
            {"id": "knowledge:papers:paper_a::claim_1", "kind": "claim", "layer": "claims",
             "summary": "Model calibration against measured flight data reduced prediction error.",
             "details": {"content": "Model calibration against measured flight data reduced prediction error."}},
            {"id": "knowledge:papers:paper_a::claim_2", "kind": "claim", "layer": "claims",
             "summary": "Aerodynamic delta models are identified from doublet manoeuvres and used to update the database.",
             "details": {"content": "Aerodynamic delta models are identified from doublet manoeuvres and used to update the database."}},
            {"id": "knowledge:papers:paper_b::claim_1", "kind": "claim", "layer": "claims",
             "summary": "Hardware-in-the-loop testing validated the controller before flight test with tracking criteria.",
             "details": {"content": "Hardware-in-the-loop testing validated the controller before flight test with tracking criteria."}},
        ],
        "edges": [
            {"source": "knowledge:papers:paper_a::claim_1",
             "target": "knowledge:papers:paper_a::claim_2", "semantic_type": "infer"},
        ],
    }


def answer_layer() -> dict:
    return {
        "question": {"zh": "问题", "en": "question"},
        "roots": [{
            "code": "R1", "id": "answer:r1",
            "title_zh": "标定", "title_en": "Calibration",
            "statement": "Simulation fidelity can be improved by identifying aerodynamic deltas from flight-test data.",
            "scope": "", "criteria": "", "answer_type": "method_route",
            "seed_claim_ids": ["knowledge:papers:paper_a::claim_1"],
            "links": [{"claim_id": "knowledge:papers:paper_a::claim_1",
                       "relation": "support", "reason": "reports the same calibration"}],
        }],
    }


class RepresentationSwapTests(unittest.TestCase):
    def test_pool_is_richer_but_covers_the_same_claims(self) -> None:
        root = candidate_roots(answer_layer())[0]
        swap = expanded_pool(view(), root)
        self.assertTrue(swap)
        for item in swap:
            self.assertIn("neighbours", item)
            self.assertGreater(len(item["content"]), 0)

    def test_neighbours_come_from_the_same_paper(self) -> None:
        root = candidate_roots(answer_layer())[0]
        pool = expanded_pool(view(), root)
        paired = [item for item in pool
                  if item["id"] == "knowledge:papers:paper_a::claim_1"]
        self.assertTrue(paired, "the seed claim should be in the pool")
        neighbour_codes = [n["code"] for n in paired[0]["neighbours"]]
        expected = next(item["code"] for item in pool
                        if item["id"] == "knowledge:papers:paper_a::claim_2")
        # The toy view links claim_1 to claim_2, so claim_2 must appear as context.
        self.assertIn(expected, neighbour_codes)


class NegationTests(unittest.TestCase):
    def test_two_negations_per_candidate_and_both_kinds_present(self) -> None:
        targets = {item["target"] for item in NEGATIONS}
        self.assertEqual(len(NEGATIONS), 2 * len(targets))
        kinds = {item["kind"] for item in NEGATIONS}
        self.assertEqual(kinds, {"direct_denial", "opposite_route"})


class ControlPoolTests(unittest.TestCase):
    def test_control_entries_carry_no_tier_marker(self) -> None:
        entries, meta = in_domain_controls(view())
        # The toy view has none of the vetted codes, so the pool is empty but the
        # metadata contract still has to hold for every entry that is produced.
        self.assertEqual(entries, [])
        self.assertEqual(meta, [])
        physics = {"nodes": [
            {"id": "p1", "kind": "claim", "details": {"english": "A" * 200}},
        ]}
        cross, cross_meta = cross_domain_controls(physics)
        self.assertEqual(len(cross), 1)
        entry = cross[0]
        self.assertNotIn("control", entry)
        self.assertNotIn("control", json.dumps(entry))
        self.assertTrue(entry["id"].startswith("knowledge:papers:paper_xc"))
        self.assertEqual(cross_meta[0]["tier"], "cross_domain")


class PreviewTests(unittest.TestCase):
    def test_preview_needs_no_model_and_shows_the_construction(self) -> None:
        text = preview(view(), candidate_roots(answer_layer()))
        self.assertIn("Representation swap", text)
        self.assertIn("Negated candidates", text)
        self.assertIn("Negative controls", text)
        self.assertIn("R1-N1", text)


class RunTests(unittest.TestCase):
    def _fake_judge(self, links_by_root):
        def call(prompt: str) -> dict:
            # The prompt embeds the candidate id; echo a fixed answer per root.
            for code, links in links_by_root.items():
                if f'"id": "{code}"' in prompt or f'"root_id": "{code}"' in prompt:
                    return {"root_id": code, "links": links}
            return {"root_id": "R1", "links": links_by_root.get("R1", [])}
        return call

    def test_run_produces_every_arm(self) -> None:
        support = {"claim_id": "knowledge:papers:paper_a::claim_1",
                   "relation": "support", "reason": "same calibration"}
        with tempfile.TemporaryDirectory() as tmp:
            view_path = Path(tmp) / "view.json"
            layer_path = Path(tmp) / "answer_layer.json"
            view_path.write_text(json.dumps(view()), encoding="utf-8")
            layer_path.write_text(json.dumps(answer_layer()), encoding="utf-8")
            report = run(view_path, layer_path,
                         call_json=self._fake_judge({"R1": [support], "R1-N1": []}),
                         out=Path(tmp))
            self.assertTrue((Path(tmp) / "controls.md").is_file())
        self.assertEqual(set(report["arms"]),
                         {"reproduce", "representation", "negation", "controls", "stability"})
        self.assertEqual(len(report["arms"]["negation"]), 2)
        # The toy view has none of the vetted control codes, so the controls arm
        # reports no rows rather than inventing a rate from zero injections.
        self.assertEqual(report["arms"]["controls"], [])
        self.assertIn("summary", report)

    def test_negated_candidate_identity_does_not_leak(self) -> None:
        # If the judge can see "negation" in the id or code, the measured effect
        # is inflated: it would be reacting to the label, not the statement.
        prompts: list[str] = []

        def capture(prompt: str) -> dict:
            prompts.append(prompt)
            return {"root_id": "R1", "links": []}

        with tempfile.TemporaryDirectory() as tmp:
            view_path = Path(tmp) / "view.json"
            layer_path = Path(tmp) / "answer_layer.json"
            view_path.write_text(json.dumps(view()), encoding="utf-8")
            layer_path.write_text(json.dumps(answer_layer()), encoding="utf-8")
            run(view_path, layer_path, call_json=capture, arms={"negation"})
        joined = "\n".join(prompts)
        self.assertNotIn("negation", joined.lower())
        self.assertNotIn("-N1", joined)
        self.assertNotIn("-N2", joined)

    def test_stability_arm_repeats_one_prompt_and_reports_the_spread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view_path = Path(tmp) / "view.json"
            layer_path = Path(tmp) / "answer_layer.json"
            view_path.write_text(json.dumps(view()), encoding="utf-8")
            layer_path.write_text(json.dumps(answer_layer()), encoding="utf-8")
            report = run(view_path, layer_path, call_json=self._fake_judge({"R1": []}),
                         arms={"stability"}, repeat=3)
        stability = report["arms"]["stability"]
        self.assertEqual(len(stability["runs"]), 3)
        self.assertEqual(stability["support_counts"], [0, 0, 0])
        self.assertEqual(stability["polarity_flips"], 0)

    def test_summary_counts_surviving_negation_support(self) -> None:
        support = {"claim_id": "knowledge:papers:paper_a::claim_1",
                   "relation": "support", "reason": "same calibration"}
        with tempfile.TemporaryDirectory() as tmp:
            view_path = Path(tmp) / "view.json"
            layer_path = Path(tmp) / "answer_layer.json"
            view_path.write_text(json.dumps(view()), encoding="utf-8")
            layer_path.write_text(json.dumps(answer_layer()), encoding="utf-8")
            # The judge returns support whatever the candidate says: that is the
            # failure the negation arm is meant to expose.
            report = run(view_path, layer_path,
                         call_json=self._fake_judge({"R1": [support], "R1-N1": [support]}))
        self.assertEqual(report["summary"]["negations_with_surviving_support"], 2)


if __name__ == "__main__":
    unittest.main()
