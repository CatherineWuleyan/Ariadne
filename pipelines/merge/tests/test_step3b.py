"""Step 3b must dedup the domain spine by meaning and re-link Package conclusions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json, read_json
from pipeline_merge.step3b import (
    SPINE_SCHEMA,
    Step3bAlignDomainSpinePlugin,
    _equivalence_classes,
    _locate_ungrouped,
    _merged_content,
    _rewrite_relation_endpoints,
)

_synthesize_links = Step3bAlignDomainSpinePlugin._synthesize_links


def _ir(name, cid, content):
    return {"namespace": "papers", "package_name": name, "scope": "local",
            "ir_hash": "sha256:" + "0" * 64,
            "knowledges": [{"id": f"papers:{name}::{cid}", "content": content,
                            "metadata": {}, "source_anchor_ids": [f"a:{name}"]}],
            "operators": [], "strategies": [], "composes": [], "formula_graphs": []}


def _candidate(qid, content, *, level=None):
    item = {"id": qid, "kind": "candidate_claim", "content": content, "status": "materialized",
            "provenance_qids": []}
    if level is not None:
        item["level"] = level
    return item


def _proposal(candidates, weakpoints=(), operators=()):
    return {"schema_name": "gaia.integration.step3_proposals", "schema_version": "1.1.0", "step": 3,
            "groups": [], "operators": list(operators), "weakpoints": list(weakpoints),
            "candidate_knowledges": list(candidates),
            "package_set": ["integration:a", "integration:b"],
            "source_context_artifact": {"artifact_id": "x", "kind": "integration.local_context",
                                        "sha256": "0" * 64}}


class DedupUnitTests(unittest.TestCase):
    def test_classes_are_mechanically_applied_regardless_of_response_order(self):
        candidates = [{"id": f"integration:x::candidate_K_{n}", "content": f"s{n}"}
                      for n in ("b", "a", "c", "d", "e")]
        groups = [{"ids": ["integration:x::candidate_K_c", "integration:x::candidate_K_a",
                           "integration:x::candidate_K_b"]},
                  {"ids": ["integration:x::candidate_K_e", "integration:x::candidate_K_d"]}]
        with patch("pipeline_merge.step3b._post_json", return_value={"groups": groups}):
            aliases, merges, _ = _equivalence_classes(candidates, max_workers=1)
        self.assertEqual(aliases["integration:x::candidate_K_a"], "integration:x::candidate_K_a")
        self.assertEqual(aliases["integration:x::candidate_K_c"], "integration:x::candidate_K_a")
        self.assertEqual(aliases["integration:x::candidate_K_e"], "integration:x::candidate_K_d")
        self.assertEqual([m["representative_qid"] for m in merges],
                         ["integration:x::candidate_K_a", "integration:x::candidate_K_d"])

    def test_unknown_and_singleton_ids_are_ignored(self):
        candidates = [{"id": "integration:x::candidate_K_a", "content": "s"},
                      {"id": "integration:x::candidate_K_b", "content": "s"}]
        groups = [{"ids": ["integration:x::candidate_K_a", "integration:x::candidate_K_b",
                           "integration:x::candidate_K_ghost"]},
                  {"ids": ["integration:x::candidate_K_a"]}]
        with patch("pipeline_merge.step3b._post_json", return_value={"groups": groups}):
            aliases, merges, _ = _equivalence_classes(candidates, max_workers=1)
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["merged_qids"],
                         ["integration:x::candidate_K_a", "integration:x::candidate_K_b"])
        self.assertNotIn("integration:x::candidate_K_ghost", aliases)

    def test_merged_content_keeps_every_wording(self):
        contents = {"k1": "first wording", "k2": "second wording"}
        merged = _merged_content(["k1", "k2"], contents)
        self.assertTrue(merged.startswith("first wording"))
        self.assertIn("k2: second wording", merged)

    def test_relations_are_repointed_and_self_loops_dropped(self):
        proposals = _proposal([], operators=[
            {"id": "op1", "type": "conjunction", "variables": ["k1", "k2"], "conclusion": None},
            {"id": "op2", "type": "conjunction", "variables": ["k1", "paper"], "conclusion": None},
        ], weakpoints=[{"id": "w1", "payload": {
            "evidence_claim_ids": ["prem"], "target_claim_id": ["k2"],
            "reasoning_type": "deduction", "evidence_anchor_ids": [], "expression": "[prem]->[k2]"}}])
        operators, weakpoints = _rewrite_relation_endpoints(
            proposals, {"k1": "k1", "k2": "k1", "prem": "prem", "paper": "paper"}, {"k2"})
        # k1+k2 collapse to one node, so that operator became a self-loop and goes.
        self.assertEqual([op["id"] for op in operators], ["op2"])
        self.assertEqual(weakpoints[0]["payload"]["target_claim_id"], ["k1"])
        self.assertEqual(weakpoints[0]["payload"]["expression"], "[prem]->[k1]")


class LinkSynthesisTests(unittest.TestCase):
    def test_support_becomes_deduction_and_contradiction_becomes_infer(self):
        weakpoints = _synthesize_links([
            {"id": "c1", "domain_id": "K", "relation": "supports", "confidence": 0.9, "reason": "r"},
            {"id": "c2", "domain_id": "K", "relation": "contradicts", "confidence": 0.8, "reason": "r"},
        ])
        types = {wp["payload"]["evidence_claim_ids"][0]: wp["payload"]["reasoning_type"]
                 for wp in weakpoints}
        self.assertEqual(types, {"c1": "deduction", "c2": "infer"})
        for weakpoint in weakpoints:
            self.assertEqual(weakpoint["payload"]["target_claim_id"], ["K"])

    def test_link_ids_are_stable(self):
        link = {"id": "c1", "domain_id": "K", "relation": "supports", "confidence": 1, "reason": ""}
        self.assertEqual(_synthesize_links([link])[0]["id"], _synthesize_links([link])[0]["id"])


class Step3bPipelineTests(unittest.TestCase):
    """End to end through the runner: Step 3b reads Step 3 and writes the spine."""

    DOMAIN_A = "integration:i::candidate_K_aa"
    DOMAIN_B = "integration:i::candidate_K_bb"
    PREMISE = "integration:i::candidate_A_cc"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _config():
        """Step 3b reads the frozen Step 3 artifact directly, so it is the only stage."""
        return {"pipeline_id": "pipeline-merge-step3b-test", "version": "0.1.0", "stages": [
            {"name": "step0_select_scope", "plugin": "pipeline_merge.step0:Step0SelectScopePlugin",
             "options": {"mode": "reconcile", "scope": {"domain": "test",
                                                         "selection": ["papers:new"]}}},
            {"name": "step3b_align_domain_spine",
             "plugin": "pipeline_merge.step3b:Step3bAlignDomainSpinePlugin", "options": {}},
        ]}

    def _stage3_fixture(self):
        """Two paraphrased domain conclusions plus one unattached Package conclusion."""
        return _proposal(
            [_candidate(self.DOMAIN_A, "sparsity governs transfer", level=1),
             _candidate(self.DOMAIN_B, "transfer is governed by sparsity", level=1),
             _candidate(self.PREMISE, "a cluster found the same effect")],
            weakpoints=[{"id": "w_prem", "payload": {
                "evidence_claim_ids": ["papers:new::claim_1"], "target_claim_id": [self.PREMISE],
                "reasoning_type": "deduction", "evidence_anchor_ids": [],
                "expression": f"[papers:new::claim_1] -> [{self.PREMISE}]"}}],
        )

    def _run(self, *, dedup, link):
        atomic_write_json(self.root/"new.json", _ir("new", "claim_1", "sparse networks transfer"))
        atomic_write_json(self.root/"proposals.json", self._stage3_fixture())
        atomic_write_json(self.root/"manifest.json", {"artifacts": [
            {"path": "new.json", "kind": "gaia.ir"},
            {"path": "proposals.json", "kind": "integration.step3_proposals"}]})
        store = RunStore.create(self.root/"runs", self._config(), input_manifest=self.root/"manifest.json")

        def fake(prompt):
            if "SAME proposition worded differently" in prompt:
                return dedup
            if "DOMAIN_CONCLUSIONS=" in prompt:
                return link
            return {"weakpoints": []}

        with patch("pipeline_merge.step1._validate_with_official_gaia",
                   side_effect=lambda payload: (payload, "test")), \
             patch("pipeline_merge.step3b._post_json", side_effect=fake):
            run_pipeline(store.run_dir)
        spine_ref = next(r for r in store.load_artifacts() if r.kind == SPINE_SCHEMA)
        return read_json(store.artifact_path(spine_ref))

    def test_paraphrased_domain_conclusions_are_merged(self):
        spine = self._run(dedup={"groups": [{"ids": [self.DOMAIN_A, self.DOMAIN_B]}]},
                          link={"links": []})
        self.assertEqual(spine["domain_conclusion_count"], 1)
        self.assertEqual(len(spine["dedup_classes"]), 1)
        self.assertEqual(spine["dedup_classes"][0]["merged_qids"], [self.DOMAIN_A, self.DOMAIN_B])
        self.assertEqual(spine["dedup_classes"][0]["representative_qid"], self.DOMAIN_A)

    def test_unlinked_package_conclusion_is_relinked(self):
        spine = self._run(
            dedup={"groups": [{"ids": [self.DOMAIN_A, self.DOMAIN_B]}]},
            link={"links": [{"id": self.PREMISE, "domain_id": self.DOMAIN_A,
                             "relation": "supports", "confidence": 0.9, "reason": "same effect"}]})
        self.assertEqual(spine["unlinked_conclusion_ids"], [])
        self.assertEqual(len(spine["cluster_links"]), 1)
        synth = [wp for wp in spine["weakpoints"] if wp["id"].startswith("weakpoint_spine_link_")]
        self.assertEqual(len(synth), 1)
        self.assertEqual(synth[0]["payload"]["reasoning_type"], "deduction")
        self.assertEqual(synth[0]["payload"]["evidence_claim_ids"], [self.PREMISE])
        self.assertEqual(synth[0]["payload"]["target_claim_id"], [self.DOMAIN_A])

    def test_a_refused_link_is_reported_not_invented(self):
        spine = self._run(
            dedup={"groups": [{"ids": [self.DOMAIN_A, self.DOMAIN_B]}]},
            link={"links": [{"id": self.PREMISE, "domain_id": None, "relation": "none",
                             "confidence": 0.2, "reason": "topical only"}]})
        self.assertEqual(spine["cluster_links"], [])
        self.assertEqual(spine["unlinked_conclusion_ids"], [self.PREMISE])
        self.assertEqual([wp for wp in spine["weakpoints"]
                          if wp["id"].startswith("weakpoint_spine_link_")], [])

    def test_a_hallucinated_target_is_rejected(self):
        spine = self._run(
            dedup={"groups": [{"ids": [self.DOMAIN_A, self.DOMAIN_B]}]},
            link={"links": [{"id": self.PREMISE, "domain_id": "integration:i::candidate_K_ghost",
                             "relation": "supports", "confidence": 1.0, "reason": "invented"}]})
        self.assertEqual(spine["cluster_links"], [])
        self.assertEqual(spine["unlinked_conclusion_ids"], [self.PREMISE])


class UngroupedAuditTests(unittest.TestCase):
    """A conclusion in no relation must be located, not silently absent.

    `isolated` is the honest answer for a real gap; `synonym` is what stops the
    same proposal from standing as two nodes; `supports` is a relation the gate
    missed.  All three are recorded, and an unusable answer is treated as a gap
    rather than a link.
    """

    @staticmethod
    def _contents():
        return {
            "integration:i::candidate_K_aa": "the domain conclusion",
            "integration:i::candidate_A_cc": "a cluster reached the same effect",
            "integration:i::candidate_A_dd": "an unrelated cluster result",
        }

    def _locate(self, response, ungrouped=("integration:i::candidate_A_cc",)):
        with patch("pipeline_merge.step3b._post_json", return_value=response):
            return _locate_ungrouped(
                list(ungrouped),
                ["integration:i::candidate_K_aa"],
                ["integration:i::candidate_A_cc", "integration:i::candidate_A_dd"],
                self._contents(), max_workers=1)

    def test_a_synonym_is_reported_with_its_partner(self):
        rows = self._locate({"match": "synonym", "target": "integration:i::candidate_A_dd",
                             "reason": "same claim"})
        self.assertEqual(rows[0]["match"], "synonym")
        self.assertEqual(rows[0]["target"], "integration:i::candidate_A_dd")

    def test_an_invented_target_is_downgraded_to_isolated(self):
        rows = self._locate({"match": "synonym", "target": "integration:i::candidate_K_ghost",
                             "reason": "invented"})
        self.assertEqual(rows[0]["match"], "isolated")
        self.assertIsNone(rows[0]["target"])

    def test_a_support_link_names_a_real_domain_conclusion(self):
        rows = self._locate({"match": "supports", "target": "integration:i::candidate_K_aa",
                             "reason": "instance of it"})
        self.assertEqual((rows[0]["match"], rows[0]["target"]),
                         ("supports", "integration:i::candidate_K_aa"))

    def test_a_failed_call_is_recorded_as_unknown_not_as_a_link(self):
        with patch("pipeline_merge.step3b._post_json",
                   side_effect=RuntimeError("DeepSeek request failed after 5 attempts: 402")):
            rows = _locate_ungrouped(["integration:i::candidate_A_cc"],
                                     ["integration:i::candidate_K_aa"],
                                     ["integration:i::candidate_A_cc"], self._contents(), max_workers=1)
        self.assertEqual(rows[0]["match"], "unknown")
        self.assertIsNone(rows[0]["target"])

    def test_no_ungrouped_conclusions_makes_no_calls(self):
        with patch("pipeline_merge.step3b._post_json", side_effect=AssertionError("must not be called")):
            self.assertEqual(_locate_ungrouped([], ["k"], ["k"], {}), [])


class Step3bChainTests(unittest.TestCase):
    """Step 3b sits between two stages, so what it publishes must keep both fed.

    A checkpoint carries only the artifacts the stage itself produced.  A Step 3b
    that published just its audit spine therefore left Step 4 with no Step 3
    proposal at all — which is what the first three-cluster run hit.
    """

    DOMAIN_A = "integration:i::candidate_K_aa"
    DOMAIN_B = "integration:i::candidate_K_bb"
    PREMISE = "integration:i::candidate_A_cc"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _stages():
        return [
            {"name": "step0_select_scope", "plugin": "pipeline_merge.step0:Step0SelectScopePlugin",
             "options": {"mode": "reconcile", "scope": {"domain": "test",
                                                        "selection": ["papers:new"]}}},
            {"name": "step1_freeze_inputs",
             "plugin": "pipeline_merge.step1:Step1FreezeInputsPlugin", "options": {}},
            {"name": "step2_retrieve_domain_local",
             "plugin": "pipeline_merge.step2:Step2RetrieveDomainLocalPlugin", "options": {}},
            {"name": "step3_identify_structures",
             "plugin": "pipeline_merge.step3:Step3IdentifyStructuresPlugin", "options": {}},
            {"name": "step3b_align_domain_spine",
             "plugin": "pipeline_merge.step3b:Step3bAlignDomainSpinePlugin", "options": {}},
            {"name": "step4_formalize_integration",
             "plugin": "pipeline_merge.step4:Step4FormalizeIntegrationPlugin", "options": {}},
        ]

    def _write_inputs(self) -> None:
        atomic_write_json(self.root/"new.json", _ir("new", "claim_1", "sparse networks transfer"))
        atomic_write_json(self.root/"old.json", _ir("old", "claim_2", "sparse networks transfer"))
        atomic_write_json(self.root/"manifest.json", {"artifacts": [
            {"path": "new.json", "kind": "gaia.ir"},
            {"path": "old.json", "kind": "gaia.ir"}]})

    @staticmethod
    def _fake(prompt):
        candidates = json.loads(prompt.split("CANDIDATES=", 1)[1])
        if "SAME proposition worded differently" in prompt:
            return {"groups": [{"ids": [Step3bChainTests.DOMAIN_A, Step3bChainTests.DOMAIN_B]}]}
        if "DOMAIN_CONCLUSIONS=" in prompt:
            return {"links": [{"id": Step3bChainTests.PREMISE,
                               "domain_id": Step3bChainTests.DOMAIN_A,
                               "relation": "supports", "confidence": 0.9,
                               "reason": "same effect"}]}
        if "operator_candidate|not_operator" in prompt:
            return {"decisions": [{"candidate_id": item["candidate_id"],
                                   "decision": "not_operator"} for item in candidates]}
        if "deterministic operator" in prompt:
            return {"decisions": []}
        return {"weakpoints": []}

    def test_the_republished_proposal_survives_into_step4(self):
        self._write_inputs()
        config = {"pipeline_id": "pipeline-merge-step3b-chain-test", "version": "0.1.0",
                  "stages": self._stages()}
        store = RunStore.create(self.root/"runs", config, input_manifest=self.root/"manifest.json")
        with patch("pipeline_merge.step1._validate_with_official_gaia",
                   side_effect=lambda payload: (payload, "test")), \
             patch("pipeline_merge.step3._post_json", side_effect=self._fake), \
             patch("pipeline_merge.step3b._post_json", side_effect=self._fake), \
             patch("pipeline_merge.step4._post_json", side_effect=self._fake):
            run = run_pipeline(store.run_dir)

        self.assertNotEqual(run.status, "failed")
        published = [ref for ref in store.load_artifacts()
                     if ref.kind == "integration.step3_proposals"
                     and ref.producer_stage == "step3b_align_domain_spine"]
        self.assertEqual(len(published), 1, "Step 3b must republish the repaired proposal")
        aligned = read_json(store.artifact_path(published[0]))
        self.assertIn("spine_alignment", aligned)
        self.assertTrue(aligned["spine_alignment"]["spine_artifact_id"].startswith("sha256:"))
        formalization = next((ref for ref in store.load_artifacts()
                              if ref.kind == "formalization"
                              and ref.producer_stage == "step4_formalize_integration"), None)
        self.assertIsNotNone(formalization, "Step 4 must still receive the proposal")


if __name__ == "__main__":
    unittest.main()
