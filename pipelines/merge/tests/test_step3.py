import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json, read_json
from pipeline_merge.step3 import (
    CONCLUSION_CAP,
    Step3IdentifyStructuresPlugin,
    _conclusion_groups,
    _is_summary_acceptable,
    _retrieve_related,
    _summarize_abduction_stars,
    _synthesize_conclusion_tree,
    merge_equivalent_nodes,
)


def _ir(name, cid, content, category=None):
    metadata = {"category": category} if category else {}
    return {"namespace":"papers","package_name":name,"scope":"local","ir_hash":"sha256:"+"0"*64,
            "knowledges":[{"id":f"papers:{name}::{cid}","content":content,"metadata":metadata,"source_anchor_ids":[f"a:{name}"]}],"operators":[],"strategies":[],"composes":[],"formula_graphs":[]}

def _config():
    stages=[
        {"name":"step0_select_scope","plugin":"pipeline_merge.step0:Step0SelectScopePlugin","options":{"mode":"incremental","scope":{"domain":"test","selection":["papers:new"]}}},
        {"name":"step1_freeze_inputs","plugin":"pipeline_merge.step1:Step1FreezeInputsPlugin","options":{}},
        {"name":"step2_retrieve_domain_local","plugin":"pipeline_merge.step2:Step2RetrieveDomainLocalPlugin","options":{}},
        {"name":"step3_identify_structures","plugin":"pipeline_merge.step3:Step3IdentifyStructuresPlugin","options":{}},]
    return {"pipeline_id":"pipeline-merge-step3-test","version":"0.4.0","stages":stages}

class Step3Tests(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def _run(self, old_content, evidence=None):
        atomic_write_json(self.root/"new.json", _ir("new","a","sparse networks transfer across dataset settings"))
        atomic_write_json(self.root/"old.json", _ir("old","b",old_content))
        atomic_write_json(self.root/"manifest.json", {"artifacts":[{"path":"new.json","kind":"gaia.ir"},{"path":"old.json","kind":"gaia.ir"}]})
        store=RunStore.create(self.root/"runs",_config(),input_manifest=self.root/"manifest.json")

        def llm(prompt):
            if evidence is not None and "Classify non-operator relations" in prompt:
                candidates = json.loads(prompt.split("CANDIDATES=", 1)[1])
                return {"weakpoints": [{"candidate_id": candidates[0]["candidate_id"],
                                        "evidence_claim_ids": [evidence],
                                        "target_claim_id": ["papers:new::a"],
                                        "reasoning_type": "deduction", "evidence_anchor_ids": [],
                                        "expression": f"[{evidence}] -> [papers:new::a]"}]}
            return self._llm(prompt)

        with patch("pipeline_merge.step1._validate_with_official_gaia",side_effect=lambda payload:(payload,"test")), \
             patch("pipeline_merge.step3._post_json", side_effect=llm):
            run=run_pipeline(store.run_dir)
        ref=next(r for r in store.load_artifacts() if r.kind=="integration.step3_proposals")
        return run,read_json(store.artifact_path(ref))

    @staticmethod
    def _llm(prompt):
        import json
        candidates = json.loads(prompt.split("CANDIDATES=", 1)[1])
        if "operator_candidate|not_operator" in prompt:
            decision = "not_operator" if "model settings" in prompt else "operator_candidate"
            return {"decisions": [{"candidate_id": item["candidate_id"], "decision": decision} for item in candidates]}
        if "deterministic operator candidates" in prompt:
            return {"decisions": [{"candidate_id": item["candidate_id"], "type": "equivalence", "variables": item["source_qids"], "expression": "equivalent"} for item in candidates]}
        return {"weakpoints": []}

    def test_exact_cross_package_hit_merges_the_two_nodes(self):
        """An approved equivalence is merged here, not left as two nodes plus an edge.

        The gate deciding "these are the same proposition" *is* the decision, so
        keeping both nodes is a duplicate, and leaving the equivalence as an
        operator would make Step 4/3b re-derive a merge Step 3 already knows.
        """
        run, proposal = self._run("sparse networks transfer across dataset settings")
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(proposal["operators"], [], "the equivalence is consumed by the merge")
        self.assertEqual(len(proposal["equivalence_merges"]), 1)
        merge = proposal["equivalence_merges"][0]
        self.assertEqual(merge["merged_qids"], ["papers:new::a", "papers:old::b"])
        self.assertEqual(merge["representative_qid"], "papers:new::a")

    def test_non_exact_hit_is_rejected_not_invented(self):
        run, proposal = self._run("sparse networks transfer across model settings")
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(proposal["operators"], [])
        self.assertEqual(proposal["weakpoints"], [])

    def test_relations_are_repointed_onto_the_representative(self):
        """Every endpoint in the stage moves with the merge, so nothing dangles."""
        _, proposal = self._run("sparse networks transfer across dataset settings",
                                evidence="papers:old::b")
        self.assertEqual(len(proposal["equivalence_merges"]), 1)
        self.assertNotIn("papers:old::b", proposal["equivalence_merges"][0]["representative_qid"])
        for weakpoint in proposal["weakpoints"]:
            endpoints = [*weakpoint["payload"]["evidence_claim_ids"],
                         *weakpoint["payload"]["target_claim_id"]]
            self.assertEqual(endpoints, list(dict.fromkeys(endpoints)))
        # The relation that used the absorbed id is gone only if it became a
        # self-loop; either way no surviving weakpoint may name the alias.
        for weakpoint in proposal["weakpoints"]:
            self.assertNotIn("papers:old::b",
                             [*weakpoint["payload"]["evidence_claim_ids"],
                              *weakpoint["payload"]["target_claim_id"]])

    def test_same_category_equivalence_is_merged_once(self):
        atomic_write_json(self.root/"new.json", _ir("new","a","sparse networks transfer across dataset settings", "O"))
        atomic_write_json(self.root/"old.json", _ir("old","b","sparse networks transfer across dataset settings", "O"))
        atomic_write_json(self.root/"manifest.json", {"artifacts":[{"path":"new.json","kind":"gaia.ir"},{"path":"old.json","kind":"gaia.ir"}]})
        store=RunStore.create(self.root/"runs",_config(),input_manifest=self.root/"manifest.json")
        with patch("pipeline_merge.step1._validate_with_official_gaia",side_effect=lambda payload:(payload,"test")), \
             patch("pipeline_merge.step3._post_json", side_effect=self._llm):
            run=run_pipeline(store.run_dir)
        ref=next(r for r in store.load_artifacts() if r.kind=="integration.step3_proposals")
        proposal=read_json(store.artifact_path(ref))
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(len(proposal["equivalence_merges"]), 1)
        self.assertEqual(proposal["equivalence_merges"][0]["merged_qids"],
                         ["papers:new::a", "papers:old::b"])


class EquivalenceMergeTests(unittest.TestCase):
    """An approved equivalence must leave one node, not two nodes and an edge."""

    @staticmethod
    def _equiv(left, right):
        return {"id": f"operator_equivalence_{left}_{right}", "type": "equivalence",
                "variables": [left, right], "conclusion": None, "metadata": {}}

    def test_a_class_collapses_to_its_representative(self):
        operators, weakpoints, aliases, merges = merge_equivalent_nodes(
            [self._equiv("c", "a"), self._equiv("b", "a")], [], {"a", "b", "c"})
        self.assertEqual(operators, [])
        self.assertEqual(aliases["b"], "a")
        self.assertEqual(aliases["c"], "a")
        self.assertEqual([m["merged_qids"] for m in merges], [["a", "b", "c"]])

    def test_endpoints_and_expressions_follow_the_merge(self):
        weakpoints = [{"id": "w1", "payload": {
            "evidence_claim_ids": ["paper1"], "target_claim_id": ["b"],
            "reasoning_type": "deduction", "evidence_anchor_ids": [],
            "expression": "[paper1] -> [b]"}}]
        _, rewritten, _, _ = merge_equivalent_nodes([self._equiv("b", "a")], weakpoints, {"a", "b"})
        self.assertEqual(rewritten[0]["payload"]["target_claim_id"], ["a"])
        self.assertEqual(rewritten[0]["payload"]["expression"], "[paper1] -> [a]")

    def test_a_relation_that_becomes_a_self_loop_is_dropped(self):
        weakpoints = [{"id": "w1", "payload": {
            "evidence_claim_ids": ["b"], "target_claim_id": ["a"],
            "reasoning_type": "deduction", "evidence_anchor_ids": [], "expression": "[b]->[a]"}}]
        _, rewritten, _, _ = merge_equivalent_nodes([self._equiv("b", "a")], weakpoints, {"a", "b"})
        self.assertEqual(rewritten, [], "a -> a is not a relation")

    def test_conjunction_is_not_a_merge_trigger(self):
        """Only equivalence merges; conjunction is a weak operator, not a decision of sameness."""
        operators = [{"id": "op1", "type": "conjunction", "variables": ["b", "a"],
                      "conclusion": None, "metadata": {}}]
        kept, _, aliases, merges = merge_equivalent_nodes(operators, [], {"a", "b"})
        self.assertEqual(len(kept), 1)
        self.assertEqual(merges, [])
        self.assertEqual(aliases, {})

    def test_unordered_operators_deduplicate_across_variable_order(self):
        """Rewriting can name the same operands in two orders; that is one operator.

        The compiler rejects a graph whose operators repeat an id, and an
        unordered operator's id is built from a sorted endpoint list.
        """
        operators = [{"id": "op1", "type": "conjunction", "variables": ["b", "a"],
                      "conclusion": None, "metadata": {"source": "one"}},
                     {"id": "op2", "type": "conjunction", "variables": ["a", "b"],
                      "conclusion": None, "metadata": {"source": "two"}}]
        kept, _, _, _ = merge_equivalent_nodes(
            [self._equiv("c", "b"), *operators], [], {"a", "b", "c"})
        self.assertEqual(len(kept), 1, "the two spellings are one operator")
        # `b` is the class representative (smallest id), and the endpoint order
        # the merge writes is sorted.
        self.assertEqual(kept[0]["variables"], ["a", "b"])

    def test_no_equivalence_leaves_everything_untouched(self):
        operators = [{"id": "op1", "type": "contradiction", "variables": ["a", "b"],
                      "conclusion": None, "metadata": {}}]
        kept, _, aliases, merges = merge_equivalent_nodes(operators, [], {"a", "b"})
        self.assertEqual(len(kept), 1)
        self.assertEqual((aliases, merges), ({}, []))


class LinkRetryTests(unittest.TestCase):
    """A group that found nothing in round 1 is asked again with a directed framing.

    The neutral first question ("is there a relation?") is answered conservatively
    for the abstract sentences a cross-Package group is made of, so a real link
    can be missed.  The retry must recover it through the same mechanical
    validation chain, and must not run when round 1 already produced a relation.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *, round_one, round_two):
        content = "sparse networks transfer across dataset settings"
        atomic_write_json(self.root/"new.json", _ir("new", "claim_1", content))
        atomic_write_json(self.root/"old.json", _ir("old", "claim_1", content))
        atomic_write_json(self.root/"manifest.json", {"artifacts": [
            {"path": "new.json", "kind": "gaia.ir"}, {"path": "old.json", "kind": "gaia.ir"}]})
        store = RunStore.create(self.root/"runs", _config(), input_manifest=self.root/"manifest.json")
        calls: list[str] = []

        def fake(prompt):
            if "Re-read both propositions from scratch" in prompt:
                kind = "operator_gate"          # round 2: the neutral re-ask
            elif "support, refine, contradict, or " in prompt:
                kind = "group_gate"             # round 1: the directed question
            elif "deterministic operator candidates" in prompt:
                kind = "operator_type"
            else:
                kind = "weakpoint"
            calls.append(kind)
            candidates = json.loads(prompt.split("CANDIDATES=", 1)[1])
            if kind in {"group_gate", "operator_gate"}:
                decision = round_one if kind == "group_gate" else round_two
                return {"decisions": [{"candidate_id": item["candidate_id"], "decision": decision}
                                      for item in candidates]}
            if kind == "operator_type":
                return {"decisions": [{"candidate_id": item["candidate_id"], "type": "equivalence",
                                       "variables": item["source_qids"], "expression": "equivalent"}
                                      for item in candidates]}
            return {"weakpoints": []}

        with patch("pipeline_merge.step1._validate_with_official_gaia",
                   side_effect=lambda payload: (payload, "test")), \
             patch("pipeline_merge.step3._post_json", side_effect=fake):
            run_pipeline(store.run_dir)
        ref = next(r for r in store.load_artifacts() if r.kind == "integration.step3_proposals")
        return calls, read_json(store.artifact_path(ref))

    def test_first_round_asks_the_directed_question(self):
        """The directed framing is round 1: it is where the relations come from."""
        calls, proposal = self._run(round_one="operator_candidate", round_two="insufficient_evidence")
        self.assertIn("group_gate", calls)
        self.assertNotIn("operator_gate", calls, "a group that answered round 1 needs no retry")
        # The operator it found is an equivalence, which Step 3 merges rather than
        # emitting: the merge record is the surviving evidence that it was found.
        self.assertEqual(len(proposal["equivalence_merges"]), 1)
        self.assertEqual(proposal["operators"], [])

    def test_the_retry_asks_the_other_question(self):
        """A group the directed round missed is re-asked neutrally, not identically."""
        calls, proposal = self._run(round_one="insufficient_evidence", round_two="operator_candidate")
        self.assertEqual(calls[:2], ["group_gate", "operator_gate"])
        self.assertEqual(len(proposal["equivalence_merges"]), 1)

    def test_no_retry_when_the_first_round_found_a_relation(self):
        calls, proposal = self._run(round_one="operator_candidate", round_two="operator_candidate")
        self.assertNotIn("operator_gate", calls)
        self.assertEqual(len(proposal["equivalence_merges"]), 1)


class JudgeResilienceTests(unittest.TestCase):
    """One unreachable group must cost that group, not the whole stage.

    Step 3 is minutes of retrieval plus hundreds of network calls; a dropped
    connection or a momentary quota error used to discard all of it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_group_whose_calls_never_succeed_is_dropped_and_reported(self):
        content = "sparse networks transfer across dataset settings"
        atomic_write_json(self.root/"new.json", _ir("new", "claim_1", content))
        atomic_write_json(self.root/"old.json", _ir("old", "claim_1", content))
        atomic_write_json(self.root/"manifest.json", {"artifacts": [
            {"path": "new.json", "kind": "gaia.ir"}, {"path": "old.json", "kind": "gaia.ir"}]})
        store = RunStore.create(self.root/"runs", _config(), input_manifest=self.root/"manifest.json")

        with patch("pipeline_merge.step1._validate_with_official_gaia",
                   side_effect=lambda payload: (payload, "test")), \
             patch("pipeline_merge.step3._post_json",
                   side_effect=RuntimeError("DeepSeek request failed after 5 attempts: 402")):
            run = run_pipeline(store.run_dir)

        self.assertNotEqual(run.status, "failed", "a failed group must not fail the stage")
        codes = [finding["code"] for finding in run.findings]
        self.assertIn("STEP3_GROUP_UNJUDGED", codes)
        ref = next(r for r in store.load_artifacts() if r.kind == "integration.step3_proposals")
        proposal = read_json(store.artifact_path(ref))
        self.assertEqual(proposal["operators"], [])
        self.assertEqual(proposal["weakpoints"], [])


def _record(qid, package):
    return {"qid": qid, "package": package, "content": f"content of {qid}", "type": "claim",
            "metadata": {}, "source_anchor_ids": [], "category": None}

class RetrievalCoverageTests(unittest.TestCase):
    """Bootstrap retrieval must anchor every Package, not just the smallest."""

    @staticmethod
    def _records():
        return {
            "papers:small::s1": _record("papers:small::s1", "papers:small"),
            "papers:mid::m1": _record("papers:mid::m1", "papers:mid"),
            "papers:mid::m2": _record("papers:mid::m2", "papers:mid"),
            "papers:big::b1": _record("papers:big::b1", "papers:big"),
            "papers:big::b2": _record("papers:big::b2", "papers:big"),
            "papers:big::b3": _record("papers:big::b3", "papers:big"),
        }

    @staticmethod
    def _nominate_every_candidate(prompt):
        candidates = json.loads(prompt.split("CANDIDATES=", 1)[1])
        return {"related": [{"qid": item["qid"], "direction": "tension", "reason": "test"}
                            for item in candidates]}

    def _retrieve(self):
        with patch("pipeline_merge.step3._post_json", side_effect=self._nominate_every_candidate):
            return _retrieve_related(self._records(), max_workers=2)

    def test_pair_between_two_non_smallest_packages_is_retrieved(self):
        pairs = {tuple(sorted(item["source_qids"])) for item in self._retrieve()}
        # Under the old single-anchor rule neither mid nor big ever anchors.
        self.assertIn(("papers:big::b1", "papers:mid::m1"), pairs)
        self.assertIn(("papers:big::b1", "papers:small::s1"), pairs)

    def test_each_unordered_pair_is_emitted_once(self):
        found = self._retrieve()
        pairs = [tuple(sorted(item["source_qids"])) for item in found]
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertEqual(len(set(pairs)), len(found))


def _judge(records, candidates, weakpoint_items):
    """Run _judge_candidates with a scripted gate and weakpoint response."""
    def fake(prompt):
        if "operator_candidate|not_operator" in prompt:
            supplied = json.loads(prompt.split("CANDIDATES=", 1)[1])
            return {"decisions": [{"candidate_id": item["candidate_id"], "decision": "not_operator"}
                                  for item in supplied]}
        if "deterministic operator candidates" in prompt:
            return {"decisions": []}
        return {"weakpoints": weakpoint_items}

    with patch("pipeline_merge.step3._post_json", side_effect=fake):
        return Step3IdentifyStructuresPlugin._judge_candidates(candidates, "g1", records)


def _pair(records, left, right):
    return {"candidate_id": "cand_1", "source_qids": [left, right],
            "propositions": [records[left], records[right]]}


class AnalogyArityTests(unittest.TestCase):
    """Step 4 needs [G_src, BridgeClaim]; Step 3 must not emit single-premise analogy."""

    @staticmethod
    def _run(evidence_count):
        records = {qid: _record(qid, f"papers:{name}")
                   for qid, name in (("papers:a::claim_1", "a"), ("papers:b::claim_1", "b"),
                                     ("papers:c::claim_1", "c"))}
        item = {"candidate_id": "cand_1",
                "evidence_claim_ids": ["papers:a::claim_1", "papers:c::claim_1"][:evidence_count],
                "target_claim_id": ["papers:b::claim_1"], "reasoning_type": "analogy",
                "expression": "[papers:a::claim_1] 类比 [papers:b::claim_1]"}
        _, weakpoints, _ = _judge(records, [_pair(records, "papers:a::claim_1", "papers:b::claim_1")], [item])
        return weakpoints

    def test_single_evidence_analogy_is_dropped(self):
        self.assertEqual(self._run(1), [])

    def test_two_evidence_analogy_is_kept(self):
        self.assertEqual(len(self._run(2)), 1)


class AbductionShapeTests(unittest.TestCase):
    """Abduction needs an observation premise and a non-observation hypothesis."""

    RECORDS = {qid: _record(qid, f"papers:{name}") for qid, name in (
        ("papers:a::claim_O01", "a"), ("papers:a::claim_1", "a"),
        ("papers:b::claim_O02", "b"), ("papers:b::claim_2", "b"),
    )}

    def _run(self, evidence, target):
        item = {"candidate_id": "cand_1",
                "evidence_claim_ids": evidence, "target_claim_id": [target],
                "reasoning_type": "abduction", "expression": f"[{evidence[0]}] 解释 [{target}]"}
        _, weakpoints, _ = _judge(self.RECORDS, [_pair(self.RECORDS, "papers:a::claim_O01", "papers:b::claim_2")], [item])
        return weakpoints

    def test_observation_explained_by_hypothesis_is_kept(self):
        self.assertEqual(len(self._run(["papers:a::claim_O01"], "papers:b::claim_2")), 1)

    def test_non_observation_premise_is_dropped(self):
        self.assertEqual(self._run(["papers:a::claim_1"], "papers:b::claim_2"), [])

    def test_observation_as_hypothesis_is_dropped(self):
        self.assertEqual(self._run(["papers:a::claim_O01"], "papers:b::claim_O02"), [])

    def test_multi_observation_premise_is_dropped(self):
        # The official compiler accepts exactly one observation premise.
        self.assertEqual(
            self._run(["papers:a::claim_O01", "papers:b::claim_O02"], "papers:b::claim_2"), [])

    def test_item_without_relation_field_is_judged_by_reasoning_type(self):
        # There is no `relation` layer: the doc's four reasoning types decide.
        item = {"candidate_id": "cand_1", "evidence_claim_ids": ["papers:a::claim_O01"],
                "target_claim_id": ["papers:b::claim_2"], "reasoning_type": "abduction",
                "expression": "[papers:a::claim_O01] 解释 [papers:b::claim_2]"}
        _, weakpoints, _ = _judge(
            self.RECORDS, [_pair(self.RECORDS, "papers:a::claim_O01", "papers:b::claim_2")], [item])
        self.assertEqual(len(weakpoints), 1)


class AbductionSummaryTests(unittest.TestCase):
    """A hypothesis with many observations is compressed into one summarized premise."""

    RECORDS = {qid: _record(qid, f"papers:{name}") for qid, name in (
        ("papers:a::claim_O01", "a"), ("papers:a::claim_O02", "a"), ("papers:a::claim_O03", "a"),
        ("papers:b::claim_1", "b"), ("papers:b::claim_2", "b"),
    )}

    @staticmethod
    def _abduction(evidence, target):
        return {"id": f"wp_{target}_{evidence[0]}", "payload": {
            "evidence_claim_ids": evidence, "target_claim_id": [target],
            "reasoning_type": "abduction", "evidence_anchor_ids": [], "expression": "x"}}

    def _run(self, weakpoints, summary="Sparsity degrades behaviour past a setting-dependent boundary."):
        with patch("pipeline_merge.step3._post_json", return_value={"summary": summary}):
            return _summarize_abduction_stars(weakpoints, self.RECORDS, max_workers=1)

    def _star(self):
        return [
            self._abduction(["papers:a::claim_O01"], "papers:b::claim_1"),
            self._abduction(["papers:a::claim_O02"], "papers:b::claim_1"),
            self._abduction(["papers:a::claim_O03"], "papers:b::claim_1"),
        ]

    def test_star_is_compressed_into_one_summarized_premise(self):
        weakpoints, candidates, stats = self._run(self._star())
        self.assertEqual(len(candidates), 1)
        summary_id = candidates[0]["id"]
        self.assertTrue(summary_id.startswith("candidate_A_"))
        abductions = [w for w in weakpoints if w["payload"]["reasoning_type"] == "abduction"]
        self.assertEqual(len(abductions), 1)
        # One observation premise: the summarized claim, not the raw observations.
        self.assertEqual(abductions[0]["payload"]["evidence_claim_ids"], [summary_id])
        self.assertEqual(abductions[0]["payload"]["target_claim_id"], ["papers:b::claim_1"])
        # Every source observation stays reachable through the bounded summary.
        summaries = [w for w in weakpoints if w["payload"]["reasoning_type"] == "deduction"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["payload"]["target_claim_id"], [summary_id])
        self.assertEqual(summaries[0]["payload"]["evidence_claim_ids"],
                         ["papers:a::claim_O01", "papers:a::claim_O02", "papers:a::claim_O03"])
        self.assertEqual(stats["summarized_count"], 1)

    def test_null_summary_falls_back_to_a_single_observation(self):
        weakpoints, candidates, _ = self._run(self._star(), summary="null")
        self.assertEqual(candidates, [])
        abductions = [w for w in weakpoints if w["payload"]["reasoning_type"] == "abduction"]
        self.assertEqual(len(abductions), 1)

    def test_concatenated_summary_is_rejected(self):
        glued = ("Sparsity hurts accuracy, and fine-grained pruning is worse than global pruning, "
                 "while high sparsity yields no ticket-like subnetworks.")
        weakpoints, candidates, stats = self._run(self._star(), summary=glued)
        self.assertEqual(candidates, [])
        self.assertEqual(len(stats["rejected_summaries"]), 1)
        self.assertEqual(len([w for w in weakpoints if w["payload"]["reasoning_type"] == "abduction"]), 1)

    def test_targets_sharing_observations_share_one_summary(self):
        # The same two observations are offered as evidence for two hypotheses.
        weakpoints, candidates, stats = self._run([
            self._abduction(["papers:a::claim_O01"], "papers:b::claim_1"),
            self._abduction(["papers:a::claim_O02"], "papers:b::claim_1"),
            self._abduction(["papers:a::claim_O01"], "papers:b::claim_2"),
            self._abduction(["papers:a::claim_O02"], "papers:b::claim_2"),
        ])
        # One A per observation set, summarized once and shared.
        self.assertEqual(len(candidates), 1)
        self.assertEqual(stats["summarized_count"], 1)
        self.assertEqual(len([w for w in weakpoints if w["payload"]["reasoning_type"] == "deduction"]), 1)
        self.assertEqual(len([w for w in weakpoints if w["payload"]["reasoning_type"] == "abduction"]), 2)
        self.assertEqual(len({c["id"] for c in candidates}), len(candidates))

    def test_single_observation_target_needs_no_summary(self):
        weakpoints, candidates, _ = self._run([self._abduction(["papers:a::claim_O01"], "papers:b::claim_2")])
        self.assertEqual(candidates, [])
        self.assertEqual(len(weakpoints), 1)

    def test_distinct_targets_stay_separate(self):
        weakpoints, candidates, _ = self._run([
            self._abduction(["papers:a::claim_O01"], "papers:b::claim_1"),
            self._abduction(["papers:a::claim_O02"], "papers:b::claim_2"),
        ])
        self.assertEqual(candidates, [])
        self.assertEqual(len([w for w in weakpoints if w["payload"]["reasoning_type"] == "abduction"]), 2)

    def test_non_abduction_weakpoints_pass_through(self):
        deduction = {"id": "wp_d", "payload": {"evidence_claim_ids": ["papers:a::claim_O01"],
                                               "target_claim_id": ["papers:b::claim_1"],
                                               "reasoning_type": "deduction", "evidence_anchor_ids": [],
                                               "expression": "x"}}
        weakpoints, candidates, _ = self._run([deduction])
        self.assertEqual(weakpoints, [deduction])
        self.assertEqual(candidates, [])


class SummaryQualityTests(unittest.TestCase):
    """A summary must be an abstraction, never the observations glued together."""

    OBSERVATIONS = [
        "On CIFAR-10 with ResNet-18, accuracy drops sharply above 90% sparsity.",
        "On ImageNet-10 with ResNet-10, fine-grained pruning degrades faster than global pruning.",
    ]

    def test_abstraction_is_accepted(self):
        self.assertTrue(_is_summary_acceptable(
            "Across the evaluated pruning settings, degradation is governed by pruning granularity.",
            self.OBSERVATIONS))

    def test_clause_chained_summary_is_rejected(self):
        self.assertFalse(_is_summary_acceptable(
            "Accuracy drops above 90% sparsity, while fine-grained pruning degrades faster than global pruning.",
            self.OBSERVATIONS))

    def test_too_long_summary_is_rejected(self):
        self.assertFalse(_is_summary_acceptable(" ".join(["sparsity"] * 31), self.OBSERVATIONS))

    def test_verbatim_copy_is_rejected(self):
        self.assertFalse(_is_summary_acceptable(self.OBSERVATIONS[0], self.OBSERVATIONS))

    def test_study_specific_number_is_rejected(self):
        self.assertFalse(_is_summary_acceptable(
            "Across settings, accuracy degrades sharply above 90% sparsity.", self.OBSERVATIONS))


if __name__ == "__main__":
    unittest.main()

class ConclusionGroupTests(unittest.TestCase):
    """Level-1 grouping follows the relation graph; every level stays bounded."""

    def test_relation_components_become_groups(self):
        adjacency = {"a": {"b"}, "b": {"a"}, "c": {"d"}, "d": {"c"}}
        self.assertEqual(_conclusion_groups(["a", "b", "c", "d"], adjacency), [["a", "b"], ["c", "d"]])

    def test_oversized_component_is_chunked_deterministically(self):
        members = [f"m{index:02d}" for index in range(10)]
        adjacency = {member: set(members) - {member} for member in members}
        groups = _conclusion_groups(members, adjacency)
        self.assertEqual([len(group) for group in groups], [CONCLUSION_CAP, 2])
        self.assertEqual(groups[0], members[:CONCLUSION_CAP])

    def test_higher_levels_chunk_without_adjacency(self):
        members = [f"k{index}" for index in range(5)]
        self.assertEqual(_conclusion_groups(members, {}), [members])


class ConclusionTreeTests(unittest.TestCase):
    """Doc §3.3 applied recursively: premises -> K1..Kn -> root."""

    RECORDS = {qid: _record(qid, f"papers:{name}") for qid, name in (
        ("papers:a::claim_1", "a"), ("papers:b::claim_1", "b"),
        ("papers:a::claim_2", "a"), ("papers:b::claim_2", "b"),
        ("papers:a::claim_3", "a"), ("papers:b::claim_3", "b"),
    )}

    @staticmethod
    def _link(left, right):
        return {"id": f"wp_{left}_{right}", "payload": {
            "evidence_claim_ids": [left], "target_claim_id": [right],
            "reasoning_type": "abduction", "evidence_anchor_ids": [], "expression": "x"}}

    SUMMARIES = (
        "Across the evaluated settings, degradation follows pruning granularity.",
        "Retained performance tracks structural specialization rather than budget.",
        "Sparsity effects are conditioned on the transfer setting under study.",
        "Compression outcomes are governed by how the retained structure is chosen.",
    )

    def _build(self):
        weakpoints = [self._link("papers:a::claim_1", "papers:b::claim_1"),
                      self._link("papers:a::claim_2", "papers:b::claim_2"),
                      self._link("papers:a::claim_3", "papers:b::claim_3")]
        calls = iter(self.SUMMARIES)
        with patch("pipeline_merge.step3._post_json", side_effect=lambda prompt: {"summary": next(calls)}):
            return _synthesize_conclusion_tree(weakpoints, self.RECORDS)

    def test_two_levels_are_built_up_to_a_single_root(self):
        candidates, weakpoints, stats = self._build()
        # Three linked pairs -> three level-1 conclusions -> one root.
        self.assertEqual([c["level"] for c in candidates].count(1), 3)
        self.assertEqual([c["level"] for c in candidates].count(2), 1)
        self.assertEqual(stats["conclusion_levels"], 2)
        self.assertEqual(stats["conclusion_count"], 4)

    def test_premises_point_at_the_conclusion(self):
        candidates, weakpoints, _ = self._build()
        root = [c for c in candidates if c["level"] == 2][0]["id"]
        rooted = [w for w in weakpoints if w["payload"]["target_claim_id"] == [root]]
        self.assertEqual(len(rooted), 1)
        # The root summarizes the three level-1 conclusions, not raw paper claims.
        self.assertEqual(len(rooted[0]["payload"]["evidence_claim_ids"]), 3)
        self.assertTrue(all(qid.startswith("candidate_K_") for qid in rooted[0]["payload"]["evidence_claim_ids"]))
        self.assertEqual(rooted[0]["payload"]["reasoning_type"], "deduction")

    def test_level_one_groups_never_exceed_the_cap(self):
        candidates, weakpoints, _ = self._build()
        level_one = [w for w in weakpoints if w["payload"]["target_claim_id"][0] in
                     {c["id"] for c in candidates if c["level"] == 1}]
        self.assertTrue(all(len(w["payload"]["evidence_claim_ids"]) <= CONCLUSION_CAP for w in level_one))

    def test_node_ids_are_deterministic(self):
        first, _, _ = self._build()
        second, _, _ = self._build()
        self.assertEqual([c["id"] for c in first], [c["id"] for c in second])


class ConclusionTreeGuardTests(unittest.TestCase):
    """Single-premise groups are not summaries; failed groups are retried upward."""

    RECORDS = {qid: _record(qid, f"papers:{name}") for qid, name in (
        ("papers:a::claim_1", "a"), ("papers:b::claim_1", "b"),
        ("papers:a::claim_2", "a"), ("papers:b::claim_2", "b"),
        ("papers:a::claim_3", "a"), ("papers:b::claim_3", "b"),
    )}

    @staticmethod
    def _link(left, right):
        return {"id": f"wp_{left}_{right}", "payload": {
            "evidence_claim_ids": [left], "target_claim_id": [right],
            "reasoning_type": "abduction", "evidence_anchor_ids": [], "expression": "x"}}

    def test_lone_claim_is_not_summarized_into_a_conclusion(self):
        # a3 is linked only to b3, but a lone claim appears when its partner is a
        # candidate_A premise filtered out of records.
        weakpoints = [self._link("papers:a::claim_3", "papers:b::claim_3"),
                      {"id": "wp_solo", "payload": {"evidence_claim_ids": ["papers:a::claim_1"],
                                                    "target_claim_id": ["papers:b::claim_1"],
                                                    "reasoning_type": "abduction",
                                                    "evidence_anchor_ids": [], "expression": "x"}}]
        calls = iter(["Alpha regularity across the evaluated settings.",
                      "Beta regularity across the evaluated settings.",
                      "Gamma regularity across the evaluated settings."])
        with patch("pipeline_merge.step3._post_json", side_effect=lambda prompt: {"summary": next(calls)}):
            candidates, weakpoints_out, stats = _synthesize_conclusion_tree(weakpoints, self.RECORDS)
        # Every conclusion has at least two premises.
        self.assertTrue(all(len(c["provenance_qids"]) >= 2 for c in candidates))
        self.assertNotIn(1, [len(c["provenance_qids"]) for c in candidates])

    def test_failed_group_is_promoted_rather_than_dropped(self):
        weakpoints = [self._link("papers:a::claim_1", "papers:b::claim_1"),
                      self._link("papers:a::claim_2", "papers:b::claim_2"),
                      self._link("papers:a::claim_3", "papers:b::claim_3")]
        responses = iter([{"summary": None},
                          {"summary": "Alpha regularity across the evaluated settings."},
                          {"summary": "Beta regularity across the evaluated settings."},
                          {"summary": "Gamma regularity across the evaluated settings."}])
        with patch("pipeline_merge.step3._post_json", side_effect=lambda prompt: next(responses)):
            candidates, weakpoints_out, stats = _synthesize_conclusion_tree(weakpoints, self.RECORDS)
        self.assertGreaterEqual(stats["conclusion_count"], 2)
        self.assertIn("conclusion_orphans", stats)
