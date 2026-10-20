from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_pipeline_v2.authoring import (
    canonical_strategy,
    content_hash,
    emit_formalization,
    validate,
)
from agent_pipeline_v2.compiler_projection import project_for_official_compiler
from agent_pipeline_v2.step1 import PAPER_TEXT_KIND
from agent_pipeline_v2.step4 import (
    WeakpointExpansionTool,
    _clean_additions,
    _clean_proposition,
    _prune_orphan_observations,
    _shorten_addition_ids,
    _validate_expansion,
)
from pipeline_harness.domain.stages import _best_paragraph_anchor, _paper_paragraphs
from pipeline_harness.domain.tools import ToolCallRequest, ToolCallResponse
from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json, read_json
from pipeline_harness.view.projector import project_run


class Classifier:
    name = "test-classifier"
    version = "1"
    kind: str | None = "deduction"

    def invoke(self, request: ToolCallRequest) -> ToolCallResponse:
        if request.operation == "classify_weakpoints":
            return ToolCallResponse(
                request.call_id, "succeeded", {},
                {"classifications": [
                    {"weakpoint_id": item["weakpoint_id"],
                     "reasoning_type": (
                         None if any(target["claim_id"] == "claim_5" for target in item["target_claims"])
                         else self.kind
                     )}
                    for item in request.parameters["weakpoints"]
                ]},
            )
        clusters = request.parameters["clusters"]
        normalized = []
        for cluster in clusters:
            weakpoints = []
            for relation in cluster["candidate_relations"]:
                endpoints = [*relation["sources"], relation["target"]]
                weakpoints.append({
                    "member_relation_ids": [relation["relation_id"]],
                    "evidence_claim_ids": list(relation["sources"]),
                    "target_claim_id": [relation["target"]],
                    "reasoning_type": self.kind if relation["target"] != "claim_5" else None,
                    "expression": " 推出 ".join(f"[{key}]" for key in endpoints),
                })
            normalized.append({"cluster_id": cluster["cluster_id"], "weakpoints": weakpoints, "rejected_relation_ids": []})
        return ToolCallResponse(request.call_id, "succeeded", {}, {"clusters": normalized})


class CleanerEmptyInstanceTests(unittest.TestCase):
    def test_instance_locator_writes_empty_list_when_no_instance_exists(self) -> None:
        from agent_pipeline_v2.claim_cleaner.step3_instance_extraction import step3a_locate_instance_targets as step3a

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            paper_dir = data_dir / "paper_without_instances"
            paper_dir.mkdir()
            atomic_write_json(paper_dir / "organized_content.json", [{
                "id": "paper:test::conclusion_1",
                "content": "A self-contained proposition.",
                "organized_parts": [{
                    "number": 1,
                    "label": "assertion",
                    "content": "A self-contained proposition.",
                    "source_spans": [{"start": 0, "end": 29}],
                }],
            }])
            with patch.object(step3a, "DATA_DIR", data_dir), patch(
                "sys.argv", ["step3a_locate_instance_targets.py", "paper_without_instances"]
            ):
                step3a.main()
            self.assertEqual(
                [],
                json.loads(
                    (paper_dir / "instance_target_positions.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )


class AllClassifier(Classifier):
    def invoke(self, request: ToolCallRequest) -> ToolCallResponse:
        response = super().invoke(request)
        if request.operation == "normalize_weakpoint_clusters":
            for cluster in response.normalized["clusters"]:
                for item in cluster["weakpoints"]:
                    item["reasoning_type"] = self.kind
        else:
            for item in response.normalized["classifications"]:
                item["reasoning_type"] = self.kind
        return response


class ParallelExpansionTool:
    name = "parallel-expansion"
    version = "1"
    barrier = threading.Barrier(2)
    thread_ids: set[int] = set()
    knowledge_sets: list[set[str]] = []

    def invoke(self, request: ToolCallRequest) -> ToolCallResponse:
        type(self).thread_ids.add(threading.get_ident())
        type(self).knowledge_sets.append(set(request.parameters["knowledges"]))
        type(self).barrier.wait(timeout=5)
        payload = request.parameters["weakpoint"]["payload"]
        target = payload["target_claim_id"][0]
        return ToolCallResponse(request.call_id, "succeeded", {}, {
            "knowledges": {},
            "strategies": [strategy("deduction", list(payload["evidence_claim_ids"]), target)],
        })


def knowledge(text: str | None, kind: str = "claim") -> dict:
    return {"type": kind, "content": {"canonical": text} if text is not None else None,
            "source_anchor_ids": ["anchor_paragraph_rule"] if text is not None else []}


def strategy(kind: str, variables: list[str], conclusion: str, background: list[str] | None = None) -> dict:
    return {"scope": "local", "type": kind, "premises": variables, "conclusion": conclusion, "background": background or []}


def deduction() -> dict:
    return {"knowledges": {"M": knowledge("Both premises hold in the stated domain.")}, "strategies": [
        strategy("deduction", ["claim_1", "claim_2"], "M"),
        strategy("deduction", ["M"], "claim_3", ["note_1"]),
    ]}


def abduction() -> dict:
    return {"knowledges": {"A": knowledge("The two observations jointly support the hypothesis.")}, "strategies": [
        strategy("deduction", ["claim_1", "claim_2"], "A"),
        strategy("abduction", ["A"], "claim_3", ["note_1"]),
    ]}


class AnchorPaperPlugin:
    """Mirror Step 2's mechanical anchoring so Step 3's screening gate can run.

    The real pipeline gives every Knowledge a ``source.paper_text`` anchor in
    Step 2; the unit fixtures below skip Step 2 to avoid LLM calls, so without
    this stage Step 3's paper-anchor screening rejects every non-null weakpoint.
    It also drops the ``claims_final`` high-confidence marker so these Step 4
    tests keep exercising Step 3's typed cluster/LLM classification instead of
    the direct claims_final adoption path.
    """

    def run(self, context):
        paper_ref = context.require_one(PAPER_TEXT_KIND)
        paper_text = context.artifact_path(paper_ref).read_text(encoding="utf-8-sig")
        paragraphs = _paper_paragraphs(paper_text.splitlines())
        reference = context.latest("formalization")
        document = json.loads(context.artifact_path(reference).read_text(encoding="utf-8"))
        for knowledge in document["knowledges"].values():
            content = knowledge.get("content")
            if not isinstance(content, dict) or not isinstance(content.get("canonical"), str):
                continue
            best = _best_paragraph_anchor(content["canonical"], paragraphs)
            if best is None:
                continue
            source_ids = knowledge.setdefault("source_anchor_ids", [])
            if best["anchor_id"] not in source_ids:
                source_ids.append(best["anchor_id"])
        for link in document["workflow"]["non_reasoning_links"]:
            metadata = link.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("source", None)
        prior = document["revision"]
        document["revision"] = {
            "revision_id": f"revision_{context.run_id}_step_2",
            "supersedes": prior.get("revision_id"),
            "parent_hash": prior.get("content_hash"),
            "content_hash": "",
        }
        return emit_formalization(context, document, step=2, step_name="anchor_paper")


class AdditionIdShorteningTests(unittest.TestCase):
    def test_strips_only_the_pipeline_bookkeeping_prefix(self) -> None:
        parameters = {"knowledges": {"claim_1": {"type": "claim"}}}
        result = {
            "knowledges": {
                "claim_step4_weakpoint_relation_step2_11_claim_summary_evidence_for_7_cleaned_1": {
                    "type": "claim", "content": {"canonical": "text"}, "source_anchor_ids": ["anchor_paragraph_p1"],
                },
                "claim_step2_11_alternative": {
                    "type": "claim", "content": {"canonical": "alt"}, "source_anchor_ids": ["anchor_paragraph_p2"],
                },
            },
            "strategies": [{
                "scope": "local", "type": "deduction",
                "premises": ["claim_step4_weakpoint_relation_step2_11_claim_summary_evidence_for_7_cleaned_1"],
                "conclusion": "claim_step2_11_alternative",
                "background": [],
            }],
        }
        renames = _shorten_addition_ids(parameters, result)
        self.assertEqual(
            {
                "claim_step4_weakpoint_relation_step2_11_claim_summary_evidence_for_7_cleaned_1":
                    "step2_11_claim_summary_evidence_for_7_cleaned_1",
            },
            renames,
        )
        self.assertEqual(
            {"step2_11_claim_summary_evidence_for_7_cleaned_1", "claim_step2_11_alternative"},
            set(result["knowledges"]),
        )
        strategy = result["strategies"][0]
        self.assertEqual(["step2_11_claim_summary_evidence_for_7_cleaned_1"], strategy["premises"])
        self.assertEqual("claim_step2_11_alternative", strategy["conclusion"])

    def test_never_rewrites_existing_or_unnamed_claim_ids(self) -> None:
        parameters = {"knowledges": {"claim_21": {"type": "claim"}, "claim_O03": {"type": "observation_claim"}}}
        result = {
            "knowledges": {
                "claim_21": {"type": "claim", "content": {"canonical": "x"}, "source_anchor_ids": ["a"]},
                "hypothesis_bridge": {"type": "claim", "content": {"canonical": "y"}, "source_anchor_ids": ["b"]},
            },
            "strategies": [],
        }
        self.assertEqual({}, _shorten_addition_ids(parameters, result))
        self.assertEqual({"claim_21", "hypothesis_bridge"}, set(result["knowledges"]))

    def test_collision_with_existing_knowledge_is_disambiguated(self) -> None:
        original = "claim_step4_weakpoint_relation_step2_11_rule"
        parameters = {"knowledges": {"step2_11_rule": {"type": "claim"}}}
        result = {
            "knowledges": {original: {"type": "claim", "content": {"canonical": "z"}, "source_anchor_ids": ["a"]}},
            "strategies": [],
        }
        renames = _shorten_addition_ids(parameters, result)
        short = renames[original]
        self.assertNotEqual("step2_11_rule", short)
        self.assertTrue(short.startswith("step2_11_rule_"))
        self.assertEqual({short}, set(result["knowledges"]))


class Step4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "paper.md").write_text(
            "[#rule] Under the stated conditions the two premises jointly imply the conclusion. "
            "A source-domain law, its variable mapping and target boundary conditions are specified here. "
            "The hypothesis or another sufficient cause accounts for each observation in the stated domain.\n",
            encoding="utf-8",
        )
        atomic_write_json(self.root / "claims.json", {
            "claim": [{"number": number, "conclusion": "c", "text": text} for number, text in enumerate([
                "First premise or observation.", "Second premise or observation.", "Target hypothesis or conclusion.",
                "Existing source law.", "Existing bridge.", "Existing alternative mechanism.",
            ], 1)],
            "note": [{"number": 1, "conclusion": "c", "text": "The stated rule applies under the specified conditions."}],
            "relation": [
                {"connects": [1, 2, 3], "expression": "([1] 和 [2]) 推出 [3]"},
                {"connects": [4, 5], "expression": "[4] 推出 [5]"},
            ],
        })
        atomic_write_json(self.root / "manifest.json", {"artifacts": [
            {"path": "paper.md", "kind": "source.paper_text", "media_type": "text/markdown"},
            {"path": "claims.json", "kind": "source.claims_final", "media_type": "application/json"},
        ]})
        Classifier.kind = "deduction"
        self.pipeline = {
            "pipeline_id": "step4-test", "version": "1", "view_adapter": "agent_pipeline_v2.view:V2FormalizationViewAdapter",
            "stages": [
                {"name": "import", "plugin": "agent_pipeline_v2.step1:ClaimsFinalInputImporter", "options": {}},
                {"name": "step1", "plugin": "agent_pipeline_v2.step1:Step1ImportClaimsFinalPlugin", "options": {}},
                {"name": "anchor_paper", "plugin": f"{__name__}:AnchorPaperPlugin", "options": {}},
                {"name": "step3", "plugin": "agent_pipeline_v2.step3:Step3AnalyzeReasoningPlugin", "options": {"tool_plugin": f"{__name__}:Classifier"}},
                {"name": "step4_formalize_reasoning", "plugin": "agent_pipeline_v2.step4:Step4FormalizeReasoningPlugin", "options": {}},
            ],
        }

    @staticmethod
    def cleaned(text: str) -> tuple[dict, dict]:
        return {"claim": [{"number": 1, "text": text + " (cleaned)", "needs_more_context": [], "is_pure_data": False}],
                "note": [], "relation": []}, {"path": "/test/claims_final.json", "sha256": "a" * 64}

    def run_expansion(self, expansion: dict, *, cleaner=None) -> tuple[RunStore, dict, dict | None]:
        store = RunStore.create(self.root / "runs", self.pipeline, input_manifest=self.root / "manifest.json")
        self.http_calls = []

        def http(request, **kwargs):
            payload = json.loads(request.data)
            self.http_calls.append(payload)
            # The post-cleaning Group rebuild must re-derive strategies only and
            # may not reintroduce knowledge the cleaner already persisted.
            if "post-cleaning Group rebuild" in payload["messages"][-1]["content"]:
                body = {"knowledges": {}, "strategies": expansion.get("strategies", [])}
            else:
                body = expansion
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(body)}}]}).encode())

        with patch("agent_pipeline_v2.step4.urlopen", side_effect=http), \
                patch("agent_pipeline_v2.step4._load_deepseek_env"), \
                patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), \
                patch("agent_pipeline_v2.step4._clean_proposition", side_effect=cleaner or self.cleaned) as cleaning:
            self.result = run_pipeline(store.run_dir)
            self.cleaning_calls = cleaning.call_args_list
        documents = {int(ref.metadata["step"]): read_json(store.artifact_path(ref))
                     for ref in store.load_artifacts() if ref.kind == "formalization"}
        return store, documents[3], documents.get(4)

    @staticmethod
    def parameters(document: dict, kind: str = "deduction") -> dict:
        weakpoint = copy.deepcopy(document["workflow"]["weakpoints"][0])
        weakpoint["payload"]["reasoning_type"] = kind
        return {"weakpoint": weakpoint, "knowledges": document["knowledges"],
                "source_excerpts": [{"anchor_id": "anchor_paragraph_rule", "text": "Source text."}]}

    def test_deduction_cleans_new_claim_reuses_note_and_maps_null_to_infer(self) -> None:
        store, prior, final = self.run_expansion(deduction())
        self.assertEqual([], final["workflow"]["revisions"])
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual(2, len(self.http_calls))
        self.assertEqual(1, len(self.cleaning_calls))
        self.assertEqual([], final["workflow"]["weakpoints"])
        self.assertEqual(prior["knowledges"], {key: final["knowledges"][key] for key in prior["knowledges"]})
        new_id = next(key for key in final["knowledges"] if key not in prior["knowledges"])
        self.assertTrue(final["knowledges"][new_id]["content"]["canonical"].endswith("(cleaned)"))
        self.assertEqual(["infer", "deduction", "deduction"], [item["type"] for item in final["graph"]["strategies"]])
        self.assertEqual([new_id], final["graph"]["strategies"][-1]["premises"])
        self.assertEqual(["note_1"], final["graph"]["strategies"][-1]["background"])
        self.assertNotIn("note_1", final["graph"]["nodes"])
        self.assertEqual(prior["revision"]["content_hash"], final["revision"]["parent_hash"])
        validate(final)
        view = project_run(store.run_dir)
        self.assertEqual(4, view.metadata["latest_step"])
        self.assertEqual(prior["graph"]["operators"], final["graph"]["operators"])
        projected = project_for_official_compiler(final)
        self.assertEqual(final["graph"]["strategies"], projected["graph"]["strategies"])
        self.assertIn("note_1", {item["id"] for item in projected["graph"]["knowledges"]})
        visible_strategies = [node for node in view.nodes if node["step"] == 4 and node["kind"] == "strategy"]
        self.assertEqual(["infer"], [node["details"]["type"] for node in visible_strategies])
        self.assertTrue(any(node["kind"] == "operator" and node.get("fold_group")
                            for node in view.nodes if node["step"] == 4))

    def test_observation_edge_is_allowed_once_e_layer_is_removed(self) -> None:
        parameters = {
            "weakpoint": {
                "payload": {
                    "reasoning_type": "abduction",
                    "evidence_claim_ids": ["claim_O01"],
                    "target_claim_id": ["claim_A"],
                },
            },
            "knowledges": {
                "claim_O01": knowledge("Measured result.", "observation_claim"),
                "claim_A": knowledge("General hypothesis."),
            },
            "source_excerpts": [],
        }
        expansion = {
            "knowledges": {},
            "strategies": [strategy("abduction", ["claim_O01"], "claim_A")],
        }
        # O is now the observation proposition itself, so it may connect
        # directly to a hypothesis through a named strategy.
        _validate_expansion(parameters, expansion)

    def test_all_null_weakpoints_become_infer_without_tools_new_facts_or_probabilities(self) -> None:
        Classifier.kind = None
        self.pipeline["stages"][-1]["options"]["tool_plugin"] = "missing_plugin:MustNotBeLoaded"
        _, prior, final = self.run_expansion({})
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual([], self.http_calls)
        self.assertEqual([], self.cleaning_calls)
        self.assertEqual(prior["knowledges"], final["knowledges"])
        for field in ("nodes", "operators", "composes"):
            self.assertEqual(prior["graph"][field], final["graph"][field])
        self.assertEqual([], final["workflow"]["weakpoints"])
        self.assertEqual(2, len(final["graph"]["strategies"]))
        for weakpoint, item in zip(prior["workflow"]["weakpoints"], final["graph"]["strategies"]):
            self.assertEqual("infer", item["type"])
            self.assertEqual(weakpoint["payload"]["evidence_claim_ids"], item["premises"])
            self.assertEqual(weakpoint["payload"]["target_claim_id"][0], item["conclusion"])
            self.assertEqual([], item["background"])
            self.assertNotIn("conditional_probabilities", item)

    def test_multi_target_weakpoint_requires_one_terminal_strategy_per_target(self) -> None:
        _, prior, _ = self.run_expansion(deduction())
        parameters = self.parameters(prior)
        parameters["weakpoint"]["payload"]["target_claim_id"] = ["claim_3", "claim_5"]
        expansion = {"knowledges": {}, "strategies": [
            strategy("deduction", ["claim_1", "claim_2"], "claim_3", ["note_1"]),
            strategy("deduction", ["claim_1", "claim_2"], "claim_5", ["note_1"]),
        ]}
        _validate_expansion(parameters, expansion)
        expansion["strategies"].pop()
        with self.assertRaisesRegex(ValueError, "every target"):
            _validate_expansion(parameters, expansion)

    def test_abduction_without_grounded_alternative_keeps_evidence_only_strategy(self) -> None:
        Classifier.kind = "abduction"
        store, prior, final = self.run_expansion(abduction())
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual(1, len(self.cleaning_calls))
        self.assertFalse(any(item["content"] is None for item in final["knowledges"].values()))
        abductions = [item for item in final["graph"]["strategies"] if item["type"] == "abduction"]
        self.assertEqual(1, len(abductions))
        summary_id = next(key for key in final["knowledges"] if key not in prior["knowledges"])
        self.assertEqual([summary_id], abductions[0]["premises"])
        self.assertEqual("claim_3", abductions[0]["conclusion"])
        self.assertEqual(prior["graph"]["operators"], final["graph"]["operators"])
        view = project_run(store.run_dir)
        alternatives = [node for node in view.nodes if node["step"] == 4 and node["kind"] == "alternative_placeholder"]
        self.assertEqual(1, len(alternatives))
        self.assertTrue(all(node["layer"] == "claims" for node in alternatives))
        abduction_operators = [node["details"] for node in view.nodes
                               if node["step"] == 4 and node["kind"] == "operator"
                               and node["details"]["metadata"].get("formalization_template") == "abduction"]
        self.assertTrue(abduction_operators)
        validate(final)

    def test_abduction_can_reuse_alternative_and_explicit_background_condition(self) -> None:
        Classifier.kind = "abduction"
        expansion = abduction()
        expansion["knowledges"] = {"condition": knowledge("The hypothesis explains each observation within the stated boundary.", "note")}
        for op in expansion["strategies"]:
            op["premises"].append("claim_6")
            op["background"].append("condition")
        _, _, final = self.run_expansion(expansion)
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertIsNotNone(final)

    def test_analogy_cleans_bridge_and_condition_but_note_stays_outside_graph(self) -> None:
        Classifier.kind = "analogy"
        expansion = deduction()
        expansion["knowledges"].update({
            "bridge": knowledge("The source and target variables correspond while preserving the stated constraint."),
            "condition": knowledge("The target is restricted to the stated parameter range.", "note"),
        })
        expansion["strategies"][1]["type"] = "analogy"
        expansion["strategies"][1]["premises"].append("bridge")
        expansion["strategies"][1]["background"].append("condition")
        _, _, final = self.run_expansion(expansion)
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual(3, len(self.cleaning_calls))
        condition_id = next(key for key in final["knowledges"] if key.endswith("_condition"))
        self.assertNotIn(condition_id, final["graph"]["nodes"])
        self.assertIn(condition_id, final["graph"]["strategies"][-1]["background"])

    def test_insufficient_evidence_lowers_classified_weakpoint_to_infer(self) -> None:
        _, prior, final = self.run_expansion({"knowledges": {}, "strategies": []})
        self.assertEqual("succeeded", self.result.status)
        # A typed weakpoint whose strict family cannot be justified is kept as
        # a soft infer edge instead of disappearing from the graph.
        self.assertEqual([], final["workflow"]["weakpoints"])
        self.assertTrue(final["graph"]["strategies"])
        self.assertTrue(all(item["type"] == "infer" for item in final["graph"]["strategies"]))
        self.assertEqual(prior["graph"]["operators"], final["graph"]["operators"])
        self.assertEqual([], self.cleaning_calls)
        self.assertTrue(any(item["code"] == "STEP4_INSUFFICIENT_EVIDENCE" for item in self.result.findings))

    def test_orphan_observations_are_pruned_after_step4(self) -> None:
        document = {
            "knowledges": {
                "claim_1": {"type": "claim", "content": {"canonical": "linked"}, "source_anchor_ids": []},
                "claim_O1": {"type": "observation_claim", "content": {"canonical": "linked obs"}, "source_anchor_ids": []},
                "claim_O2": {"type": "observation_claim", "content": {"canonical": "orphan obs"}, "source_anchor_ids": []},
                "note_1": {"type": "note", "content": {"canonical": "condition"}, "source_anchor_ids": []},
            },
            "graph": {
                "nodes": ["claim_1", "claim_O1", "claim_O2"],
                "operators": [],
                "strategies": [{"scope": "local", "type": "infer", "premises": ["claim_O1"],
                                "conclusion": "claim_1", "background": ["note_1"]}],
            },
            "workflow": {"weakpoints": []},
        }
        self.assertEqual(["claim_O2"], _prune_orphan_observations(document))
        self.assertNotIn("claim_O2", document["knowledges"])
        self.assertNotIn("claim_O2", document["graph"]["nodes"])
        self.assertIn("claim_O1", document["graph"]["nodes"])
        # Plain claims and notes are never pruned.
        self.assertIn("claim_1", document["graph"]["nodes"])
        self.assertIn("note_1", document["knowledges"])

    def test_cleaning_failure_keeps_audit_and_does_not_publish_revision(self) -> None:
        def fail(text):
            raise RuntimeError("synthetic cleaning failure")
        store, _, final = self.run_expansion(deduction(), cleaner=fail)
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertIsNotNone(final)
        audits = [ref for ref in store.load_artifacts() if ref.kind == "tool.semantic_review.response" and ref.metadata["step"] == 4]
        self.assertEqual(1, len(audits))
        response = read_json(store.artifact_path(audits[0]))["response"]
        self.assertEqual("failed", response["status"])
        self.assertIn("synthetic cleaning failure", response["error"]["message"])

    def test_each_weakpoint_merges_independently(self) -> None:
        self.pipeline["stages"][3]["options"]["tool_plugin"] = f"{__name__}:AllClassifier"
        store, prior, final = self.run_expansion(deduction())
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertIsNotNone(final)
        self.assertEqual(8, len(prior["knowledges"]))
        self.assertEqual(9, len(final["knowledges"]))
        # The unexpandable second weakpoint is lowered to a soft infer edge.
        self.assertEqual([], final["workflow"]["weakpoints"])
        audits = [read_json(store.artifact_path(ref)) for ref in store.load_artifacts()
                  if ref.kind == "tool.semantic_review.response" and ref.metadata["step"] == 4]
        self.assertEqual(["succeeded", "succeeded"], [item["response"]["status"] for item in audits])
        self.assertTrue(all("prior_tool_call_ids" not in item["request"]["parameters"] for item in audits))
        self.assertEqual(1, len({item["request"]["parameters"]["formalization_ref"] for item in audits}))
        self.assertNotIn("source_excerpts", audits[0]["request"]["parameters"])
        self.assertNotIn("knowledges", audits[0]["request"]["parameters"])

    def test_classified_weakpoints_expand_in_parallel_from_one_frozen_step3(self) -> None:
        self.pipeline["stages"][3]["options"]["tool_plugin"] = f"{__name__}:AllClassifier"
        self.pipeline["stages"][4]["options"]["tool_plugin"] = f"{__name__}:ParallelExpansionTool"
        ParallelExpansionTool.barrier = threading.Barrier(2)
        ParallelExpansionTool.thread_ids = set()
        ParallelExpansionTool.knowledge_sets = []
        _, _, final = self.run_expansion({})
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertIsNotNone(final)
        self.assertEqual(2, len(ParallelExpansionTool.thread_ids))
        self.assertEqual(2, len(ParallelExpansionTool.knowledge_sets))
        self.assertEqual(ParallelExpansionTool.knowledge_sets[0], ParallelExpansionTool.knowledge_sets[1])
        self.assertEqual([], final["workflow"]["weakpoints"])

    def test_ambiguous_cleaning_cannot_create_nodes(self) -> None:
        def split(text):
            result, ref = self.cleaned(text)
            result["claim"].append({"number": 2, "text": "Another proposition."})
            return result, ref
        _, prior, final = self.run_expansion(deduction(), cleaner=split)
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual(prior["knowledges"], final["knowledges"])

    def test_split_background_note_retains_weakpoint_without_adding_a_relation(self) -> None:
        expansion = deduction()
        expansion["knowledges"]["condition"] = knowledge(
            "A compound applicability rule and its binding.", "note"
        )
        expansion["strategies"][-1]["background"].append("condition")

        def split_note(text):
            result, ref = self.cleaned(text)
            if text.startswith("A compound applicability"):
                result["claim"].append({
                    "number": 2,
                    "text": "A separately checkable binding.",
                    "needs_more_context": [],
                    "is_pure_data": False,
                })
            return result, ref

        _, prior, final = self.run_expansion(expansion, cleaner=split_note)
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual(prior["knowledges"], final["knowledges"])
        self.assertEqual([], final["workflow"]["weakpoints"])
        self.assertTrue(final["graph"]["strategies"])
        self.assertTrue(all(item["type"] == "infer" for item in final["graph"]["strategies"]))
        self.assertTrue(any(
            item["code"] == "STEP4_INSUFFICIENT_EVIDENCE"
            for item in self.result.findings
        ))

    def test_background_note_needing_context_retains_the_weakpoint(self) -> None:
        expansion = deduction()
        expansion["knowledges"]["condition"] = knowledge(
            "A rule using a paper-specific undefined term.", "note"
        )
        expansion["strategies"][-1]["background"].append("condition")

        def needs_context(text):
            result, ref = self.cleaned(text)
            if text.startswith("A rule using"):
                result["claim"][0]["needs_more_context"] = ["paper-specific term"]
            return result, ref

        _, prior, final = self.run_expansion(expansion, cleaner=needs_context)
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual(prior["knowledges"], final["knowledges"])
        self.assertEqual([], final["workflow"]["weakpoints"])
        self.assertTrue(any(
            item["code"] == "STEP4_INSUFFICIENT_EVIDENCE"
            for item in self.result.findings
        ))

    def test_claim_reclassified_as_note_retains_the_weakpoint(self) -> None:
        def as_note(text):
            del text
            return {
                "claim": [],
                "note": [{"number": 1, "text": "A reporting condition."}],
                "relation": [],
            }, {"path": "/test/claims_final.json", "sha256": "b" * 64}

        _, prior, final = self.run_expansion(deduction(), cleaner=as_note)
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual(prior["knowledges"], final["knowledges"])
        self.assertEqual([], final["workflow"]["weakpoints"])
        self.assertTrue(any(
            item["code"] == "STEP4_INSUFFICIENT_EVIDENCE"
            for item in self.result.findings
        ))

    def test_observation_to_general_deduction_fails_closed_as_insufficient(self) -> None:
        _, prior, _ = self.run_expansion(deduction())
        parameters = self.parameters(prior)
        parameters["knowledges"]["claim_1"]["type"] = "observation_claim"
        request = ToolCallRequest(
            "unsound-observation-deduction",
            WeakpointExpansionTool.name,
            WeakpointExpansionTool.version,
            "expand_weakpoint",
            [],
            parameters,
        )
        raw = {
            "choices": [{"message": {"content": json.dumps(deduction())}}]
        }
        with patch("agent_pipeline_v2.step4.urlopen", return_value=io.BytesIO(
            json.dumps(raw).encode("utf-8")
        )), patch("agent_pipeline_v2.step4._load_deepseek_env"), patch.dict(
            "os.environ", {"DEEPSEEK_API_KEY": "test-key"}
        ):
            response = WeakpointExpansionTool().invoke(request)
        # Observational support for a general claim cannot become a strict
        # implication, so the expansion degrades to an explicit empty result
        # (which the caller then lowers to a soft infer edge).
        self.assertEqual("succeeded", response.status)
        self.assertEqual({"knowledges": {}, "strategies": []}, response.normalized)

    def test_empty_model_content_retains_the_weakpoint_without_cleaning(self) -> None:
        _, prior, _ = self.run_expansion(deduction())
        request = ToolCallRequest(
            "empty-expansion",
            WeakpointExpansionTool.name,
            WeakpointExpansionTool.version,
            "expand_weakpoint",
            [],
            self.parameters(prior),
        )
        raw = {"choices": [{"message": {"content": "   "}}]}
        with patch("agent_pipeline_v2.step4.urlopen", return_value=io.BytesIO(
            json.dumps(raw).encode("utf-8")
        )), patch("agent_pipeline_v2.step4._load_deepseek_env"), patch.dict(
            "os.environ", {"DEEPSEEK_API_KEY": "test-key"}
        ), patch("agent_pipeline_v2.step4._clean_proposition") as cleaning:
            response = WeakpointExpansionTool().invoke(request)
        self.assertEqual("failed", response.status)
        self.assertIsNone(response.normalized)
        cleaning.assert_not_called()

    def test_cleaner_note_references_remain_resolvable_without_entering_graph(self) -> None:
        _, prior, _ = self.run_expansion(deduction())
        parameters = self.parameters(prior)

        def with_note(text):
            result, ref = self.cleaned(text)
            result["claim"][0]["text"] = "Both premises hold under note 1."
            result["note"] = [{"number": 1, "text": "The restricted source domain."}]
            return result, ref

        with patch("agent_pipeline_v2.step4._clean_proposition", side_effect=with_note):
            result = _clean_additions(parameters, deduction(), [])
        note_id = next(key for key in result["knowledges"] if key.endswith("_note_1"))
        self.assertEqual("note", result["knowledges"][note_id]["type"])
        self.assertIn(f"[{note_id}]", result["knowledges"]["M"]["content"]["canonical"])
        self.assertTrue(all(note_id in op["background"] for op in result["strategies"] if op["type"] == "deduction"))

    def test_rejects_unsound_shapes_cycles_and_unknown_references(self) -> None:
        _, prior, _ = self.run_expansion(deduction())
        parameters = self.parameters(prior)
        invalid = []
        wrong = deduction(); wrong["strategies"][-1]["premises"] = ["claim_3"]; invalid.append(wrong)
        wrong = deduction(); wrong["strategies"][0]["premises"] = ["claim_1", "absent"]; invalid.append(wrong)
        wrong = deduction(); wrong["strategies"][0]["premises"] = ["claim_1", "note_1"]; invalid.append(wrong)
        wrong = deduction(); wrong["strategies"][-1]["background"] = ["claim_4"]; invalid.append(wrong)
        wrong = deduction(); wrong["knowledges"]["M"] = knowledge(None); invalid.append(wrong)
        for result in invalid:
            with self.subTest(result=result), self.assertRaises(ValueError):
                _validate_expansion(parameters, result)
        parameters["knowledges"]["claim_1"]["type"] = "observation_claim"
        with self.assertRaisesRegex(ValueError, "observational support"):
            _validate_expansion(parameters, deduction())

    def test_abduction_rejects_reverse_implication_missing_pairs_or_unknown_alternatives(self) -> None:
        _, prior, _ = self.run_expansion(deduction())
        parameters = self.parameters(prior, "abduction")
        invalid = [deduction()]
        wrong = abduction(); wrong["strategies"].pop(); invalid.append(wrong)
        wrong = abduction(); wrong["strategies"][1]["premises"].append("absent"); invalid.append(wrong)
        wrong = abduction(); wrong["knowledges"]["unused"] = knowledge("An unused alternative cause."); invalid.append(wrong)
        wrong = abduction(); wrong["strategies"][0]["premises"] = ["claim_3"]; invalid.append(wrong)
        for result in invalid:
            with self.subTest(result=result), self.assertRaises(ValueError):
                _validate_expansion(parameters, result)

    def test_existing_knowledge_is_reused_without_cleaning_or_duplication(self) -> None:
        _, prior, _ = self.run_expansion(deduction())
        parameters = self.parameters(prior)
        result = deduction()
        result["knowledges"]["condition"] = copy.deepcopy(parameters["knowledges"]["note_1"])
        result["strategies"][-1]["background"] = ["condition"]
        with patch("agent_pipeline_v2.step4._clean_proposition", side_effect=self.cleaned) as cleaning:
            result = _clean_additions(parameters, result, [])
        self.assertEqual(1, cleaning.call_count)
        self.assertNotIn("condition", result["knowledges"])
        self.assertEqual(["note_1"], result["strategies"][-1]["background"])

    def test_cleaning_can_resolve_to_existing_note_and_rewrite_explicit_references(self) -> None:
        _, prior, _ = self.run_expansion(deduction())
        parameters = self.parameters(prior)
        result = deduction()
        result["knowledges"]["condition"] = knowledge("A reworded condition.", "note")
        result["strategies"][-1]["background"] = ["condition"]

        def clean(text):
            output, ref = self.cleaned(text)
            output["claim"][0]["text"] = (
                "M is the conjunction under [condition]." if "premises" in text
                else parameters["knowledges"]["note_1"]["content"]["canonical"]
            )
            return output, ref
        with patch("agent_pipeline_v2.step4._clean_proposition", side_effect=clean):
            result = _clean_additions(parameters, result, [])
        self.assertEqual("M is the conjunction under [note_1].", result["knowledges"]["M"]["content"]["canonical"])
        self.assertNotIn("condition", result["knowledges"])

    def test_validator_rejects_note_in_graph_and_misused_null_content(self) -> None:
        Classifier.kind = "abduction"
        _, _, final = self.run_expansion(abduction())
        final["graph"]["nodes"].append("note_1")
        final["revision"]["content_hash"] = content_hash(final)
        with self.assertRaisesRegex(ValueError, "formal claims"):
            validate(final)
        final["graph"]["nodes"].remove("note_1")
        final["knowledges"]["claim_3"]["content"] = None
        final["revision"]["content_hash"] = content_hash(final)
        with self.assertRaisesRegex(ValueError, "requires content.canonical"):
            validate(final)

    def test_validator_rejects_unreviewed_weakpoint_operator_and_revision_values(self) -> None:
        _, _, final = self.run_expansion(deduction())
        self.assertIsNotNone(final)

        def rejected(mutator, message):
            candidate = copy.deepcopy(final)
            mutator(candidate)
            candidate["revision"]["content_hash"] = content_hash(candidate)
            with self.assertRaisesRegex(ValueError, message):
                validate(candidate)

        rejected(lambda item: item["workflow"]["revisions"].append({"arbitrary": True}), "must remain empty")
        rejected(lambda item: item["workflow"]["weakpoints"].append({
            "id": "bad_reasoning", "payload": {
                "evidence_claim_ids": ["claim_1"], "target_claim_id": ["claim_2"],
                "reasoning_type": "support", "evidence_anchor_ids": [], "expression": "[1] supports [2]",
            },
        }), "invalid reasoning type")
        rejected(lambda item: item["workflow"]["weakpoints"].append({
            "id": "overlap", "payload": {
                "evidence_claim_ids": ["claim_1"], "target_claim_id": ["claim_1"],
                "reasoning_type": None, "evidence_anchor_ids": [], "expression": "[1] supports [1]",
            },
        }), "both evidence and target")
        rejected(lambda item: item["graph"]["operators"][0].update({"type": "banana"}), "unsupported type")
        rejected(lambda item: item["graph"]["operators"][0]["variables"].append("absent"), "distinct graph claims")

    def test_operator_background_lowers_to_official_metadata_not_logic(self) -> None:
        from pipeline_harness.domain.compiler import _compile_v2_formalization

        _, _, final = self.run_expansion(deduction())
        operator = final["graph"]["operators"][0]
        operator["background"] = ["note_1"]
        final["revision"]["content_hash"] = content_hash(final)
        validate(final)
        compiled, _ = _compile_v2_formalization(final, namespace="test", package_name="background")
        lowered = next(item for item in compiled["operators"]
                       if item["metadata"]["source_operator_id"] == operator["id"])
        self.assertEqual(2, len(lowered["variables"]))
        self.assertNotIn(lowered["metadata"]["background"][0], lowered["variables"])
        self.assertTrue(lowered["metadata"]["background"][0].endswith("::note_1"))

    def test_cleaner_integration_requests_strict_completion(self) -> None:
        cleaner_dir = self.root / "cleaner"
        (cleaner_dir / "text_test").mkdir(parents=True)
        atomic_write_json(cleaner_dir / "text_test" / "claims_final.json", {"claim": [], "note": [], "relation": []})
        from unittest.mock import Mock
        from agent_pipeline_v2.claim_cleaner import run_single_text_pipeline_sync as cleaner
        def cleaned_run(*args, **kwargs):
            self.assertEqual("deepseek-v4-flash", os.environ.get("DEEPSEEK_MODEL"))
            return "text_test"
        run = Mock(side_effect=cleaned_run)
        with patch.object(cleaner, "DATA_DIR", cleaner_dir), patch.object(cleaner, "run_single_text", run), \
                patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_MODEL": "original-model"}):
            output, ref = _clean_proposition("An atomic proposition.")
            self.assertEqual("original-model", os.environ["DEEPSEEK_MODEL"])
            run.side_effect = RuntimeError("cleaning failed")
            with self.assertRaisesRegex(RuntimeError, "cleaning failed"):
                _clean_proposition("Another proposition.")
            self.assertEqual("original-model", os.environ["DEEPSEEK_MODEL"])
        self.assertEqual((("An atomic proposition.",), {"raise_on_failure": True}), run.call_args_list[0])
        self.assertEqual([], output["claim"])
        self.assertEqual(64, len(ref["sha256"]))

    def test_private_cleaner_preserves_a_note_when_step1_has_no_claim(self) -> None:
        from agent_pipeline_v2.claim_cleaner.run_single_text_pipeline_sync import _write_note_only_result
        paper_dir = self.root / "note_only"
        paper_dir.mkdir()
        atomic_write_json(paper_dir / "organized_content.json", [{
            "id": "paper:test::conclusion_1",
            "organized_parts": [{"label": "other", "content": "A fixed boundary condition."}],
        }])
        self.assertTrue(_write_note_only_result(paper_dir, "A fixed boundary condition."))
        self.assertEqual(
            {"claim": [], "note": [{"conclusion": "conclusion_1", "text": "A fixed boundary condition.", "number": 1}], "relation": []},
            read_json(paper_dir / "claims_final.json"),
        )

    def test_native_cleaner_uses_deepseek_without_anthropic_or_claude_parameters(self) -> None:
        from agent_pipeline_v2.claim_cleaner import claude_api_call
        response = {"choices": [{"finish_reason": "stop", "message": {"content": "cleaned text"}}]}
        with patch.dict("os.environ", {"DEEPSEEK_MODEL": "deepseek-v4-flash", "DEEPSEEK_API_KEY": "test-key"}, clear=True), \
                patch.dict(sys.modules, {"anthropic": None}), \
                patch.object(claude_api_call, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as http:
            text = claude_api_call.call_claude("Original cleaning prompt", model="claude-sonnet-5", max_tokens=50000,
                                              thinking={"type": "adaptive"}, system="Source only")
        self.assertEqual("cleaned text", text)
        request = http.call_args.args[0]
        self.assertEqual("https://api.deepseek.com/v1/chat/completions", request.full_url)
        payload = json.loads(request.data)
        self.assertEqual("deepseek-v4-flash", payload["model"])
        self.assertNotIn("thinking", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual("Original cleaning prompt", payload["messages"][-1]["content"])

    def test_native_deepseek_cleaning_rejects_incomplete_output(self) -> None:
        from agent_pipeline_v2.claim_cleaner import claude_api_call
        response = {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
        with patch.dict("os.environ", {"DEEPSEEK_MODEL": "deepseek-v4-flash", "DEEPSEEK_API_KEY": "test-key"}, clear=True), \
                patch.object(claude_api_call, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                claude_api_call.call_claude("Clean this")

    def test_direct_deduction_adds_no_operator_beyond_step3_ast(self) -> None:
        expansion = {"knowledges": {}, "strategies": [strategy("deduction", ["claim_1", "claim_2"], "claim_3", ["note_1"])]}
        _, prior, final = self.run_expansion(expansion)
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual([], self.cleaning_calls)
        self.assertEqual(prior["knowledges"], final["knowledges"])
        self.assertEqual(prior["graph"]["operators"], final["graph"]["operators"])
        self.assertEqual([canonical_strategy(expansion["strategies"][0])], [item for item in final["graph"]["strategies"] if item["type"] == "deduction"])

    def test_legacy_operator_output_is_rejected_not_translated(self) -> None:
        _, prior, final = self.run_expansion({"knowledges": {}, "operators": []})
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual(prior["knowledges"], final["knowledges"])
        self.assertEqual([], final["workflow"]["weakpoints"])
        self.assertTrue(all(item["type"] == "infer" for item in final["graph"]["strategies"]))
        self.assertEqual([], self.cleaning_calls)

    def test_old_revision_without_strategies_remains_readable_without_mutation(self) -> None:
        _, prior, _ = self.run_expansion(deduction())
        del prior["graph"]["strategies"]
        prior["revision"]["content_hash"] = content_hash(prior)
        original = copy.deepcopy(prior)
        validate(prior)
        projected = project_for_official_compiler(prior)
        self.assertEqual([], projected["graph"]["strategies"])
        self.assertEqual(original, prior)

    def test_existing_step3_operators_are_not_changed_by_step4(self) -> None:
        claims = read_json(self.root / "claims.json")
        claims["relation"].append({"connects": [5, 6], "expression": "[5] 等价 [6]"})
        atomic_write_json(self.root / "claims.json", claims)
        _, prior, final = self.run_expansion(deduction())
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        self.assertEqual(2, len(prior["graph"]["operators"]))
        self.assertEqual(prior["graph"]["operators"], final["graph"]["operators"])

    def test_strategy_ids_are_official_and_background_is_not_silently_lost(self) -> None:
        _, _, final = self.run_expansion(deduction())
        from gaia.engine.ir.strategy import Strategy
        for item in final["graph"]["strategies"]:
            without_id = {key: value for key, value in item.items() if key != "strategy_id"}
            self.assertEqual(item, Strategy.model_validate(without_id).model_dump(mode="json", exclude_none=True))
        projected = project_for_official_compiler(final)
        projected["graph"]["strategies"][-1]["background"].clear()
        self.assertEqual(["note_1"], final["graph"]["strategies"][-1]["background"])
        final["graph"]["strategies"][0]["strategy_id"] = "lcs_invented"
        final["revision"]["content_hash"] = content_hash(final)
        with self.assertRaisesRegex(ValueError, "derived ID"):
            validate(final)

    def test_rejects_cycles_unrelated_strategies_and_wrong_analogy_arity(self) -> None:
        _, prior, _ = self.run_expansion(deduction())
        parameters = self.parameters(prior)
        wrong = deduction()
        wrong["strategies"][0]["premises"] = ["claim_3", "claim_1", "claim_2"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            _validate_expansion(parameters, wrong)
        wrong = deduction()
        wrong["strategies"].append(strategy("deduction", ["claim_4"], "claim_5"))
        with self.assertRaisesRegex(ValueError, "unrelated"):
            _validate_expansion(parameters, wrong)
        parameters = self.parameters(prior, "analogy")
        wrong = deduction()
        wrong["strategies"][-1]["type"] = "analogy"
        with self.assertRaisesRegex(ValueError, "two ordered premises"):
            _validate_expansion(parameters, wrong)

    def test_bad_strategy_fields_fail_closed(self) -> None:
        base = strategy("deduction", ["claim_1"], "claim_3")
        for mutation in ({"scope": "global"}, {"type": "implication"}, {"premises": []},
                         {"formal_expr": {}}, {"conditional_probabilities": [0.1, 0.9]},
                         {"premises": ["claim_1", "claim_1"]}, {"background": ["note_1", "note_1"]}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                canonical_strategy({**base, **mutation})

    def test_named_strategies_and_official_lowering_validate_in_gaia(self) -> None:
        from gaia.engine.ir.graphs import LocalCanonicalGraph
        from gaia.engine.ir.knowledge import Knowledge, make_qid
        from gaia.engine.ir.strategy import Strategy
        from gaia.engine.ir.validator import validate_local_graph

        with_existing_alternative = abduction()
        with_existing_alternative["knowledges"] = {}
        for item in with_existing_alternative["strategies"]:
            item["premises"].append("claim_6")
        examples = [("deduction", deduction()), ("abduction", with_existing_alternative)]
        analogy = deduction()
        analogy["strategies"][-1]["type"] = "analogy"
        analogy["strategies"][-1]["premises"].append("claim_5")
        examples.append(("analogy", analogy))
        for kind, expansion in examples:
            with self.subTest(kind=kind):
                Classifier.kind = kind
                _, _, final = self.run_expansion(expansion)
                self.assertEqual("succeeded", self.result.status, self.result.findings)
                projected = project_for_official_compiler(final)
                bindings = {item["id"]: make_qid("test", "step4", item["id"]) for item in projected["graph"]["knowledges"]}
                knowledges = [Knowledge(id=bindings[item["id"]], type=item["type"],
                                       content=item["content"]["canonical"] if item["content"] is not None else None)
                              for item in projected["graph"]["knowledges"]]
                leaves = [Strategy(scope="local", type=item["type"],
                                   premises=[bindings[key] for key in item["premises"]],
                                   conclusion=bindings[item["conclusion"]], background=[bindings[key] for key in item["background"]])
                          for item in projected["graph"]["strategies"]]
                result = validate_local_graph(LocalCanonicalGraph(namespace="test", package_name="step4", knowledges=knowledges, strategies=leaves))
                self.assertTrue(result.valid, result.errors)
                lowered = [item.formalize(namespace="test", package_name="step4") for item in leaves if item.type != "infer"]
                result = validate_local_graph(LocalCanonicalGraph(
                    namespace="test", package_name="step4", knowledges=knowledges + [node for item in lowered for node in item.knowledges],
                    strategies=[item.strategy for item in lowered] + [item for item in leaves if item.type == "infer"],
                ))
                self.assertTrue(result.valid, result.errors)
                self.assertTrue(all(
                    item.get("metadata", {}).get("source_relation_id") == "relation_1"
                    for item in final["graph"]["operators"]
                ))
                self.assertTrue(all("formal_expr" not in item for item in final["graph"]["strategies"]))

    def test_official_alternatives_are_derived_at_compiler_boundary(self) -> None:
        from gaia.engine.ir.graphs import LocalCanonicalGraph
        from gaia.engine.ir.knowledge import Knowledge, make_qid
        from gaia.engine.ir.strategy import Strategy
        from gaia.engine.ir.validator import validate_local_graph
        from pipeline_harness.domain.compiler import _validate_with_official_gaia
        Classifier.kind = "abduction"
        _, _, final = self.run_expansion(abduction())
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        validate(final)
        projected = project_for_official_compiler(final)
        bindings = {item["id"]: make_qid("test", "step4", item["id"]) for item in projected["graph"]["knowledges"]}
        knowledges = [Knowledge(id=bindings[item["id"]], type=item["type"],
                               content=item["content"]["canonical"] if item["content"] is not None else None)
                      for item in projected["graph"]["knowledges"]]
        strategies = [Strategy(scope="local", type=item["type"],
                               premises=[bindings[key] for key in item["premises"]], conclusion=bindings[item["conclusion"]],
                               background=[bindings[key] for key in item["background"]]) for item in projected["graph"]["strategies"]]
        graph = LocalCanonicalGraph(
            namespace="test", package_name="step4", knowledges=knowledges,
            strategies=strategies,
        )
        result = validate_local_graph(graph)
        self.assertTrue(result.valid, result.errors)
        lowered = [item.formalize(namespace="test", package_name="step4") for item in strategies if item.type != "infer"]
        graph = LocalCanonicalGraph(
            namespace="test", package_name="step4", knowledges=knowledges + [node for item in lowered for node in item.knowledges],
            strategies=[item.strategy for item in lowered] + [item for item in strategies if item.type == "infer"],
        )
        normalized, _ = _validate_with_official_gaia(graph.model_dump(mode="json"))
        self.assertEqual(0, sum(item["content"] is None for item in normalized["knowledges"]))
        self.assertEqual(1, sum(
            (item.get("metadata") or {}).get("interface_role") == "alternative_explanation"
            for item in normalized["knowledges"]
        ))
        from pipeline_harness.domain.compiler import Gaia05OfficialCompilerTool
        compiled = Gaia05OfficialCompilerTool().invoke(ToolCallRequest(
            "compile-abduction", "gaia-0.5-official-compiler", "0.5.0a7", "compile_formalization", [],
            {"formalization": final, "namespace": "test", "package_name": "step4"},
        ))
        self.assertEqual("succeeded", compiled.status)
        self.assertEqual(1, sum(
            (item.get("metadata") or {}).get("interface_role") == "alternative_explanation"
            for item in compiled.normalized["gaia_ir"]["knowledges"]
        ))

    def test_full_v21_assembly_connects_the_shared_official_step5(self) -> None:
        root = Path(__file__).parents[1]
        self.assertFalse((root / "pipeline.step1-4.json").exists())
        pipeline = json.loads((root / "pipeline.step1-5.json").read_text(encoding="utf-8"))
        self.assertEqual("agent-pipeline-v2.1-step1-5", pipeline["pipeline_id"])
        self.assertEqual("2.1.0", pipeline["version"])
        for path in root.glob("pipeline.step*.json"):
            config = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(config["pipeline_id"].startswith("agent-pipeline-v2.1-"))
            self.assertEqual("2.1.0", config["version"])
        step5 = pipeline["stages"][-1]
        self.assertEqual("pipeline_harness.domain.compiler:Step5CompileGaiaIRPlugin", step5["plugin"])
        self.assertEqual(
            "pipeline_harness.domain.compiler:Gaia05OfficialCompilerTool",
            step5["options"]["compiler_plugin"],
        )
        self.assertEqual({}, pipeline["stages"][0]["options"])
        self.assertEqual({}, pipeline["stages"][1]["options"])

    def test_step5_index_uses_artifact_history_and_real_reasoning_incidence(self) -> None:
        self.pipeline["stages"].append({
            "name": "step5_compile_gaia_ir",
            "plugin": "pipeline_harness.domain.compiler:Step5CompileGaiaIRPlugin",
            "options": {
                "compiler_plugin": "pipeline_harness.domain.compiler:Gaia05OfficialCompilerTool",
            },
        })
        store, _, final = self.run_expansion(deduction())
        self.assertEqual("succeeded", self.result.status, self.result.findings)
        index_ref = next(ref for ref in store.load_artifacts() if ref.kind == "knowledge.index")
        index = read_json(store.artifact_path(index_ref))
        entries = {item["knowledge_id"]: item for item in index["entries"]}

        self.assertEqual(1, entries["claim_1"]["first_seen_step"])
        self.assertEqual(5, entries["claim_1"]["current_step"])
        added_claim = next(key for key in final["knowledges"] if key.startswith("claim_step4_"))
        self.assertEqual(4, entries[added_claim]["first_seen_step"])
        helper = next(
            operator["conclusion"] for operator in final["graph"]["operators"]
            if operator.get("metadata", {}).get("derived_ast_helper") is True
        )
        helper_operator = next(
            operator for operator in final["graph"]["operators"]
            if operator.get("conclusion") == helper
        )
        self.assertEqual(3, entries[helper]["first_seen_step"])
        self.assertEqual("formal_internal", entries[helper]["visibility"])
        self.assertIn(helper_operator["id"], entries[helper]["incoming_reasoning"])
        self.assertIn(helper_operator["id"], entries["claim_1"]["outgoing_reasoning"])
        background_strategies = {
            strategy["strategy_id"] for strategy in final["graph"]["strategies"]
            if "note_1" in strategy["background"]
        }
        self.assertTrue(background_strategies)
        self.assertTrue(background_strategies <= set(entries["note_1"]["outgoing_reasoning"]))

    @unittest.skipUnless(shutil.which("node"), "Viewer projection test requires Node.js")
    def test_actual_viewer_projections_lower_formal_strategies_to_operators(self) -> None:
        Classifier.kind = "abduction"
        store, _, final = self.run_expansion(abduction())
        view = project_run(store.run_dir)
        html = (store.run_dir / "views" / "viewer.html").read_text(encoding="utf-8")
        functions = []
        for name in ("knowledgeNode", "strategyNodes", "operatorType", "directOperatorEdges", "availableStep", "availableLayers", "overviewProjection", "standardProjection", "compactFormalGraph", "localFormalGraph"):
            start = html.index(f"    function {name}(")
            end = html.index("\n    function ", start + 1)
            functions.append(html[start:end])
        script = """
          const fs = require('fs');
          const {view, functions} = JSON.parse(fs.readFileSync(0, 'utf8'));
          const project = new Function('view', 'state', 'byId', 'devMode',
            'const groupsFor = item => item?.fold_groups || (item?.fold_group ? [item.fold_group] : []);\\n' + functions.join('\\n') +
            '\\nconst standard=standardProjection("4"), overview=overviewProjection("4");' +
            '\\nstate.expandedWeakpoints=new Set(view.nodes.filter(n=>n.step===3&&n.kind==="weakpoint").map(n=>n.entity_id));' +
            '\\nreturn {standard, overview, expanded:overviewProjection("3"), layers:availableLayers()};');
          const state = {step: '4', mode: 'standard', expandedWeakpoints: new Set()};
          process.stdout.write(JSON.stringify(project(view, state, new Map(view.nodes.map(n => [n.id, n])), true)));
        """
        output = subprocess.run([shutil.which("node"), "-e", script],
                                input=json.dumps({"view": view.to_dict(), "functions": functions}),
                                text=True, capture_output=True, check=True)
        projected = json.loads(output.stdout)
        self.assertIn("strategies", {layer["id"] for layer in projected["layers"]})
        step3_weakpoint = next(
            node for node in view.nodes
            if node["step"] == 3 and node["kind"] == "weakpoint"
        )
        expanded = set(step3_weakpoint["details"].get("expanded_strategy_ids", []))
        self.assertTrue(expanded)
        self.assertTrue(expanded <= {item["strategy_id"] for item in final["graph"]["strategies"]})
        for mode in ("standard",):
            nodes = projected[mode]["nodes"]
            self.assertEqual(0, len([item for item in nodes if item["kind"] == "weakpoint"]))
            # Every non-relational lowered clause keeps a real operator node,
            # while equivalence (like implication) is projected as a single
            # double-headed arrow edge rather than a symbol-bearing operator
            # node.
            operator_types = {item["details"]["type"] for item in nodes if item["kind"] == "operator"}
            self.assertTrue({"conjunction", "disjunction"} <= operator_types)
            self.assertNotIn("equivalence", operator_types)
            self.assertIn("equivalence:'↔'", html)
            self.assertIn(".edge.equivalence { marker-start:url(#arrow-start); marker-end:url(#arrow); }", html)
            self.assertFalse(any(item["kind"] == "note" for item in nodes))
            ids = {item["id"] for item in nodes}
            self.assertTrue(all(edge["source"] in ids and edge["target"] in ids for edge in projected[mode]["edges"]))
            touched = {endpoint for edge in projected[mode]["edges"] for endpoint in (edge["source"], edge["target"])}
            orphan_operators = [
                item["id"] for item in nodes if item["kind"] == "operator" and item["id"] not in touched
            ]
            self.assertEqual([], orphan_operators)
            # A Step 4 summary claim is a real graph member even though its ID
            # contains "step4_weakpoint_"; it must stay public, and every
            # disjunction clause must keep an outgoing equivalence edge to its
            # target instead of dangling.
            summary_claims = [item for item in nodes if "step4_weakpoint_" in str(item["entity_id"])]
            self.assertTrue(summary_claims)
            self.assertTrue(all(item["layer"] == "claims" for item in summary_claims))
            disjunctions = [
                item for item in nodes if item["kind"] == "operator" and item["details"]["type"] == "disjunction"
            ]
            self.assertTrue(disjunctions)
            outgoing = {edge["source"] for edge in projected[mode]["edges"]}
            self.assertTrue(all(item["id"] in outgoing for item in disjunctions))
            orphan_helpers = [
                item["id"] for item in nodes if item.get("layer") == "helpers" and item["id"] not in touched
            ]
            self.assertEqual([], orphan_helpers)
        standard_nodes = projected["standard"]["nodes"]
        standard_by_id = {item["id"]: item for item in standard_nodes}
        self.assertTrue({"claim_1", "claim_2", "claim_3"} <= {item["entity_id"] for item in standard_nodes})
        self.assertTrue(any(item["kind"] == "alternative_placeholder" for item in standard_nodes))
        unexpanded_weakpoints = {
            item["entity_id"] for item in view.nodes
            if item["step"] == 3 and item["kind"] == "weakpoint"
            and not item["details"].get("expanded_strategy_ids")
        }
        self.assertTrue(
            unexpanded_weakpoints
            <= {item["entity_id"] for item in projected["expanded"]["nodes"] if item["kind"] == "weakpoint"}
        )
        equivalences = [
            edge for edge in projected["standard"]["edges"]
            if edge["semantic_type"] == "equivalence"
        ]
        for edge in equivalences:
            self.assertIn(edge["source"], standard_by_id)
            self.assertIn(edge["target"], standard_by_id)


if __name__ == "__main__":
    unittest.main()
