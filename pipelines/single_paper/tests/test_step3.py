from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_pipeline_v2.authoring import emit_formalization
from agent_pipeline_v2.step3 import (
    _relation_clusters, _screen_classifications, _transitive_shortcut_indexes, _validate_cluster_result,
)
from pipeline_harness.domain.tools import ToolCallRequest, ToolCallResponse
from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json
from pipeline_harness.view.projector import project_run


class StripClaimsFinalSourcePlugin:
    """Downgrade claims_final relations so Step 3 exercises cluster+LLM.

    Production adopts claims_final relations directly; tests that target the
    LLM normalization path deliberately drop the high-confidence marker.
    """

    def run(self, context):
        reference = context.latest("formalization")
        document = json.loads(context.artifact_path(reference).read_text(encoding="utf-8"))
        for link in document["workflow"]["non_reasoning_links"]:
            metadata = link.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("source", None)
        prior = document["revision"]
        document["revision"] = {
            "revision_id": f"revision_{context.run_id}_strip",
            "supersedes": prior.get("revision_id"),
            "parent_hash": prior.get("content_hash"),
            "content_hash": "",
        }
        return emit_formalization(context, document, step=2, step_name="strip_claims_final")


class FakeWeakpointClassifier:
    name = "fake-weakpoint-classifier"
    version = "1"
    calls: list[ToolCallRequest] = []

    def invoke(self, request: ToolCallRequest) -> ToolCallResponse:
        self.__class__.calls.append(request)
        if request.operation == "classify_weakpoints":
            return ToolCallResponse(
                request.call_id, "succeeded", {},
                {"classifications": [
                    {"weakpoint_id": item["weakpoint_id"], "reasoning_type": "abduction"}
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
                    "reasoning_type": "abduction",
                    "expression": " 是 ".join(f"[{key}]" for key in endpoints),
                })
            normalized.append({"cluster_id": cluster["cluster_id"], "weakpoints": weakpoints, "rejected_relation_ids": []})
        return ToolCallResponse(request.call_id, "succeeded", {"clusters": clusters}, {"clusters": normalized})


class ParallelWeakpointClassifier(FakeWeakpointClassifier):
    barrier = threading.Barrier(2)
    thread_ids: set[int] = set()

    def invoke(self, request: ToolCallRequest) -> ToolCallResponse:
        if request.operation == "normalize_weakpoint_clusters":
            self.__class__.thread_ids.add(threading.get_ident())
            self.__class__.barrier.wait(timeout=2)
        return super().invoke(request)


class Step3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "paper_text.md").write_text("[#result] The observation is reported here.\n", encoding="utf-8")
        atomic_write_json(self.root / "claims_final.json", {
            "claim": [
                {"number": 1, "conclusion": "c", "text": "Observed effect.", "is_pure_data": False},
                {"number": 2, "conclusion": "c", "text": "Second observed effect.", "is_pure_data": False},
                {"number": 3, "conclusion": "c", "text": "General mechanism.", "is_pure_data": False},
            ],
            "note": [],
            "relation": [
                {"connects": [1, 2, 3], "expression": "([1] 和 [2]) 是 [3] 的例子或证据"},
                {"connects": [1, 2], "expression": "[1] 与 [2] 矛盾"},
            ],
        })
        atomic_write_json(self.root / "manifest.json", {"artifacts": [
            {"path": "paper_text.md", "kind": "source.paper_text", "media_type": "text/markdown"},
            {"path": "claims_final.json", "kind": "source.claims_final", "media_type": "application/json"},
        ]})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_transitive_shortcut_requires_full_source_coverage(self) -> None:
        # A chain that only covers a subset of a hyperedge's sources must not
        # remove the direct edge, or the uncovered observations are orphaned.
        partial = [
            (0, {"o1", "o2", "o3"}, "claim_27", "abduction"),
            (1, {"o1"}, "claim_65", "abduction"),
            (2, {"claim_65"}, "claim_27", "abduction"),
        ]
        self.assertEqual(set(), _transitive_shortcut_indexes(partial))
        # A chain whose leaves cover every direct source still replaces it.
        full = [
            (0, {"o1"}, "claim_27", "abduction"),
            (1, {"o1"}, "claim_65", "abduction"),
            (2, {"claim_65"}, "claim_27", "abduction"),
        ]
        self.assertEqual({0}, _transitive_shortcut_indexes(full))

    def test_screening_keeps_expandable_relation_but_rejects_duplicate_content(self) -> None:
        document = {
            "knowledges": {
                "claim_1": {"type": "claim", "content": {"canonical": "Observed effect"}, "source_anchor_ids": ["p1"]},
                "claim_2": {"type": "claim", "content": {"canonical": "Broader mechanism"}, "source_anchor_ids": ["p1"]},
                "claim_3": {"type": "claim", "content": {"canonical": "Observed effect"}, "source_anchor_ids": ["p1"]},
            },
            "workflow": {"source_anchors": [{"anchor_id": "p1", "source_kind": "source.paper_text"}]},
        }
        weakpoints = [
            {"id": "w1", "payload": {"evidence_claim_ids": ["claim_1"], "target_claim_id": ["claim_2"]}},
            {"id": "w2", "payload": {"evidence_claim_ids": ["claim_1"], "target_claim_id": ["claim_3"]}},
        ]
        result, rejected = _screen_classifications(document, weakpoints, {"w1": "abduction", "w2": "deduction"})
        self.assertEqual({"w1": "abduction", "w2": None}, result)
        self.assertEqual({"w2"}, rejected)

    def test_classifies_only_reasoning_type_and_records_audited_input(self) -> None:
        FakeWeakpointClassifier.calls = []
        pipeline = {
            "pipeline_id": "step3-classification-test", "version": "1",
            "stages": [
                {"name": "import", "plugin": "agent_pipeline_v2.step1:ClaimsFinalInputImporter", "options": {}},
                {"name": "step1", "plugin": "agent_pipeline_v2.step1:Step1ImportClaimsFinalPlugin", "options": {}},
                {"name": "strip", "plugin": f"{__name__}:StripClaimsFinalSourcePlugin", "options": {}},
                {"name": "step3", "plugin": "agent_pipeline_v2.step3:Step3AnalyzeReasoningPlugin", "options": {"tool_plugin": f"{__name__}:FakeWeakpointClassifier"}},
            ],
        }
        store = RunStore.create(self.root / "runs", pipeline, input_manifest=self.root / "manifest.json")
        result = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", result.status)
        self.assertEqual(2, len(FakeWeakpointClassifier.calls))
        self.assertEqual("normalize_weakpoint_clusters", FakeWeakpointClassifier.calls[0].operation)
        record = FakeWeakpointClassifier.calls[0].parameters["clusters"][0]
        self.assertEqual({"cluster_id", "relation_context_id", "claims", "candidate_relations", "source_excerpts"}, set(record))
        claims = {item["claim_id"]: item["content"] for item in record["claims"]}
        self.assertEqual("Observed effect.", claims["claim_1"])
        self.assertEqual("General mechanism.", claims["claim_3"])
        formalizations = [item for item in store.load_artifacts() if item.kind == "formalization"]
        final = json.loads(store.artifact_path(formalizations[-1]).read_text(encoding="utf-8"))
        self.assertEqual([], final["workflow"]["revisions"])
        self.assertEqual([], final["workflow"]["weakpoints"])
        self.assertTrue(
            not final["workflow"]["weakpoints"]
            or any(item["code"] == "STEP3_REJECTED_WEAKPOINT" for item in result.findings)
        )
        self.assertEqual(
            {
                "id": "operator_relation_2",
                "type": "contradiction",
                "variables": ["claim_1", "claim_2"],
                "metadata": {"expression": "[1] 与 [2] 矛盾", "source_relation_id": "relation_2"},
            },
            final["graph"]["operators"][0],
        )
        self.assertEqual(2, len([item for item in store.load_artifacts() if item.kind == "tool.semantic_review.response"]))

    def test_claims_final_relations_are_adopted_without_llm_and_in_order(self) -> None:
        claims_final = json.loads((self.root / "claims_final.json").read_text(encoding="utf-8"))
        claims_final["relation"].append({"connects": [1, 2], "expression": "[1] 推出 [2]"})
        atomic_write_json(self.root / "claims_final.json", claims_final)
        FakeWeakpointClassifier.calls = []
        pipeline = {
            "pipeline_id": "step3-claims-final-test", "version": "1",
            "stages": [
                {"name": "import", "plugin": "agent_pipeline_v2.step1:ClaimsFinalInputImporter", "options": {}},
                {"name": "step1", "plugin": "agent_pipeline_v2.step1:Step1ImportClaimsFinalPlugin", "options": {}},
                {"name": "step3", "plugin": "agent_pipeline_v2.step3:Step3AnalyzeReasoningPlugin", "options": {"tool_plugin": f"{__name__}:FakeWeakpointClassifier"}},
            ],
        }
        store = RunStore.create(self.root / "claims-final-runs", pipeline, input_manifest=self.root / "manifest.json")
        result = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", result.status)
        # High-confidence imported relations never reach the LLM judge.
        self.assertEqual([], FakeWeakpointClassifier.calls)
        final_ref = next(ref for ref in store.load_artifacts() if ref.kind == "formalization" and ref.metadata["step"] == 3)
        final = json.loads(store.artifact_path(final_ref).read_text(encoding="utf-8"))
        weakpoints = final["workflow"]["weakpoints"]
        self.assertEqual(
            ["weakpoint_relation_1", "weakpoint_relation_3"],
            [item["id"] for item in weakpoints],
        )
        payload = weakpoints[0]["payload"]
        self.assertEqual(["claim_1", "claim_2"], payload["evidence_claim_ids"])
        self.assertEqual(["claim_3"], payload["target_claim_id"])
        self.assertIsNone(payload["reasoning_type"])
        self.assertEqual("([claim_1] 和 [claim_2]) 是 [claim_3] 的例子或证据", payload["expression"])
        implied = weakpoints[1]["payload"]
        self.assertEqual(["claim_1"], implied["evidence_claim_ids"])
        self.assertEqual(["claim_2"], implied["target_claim_id"])
        self.assertEqual("[claim_1] 推出 [claim_2]", implied["expression"])
        # The fixed contradiction relation is still materialized mechanically.
        self.assertEqual(
            {
                "id": "operator_relation_2",
                "type": "contradiction",
                "variables": ["claim_1", "claim_2"],
                "metadata": {"expression": "[1] 与 [2] 矛盾", "source_relation_id": "relation_2"},
            },
            final["graph"]["operators"][0],
        )
        self.assertFalse(any(item["code"] == "STEP3_UNRESOLVED_IMPORTED_RELATION" for item in result.findings))

    def test_argument_cluster_reorients_and_merges_observations_without_expanding_two_hops(self) -> None:
        anchors = [
            {"anchor_id": "p1", "source_kind": "source.paper_text"},
            {"anchor_id": "p2", "source_kind": "source.paper_text"},
            {"anchor_id": "p3", "source_kind": "source.paper_text"},
        ]
        document = {
            "knowledges": {
                "claim_O15": {"type": "claim", "content": {"canonical": "Global permutation degraded performance."}, "source_anchor_ids": ["p1"]},
                "claim_O16": {"type": "claim", "content": {"canonical": "Local permutation moderately degraded performance."}, "source_anchor_ids": ["p1"]},
                "claim_21": {"type": "claim", "content": {"canonical": "Global and local permutation have the stated effects."}, "source_anchor_ids": ["p1"]},
                "claim_23": {"type": "claim", "content": {"canonical": "Layerwise mask statistics transfer ticket information."}, "source_anchor_ids": ["p1"]},
                "claim_20": {"type": "claim", "content": {"canonical": "Mask structure contains useful information."}, "source_anchor_ids": ["p2"]},
                "claim_99": {"type": "claim", "content": {"canonical": "A remote claim."}, "source_anchor_ids": ["p3"]},
            },
            "workflow": {"source_anchors": anchors},
        }

        def link(key, sources, target, expression, context=None):
            relation = {"expression": expression}
            if context is not None:
                relation["relation_context_id"] = context
            return {"id": key, "sources": sources, "target": target, "metadata": {"relation": relation}}

        links = [
            link("relation_27", ["claim_O15"], "claim_21", "[claim_O15] 是 [claim_21] 的证据", "experiment_mask"),
            link("relation_28", ["claim_O16"], "claim_21", "[claim_O16] 是 [claim_21] 的证据", "experiment_mask"),
            link("relation_29", ["claim_O15"], "claim_23", "[claim_O15] 是 [claim_23] 的证据", "experiment_mask"),
            link("relation_30", ["claim_O16"], "claim_23", "[claim_O16] 是 [claim_23] 的证据", "experiment_mask"),
            link("relation_8", ["claim_21"], "claim_20", "[claim_21] 推出 [claim_20]"),
            link("relation_remote", ["claim_20"], "claim_99", "[claim_20] 推出 [claim_99]"),
        ]
        with patch("agent_pipeline_v2.step3._anchor_excerpt", return_value="Source excerpt"):
            clusters = _relation_clusters(None, document, links)
        self.assertEqual(
            {"relation_27", "relation_28", "relation_29", "relation_30", "relation_8"},
            {item["relation_id"] for item in clusters[0]["candidate_relations"]},
        )
        self.assertEqual(["relation_remote"], [item["relation_id"] for item in clusters[1]["candidate_relations"]])
        normalized = {"clusters": [
            {"cluster_id": clusters[0]["cluster_id"], "weakpoints": [
                {"member_relation_ids": ["relation_27", "relation_28"], "evidence_claim_ids": ["claim_21"],
                 "target_claim_id": ["claim_O15", "claim_O16"], "reasoning_type": "deduction",
                 "expression": "[claim_21] 推出 [claim_O15] 和 [claim_O16]"},
                {"member_relation_ids": ["relation_29", "relation_30"], "evidence_claim_ids": ["claim_21"],
                 "target_claim_id": ["claim_23"], "reasoning_type": "abduction",
                 "expression": "[claim_21] 溯因支持 [claim_23]"},
                {"member_relation_ids": ["relation_8"], "evidence_claim_ids": ["claim_23"],
                 "target_claim_id": ["claim_20"], "reasoning_type": "deduction",
                 "expression": "[claim_23] 推出 [claim_20]"},
            ], "rejected_relation_ids": []},
            {"cluster_id": clusters[1]["cluster_id"], "weakpoints": [], "rejected_relation_ids": ["relation_remote"]},
        ]}
        weakpoints = _validate_cluster_result(document, clusters, normalized)
        self.assertEqual(
            [(["claim_21"], ["claim_O15", "claim_O16"]),
             (["claim_21"], ["claim_23"]),
             (["claim_23"], ["claim_20"])],
            [(item["payload"]["evidence_claim_ids"], item["payload"]["target_claim_id"]) for item in weakpoints],
        )

    def test_clusters_read_one_frozen_input_in_parallel_and_merge_once(self) -> None:
        claims = json.loads((self.root / "claims_final.json").read_text(encoding="utf-8"))
        claims["relation"].append({"connects": [2, 1], "expression": "[2] 推出 [1]"})
        atomic_write_json(self.root / "claims_final.json", claims)
        ParallelWeakpointClassifier.calls = []
        ParallelWeakpointClassifier.thread_ids = set()
        ParallelWeakpointClassifier.barrier = threading.Barrier(2)
        pipeline = {
            "pipeline_id": "step3-parallel-test", "version": "1",
            "stages": [
                {"name": "import", "plugin": "agent_pipeline_v2.step1:ClaimsFinalInputImporter", "options": {}},
                {"name": "step1", "plugin": "agent_pipeline_v2.step1:Step1ImportClaimsFinalPlugin", "options": {}},
                {"name": "strip", "plugin": f"{__name__}:StripClaimsFinalSourcePlugin", "options": {}},
                {"name": "step3", "plugin": "agent_pipeline_v2.step3:Step3AnalyzeReasoningPlugin",
                 "options": {"tool_plugin": f"{__name__}:ParallelWeakpointClassifier"}},
            ],
        }
        store = RunStore.create(self.root / "parallel-runs", pipeline, input_manifest=self.root / "manifest.json")
        self.assertEqual("succeeded", run_pipeline(store.run_dir).status)
        self.assertEqual(3, len(ParallelWeakpointClassifier.calls))
        cluster_calls = [call for call in ParallelWeakpointClassifier.calls if call.operation == "normalize_weakpoint_clusters"]
        self.assertEqual(2, len(cluster_calls))
        self.assertTrue(all(len(call.parameters["clusters"]) == 1 for call in cluster_calls))
        self.assertEqual(2, len(ParallelWeakpointClassifier.thread_ids))
        step3_refs = [ref for ref in store.load_artifacts() if ref.kind == "formalization" and ref.metadata["step"] == 3]
        self.assertEqual(1, len(step3_refs))

    def test_nested_fixed_expression_is_materialized_as_an_ast(self) -> None:
        claims = json.loads((self.root / "claims_final.json").read_text(encoding="utf-8"))
        claims["relation"] = [{"connects": [1, 2, 3], "expression": "[1] 或 ([2] 且 [3])"}]
        atomic_write_json(self.root / "claims_final.json", claims)
        pipeline = {
            "pipeline_id": "step3-ast-test", "version": "1",
            "view_adapter": "agent_pipeline_v2.view:V2FormalizationViewAdapter",
            "stages": [
                {"name": "import", "plugin": "agent_pipeline_v2.step1:ClaimsFinalInputImporter", "options": {}},
                {"name": "step1", "plugin": "agent_pipeline_v2.step1:Step1ImportClaimsFinalPlugin", "options": {}},
                {"name": "step3", "plugin": "agent_pipeline_v2.step3:Step3AnalyzeReasoningPlugin", "options": {}},
            ],
        }
        store = RunStore.create(self.root / "ast-runs", pipeline, input_manifest=self.root / "manifest.json")
        self.assertEqual("succeeded", run_pipeline(store.run_dir).status)
        final_ref = next(ref for ref in store.load_artifacts() if ref.kind == "formalization" and ref.metadata["step"] == 3)
        final = json.loads(store.artifact_path(final_ref).read_text(encoding="utf-8"))
        by_type = {item["type"]: item for item in final["graph"]["operators"]}
        self.assertEqual(["claim_2", "claim_3"], by_type["conjunction"]["variables"])
        helper = by_type["conjunction"]["conclusion"]
        self.assertEqual(["claim_1", helper], by_type["disjunction"]["variables"])
        self.assertIn(helper, final["graph"]["nodes"])
        view = project_run(store.run_dir)
        helper_node = next(node for node in view.nodes if node.get("step") == 3 and node.get("entity_id") == helper)
        self.assertEqual("formal_internal", helper_node["details"]["visibility"])
        self.assertEqual("helpers", helper_node["layer"])
        self.assertEqual(["standard"], helper_node["visible_at"])
        self.assertFalse(any(document["title"] == helper for document in view.search_documents))


if __name__ == "__main__":
    unittest.main()
