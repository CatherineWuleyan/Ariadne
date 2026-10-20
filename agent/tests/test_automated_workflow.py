from __future__ import annotations

import tempfile
import unittest
import copy
from pathlib import Path
from unittest.mock import patch

from pipeline_harness.checks import check_run
from pipeline_harness.domain.workflow import AUTOMATED_FORMALIZATION_PIPELINE
from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json, read_json
from pipeline_harness.view.projector import project_run
from pipeline_harness.view.model import search_view
from pipeline_harness.domain.tools import ToolCallResponse
from pipeline_harness.domain.contracts import canonical_hash
from pipeline_harness.domain.tools import ToolCallRequest
from pipeline_harness.domain.vision import DeepSeekFlashVisionTool


class FakeSemanticTool:
    name = "fake-semantic-review"
    version = "test-1"

    def invoke(self, request):
        return ToolCallResponse(
            request.call_id,
            "succeeded",
            {"raw_test_payload": True},
            {"snapshot_patch": {"review": {"status": "reviewed", "issues": []}}},
        )


class CaptureStep2Tool:
    name = "capture-step2"
    version = "test-1"
    request = None

    def invoke(self, request):
        type(self).request = request
        return ToolCallResponse(request.call_id, "succeeded", {"captured": True}, {"snapshot_patch": {}})


class ConfirmDeductionTool:
    name = "confirm-deduction"
    version = "test-1"

    def invoke(self, request):
        strategies = copy.deepcopy(request.parameters["snapshot"]["reasoning_units"])
        for strategy in strategies:
            if strategy.get("type") == "deduction":
                strategy["coarse"]["necessity_test"] = {"status": "confirmed", "if_false_conclusion_must_fail": True, "rationale": "Confirmed by test semantic reviewer."}
        return ToolCallResponse(request.call_id, "succeeded", {"confirmed": True}, {"snapshot_patch": {"reasoning_units": strategies}})


class FakeOfficialCompiler:
    name = "fake-official-gaia-compiler"
    version = "test-1"

    def invoke(self, request):
        graph = {
            "namespace": request.parameters["namespace"],
            "package_name": request.parameters["package_name"],
            "scope": "local",
            "ir_hash": "sha256:a969d63948c037e83b5433145c1e489980c49de9f803156a6e3d3b263fed903b",
            "knowledges": [],
            "operators": [],
            "strategies": [],
            "composes": [],
            "formula_graphs": [],
        }
        return ToolCallResponse(
            request.call_id,
            "succeeded",
            {"compiler_stdout": "ok"},
            {"gaia_ir": graph},
            metadata={"contract_status": "official"},
        )


class InvalidEdgeTool:
    name = "invalid-edge-review"
    version = "test-1"

    def invoke(self, request):
        return ToolCallResponse(request.call_id, "succeeded", {"inserted": "broken_edge"}, {"snapshot_patch": {
            "non_reasoning_links": [{
                "id": "broken_edge", "link_type": "related", "source": "claim_A1",
                "target": "missing_claim", "reasoning": False,
            }]
        }})


class AutomatedWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        validator = patch(
            "pipeline_harness.domain.compiler._validate_with_official_gaia",
            side_effect=lambda payload: (payload, "ir-v1+official-test"),
        )
        validator.start()
        self.addCleanup(validator.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "paper.md").write_text("# Generic paper\n\nThe measured experimental effect is consistently present in every evaluated sample.\n", encoding="utf-8")
        atomic_write_json(self.root / "claims.json", {
            "assertions": [
                {"id": "A1", "text_en": "The measured experimental effect is consistently present in every evaluated sample.", "sources": [{"global_id": "g-observation"}]},
                {"id": "A2", "text_en": "The effect supports the proposed conclusion.", "sources": [{"global_id": "g-conclusion"}]},
            ],
            "relations": [{"id": "R1", "related_assertions": ["A1", "A2"], "expression": "[A1] 推出 [A2]", "relation_type": "推出", "inference_type": "演绎", "relation_description": "The observation entails the conclusion."}],
        })
        atomic_write_json(self.root / "graph.json", {"code": 0, "data": {"papers": [{"graph": {"nodes": [
            {"id": "paper:x::observation", "global_id": "g-observation", "type": "claim", "kind": "observation", "content": "The measured experimental effect is consistently present in every evaluated sample."},
            {"id": "paper:x::conclusion", "global_id": "g-conclusion", "type": "claim", "kind": "conclusion", "content": "The effect supports the proposed conclusion."},
        ], "edges": [{"source": "paper:x::observation", "target": "paper:x::conclusion", "type": "concludes"}]}}]}})
        atomic_write_json(self.root / "manifest.json", {"artifacts": [
            {"path": "paper.md", "kind": "source.paper_text", "media_type": "text/markdown", "metadata": {"version": "1"}},
            {"path": "claims.json", "kind": "source.clean_claims", "media_type": "application/json", "metadata": {"version": "1"}},
            {"path": "graph.json", "kind": "source.lkm_coarse_graph", "media_type": "application/json", "metadata": {"version": "1"}},
            {"path_glob": "figures/*", "kind": "source.original_figure", "required": False, "metadata": {"version": "1"}},
        ]})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def pipeline_with_official_compiler(self, pipeline_id: str) -> dict:
        pipeline = copy.deepcopy(AUTOMATED_FORMALIZATION_PIPELINE)
        pipeline["pipeline_id"] = pipeline_id
        pipeline["stages"][5]["options"] = {
            "compiler_plugin": "tests.test_automated_workflow:FakeOfficialCompiler"
        }
        return pipeline

    def test_runs_generic_five_step_workflow_and_projects_each_step(self) -> None:
        pipeline = self.pipeline_with_official_compiler("generic-five-step-test")
        store = RunStore.create(self.root / "runs", pipeline, input_manifest=self.root / "manifest.json")
        run = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", run.status)
        artifacts = store.load_artifacts()
        step_artifacts = sorted(
            (a for a in artifacts if a.kind == "formalization"),
            key=lambda a: a.metadata["step"],
        )
        self.assertEqual([1, 2, 3, 4, 5], [a.metadata["step"] for a in step_artifacts])
        expected_step_names = [
            "step1_extract_evidence",
            "step2_normalize_claims",
            "step3_analyze_reasoning",
            "step4_formalize_reasoning",
            "step5_compile_gaia_ir",
        ]
        for step_ref, expected_name in zip(step_artifacts, expected_step_names, strict=True):
            formalization = read_json(store.artifact_path(step_ref))
            self.assertEqual(expected_name, formalization["workflow"]["revisions"][-1]["step"]["name"])
            self.assertEqual(expected_name, step_ref.metadata["step_name"])
        self.assertEqual(
            ["formalization.json"] * 5,
            [a.metadata["logical_name"] for a in step_artifacts],
        )
        self.assertEqual(5, len([a for a in artifacts if a.kind == "formalization"]))
        formalization_ref = step_artifacts[-1]
        formalization = read_json(store.artifact_path(formalization_ref))
        self.assertEqual(
            {"schema_version", "revision", "package", "graph", "workflow"},
            set(formalization),
        )
        stored_hash = formalization["revision"].pop("content_hash")
        self.assertEqual(stored_hash, canonical_hash(formalization))
        indexes = [a for a in artifacts if a.kind == "knowledge.index"]
        self.assertEqual(1, len(indexes))
        self.assertEqual(5, indexes[0].metadata["step"])
        self.assertTrue(indexes[0].metadata["final"])
        validation_refs = sorted((a for a in artifacts if a.kind == "formalization.validation"), key=lambda a: a.metadata["step"])
        self.assertEqual(5, len(validation_refs))
        for validation_ref in validation_refs:
            report = read_json(store.artifact_path(validation_ref))
            self.assertEqual("passed", report["summary"]["status"])
            self.assertEqual([], report["findings"])
        self.assertEqual(1, len([a for a in artifacts if a.kind == "gaia.ir"]))
        self.assertEqual(0, len([a for a in artifacts if a.kind == "gaia.ir.candidate"]))
        self.assertEqual(1, len([a for a in artifacts if a.kind == "tool.gaia_compile.response"]))
        step4 = read_json(store.artifact_path(step_artifacts[3]))
        self.assertFalse(step4["graph"]["operators"])
        view = project_run(store.run_dir)
        self.assertEqual([1, 2, 3, 4, 5], [stage["number"] for stage in view.stages])
        self.assertTrue(all(stage["validation_status"] == "passed" for stage in view.stages))
        self.assertTrue(any(layer["id"] == "validation" for layer in view.layers))
        self.assertEqual([], formalization["workflow"]["non_reasoning_links"])
        self.assertTrue(any(item["id"] == "weakpoint_R1" for item in formalization["workflow"]["proposals"]))
        self.assertTrue(search_view(view, "A1"))
        self.assertEqual([], check_run(store.run_dir))

    def test_step1_ignores_unlisted_lkm_conclusions(self) -> None:
        atomic_write_json(self.root / "claims.json", {
            "assertions": [
                {"id": "A1", "text_en": "First scoped reading of the shared coarse conclusion.", "sources": [{"global_id": "g-shared"}]},
                {"id": "A2", "text_en": "Second scoped reading of the shared coarse conclusion.", "sources": [{"global_id": "g-shared"}]},
            ],
            "relations": [],
        })
        atomic_write_json(self.root / "graph.json", {"code": 0, "data": {"papers": [{"graph": {
            "nodes": [
                {"id": "paper:x::coarse", "global_id": "g-shared", "type": "claim", "kind": "conclusion", "content": "A coarse conclusion with multiple clean-claim readings."},
                {"id": "paper:x::highlight", "type": "claim", "kind": "highlight", "content": "A refinement-only highlight."},
                {"id": "factor:steps", "type": "factor", "kind": "reasoning_steps", "steps": [{"reasoning": "A refinement-only step."}]},
            ],
            "edges": [],
        }}]}})
        store = RunStore.create(self.root / "runs-shared-source", AUTOMATED_FORMALIZATION_PIPELINE, input_manifest=self.root / "manifest.json")
        run = run_pipeline(store.run_dir, max_stages=2)
        self.assertEqual("waiting", run.status)
        snapshot_ref = next(artifact for artifact in store.load_artifacts() if artifact.kind == "formalization")
        snapshot = read_json(store.artifact_path(snapshot_ref))
        self.assertEqual(
            {"claim_A1", "claim_A2"},
            {item["id"] for item in snapshot["graph"]["knowledges"]},
        )
        self.assertEqual([], snapshot["workflow"]["non_reasoning_links"])
        view = project_run(store.run_dir)
        step1_nodes = {node["entity_id"] for node in view.nodes if node.get("step") == 1}
        self.assertNotIn("lkm_paper_x_highlight", step1_nodes)
        self.assertNotIn("lkm_factor_steps", step1_nodes)
        self.assertFalse(any(node["kind"] == "source_anchor" for node in view.nodes))

    def test_step1_records_full_document_and_each_input_figure_as_provenance(self) -> None:
        figures = self.root / "figures"
        figures.mkdir()
        (figures / "figure1.jpg").write_bytes(b"fixture-image")
        pipeline = self.pipeline_with_official_compiler("step1-provenance-test")
        store = RunStore.create(self.root / "runs-figures", pipeline, input_manifest=self.root / "manifest.json")
        run_pipeline(store.run_dir, max_stages=2)
        step1 = next(ref for ref in store.load_artifacts() if ref.kind == "formalization" and ref.metadata["step"] == 1)
        formalization = read_json(store.artifact_path(step1))
        anchors = formalization["workflow"]["source_anchors"]
        paper = next(anchor for anchor in anchors if anchor["anchor_id"] == "anchor_paper_text")
        figure = next(anchor for anchor in anchors if anchor["source_kind"] == "paper.figure")
        self.assertEqual({"type": "markdown_span", "start_line": 1, "end_line": 3}, paper["locator"])
        self.assertEqual({"type": "figure_region", "figure": "figure1.jpg", "bbox": [0, 0, 1, 1]}, figure["locator"])

    def test_optional_semantic_tool_is_audited_and_merged(self) -> None:
        pipeline = self.pipeline_with_official_compiler("generic-tool-backed-test")
        pipeline["stages"][2]["options"] = {"tool_plugin": "tests.test_automated_workflow:FakeSemanticTool"}
        store = RunStore.create(self.root / "runs-tool", pipeline, input_manifest=self.root / "manifest.json")
        run = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", run.status)
        responses = [artifact for artifact in store.load_artifacts() if artifact.kind == "tool.semantic_review.response"]
        self.assertEqual(1, len(responses))
        response = read_json(store.artifact_path(responses[0]))
        self.assertTrue(response["response"]["raw"]["raw_test_payload"])
        self.assertEqual("succeeded", response["response"]["status"])

    def test_step2_mechanically_anchors_claims_and_packages_figure_context(self) -> None:
        (self.root / "paper.md").write_text(
            "[#intro] Background that does not describe the measured effect.\n\n"
            "[#fig2]![](./figures/figure2.jpg)\n\n"
            "[#result] The measured experimental effect is consistently present in every evaluated sample (Figure 2).\n\n"
            "[#after] Follow-up discussion.", encoding="utf-8",
        )
        figures = self.root / "figures"
        figures.mkdir()
        (figures / "figure2.jpg").write_bytes(b"fixture-image")
        pipeline = self.pipeline_with_official_compiler("step2-anchor-test")
        pipeline["stages"][2]["options"] = {"tool_plugin": f"{__name__}:CaptureStep2Tool"}
        CaptureStep2Tool.request = None
        store = RunStore.create(self.root / "runs-step2-anchor", pipeline, input_manifest=self.root / "manifest.json")
        run = run_pipeline(store.run_dir, max_stages=3)
        self.assertEqual("waiting", run.status)
        step2 = max((ref for ref in store.load_artifacts() if ref.kind == "formalization"), key=lambda ref: ref.metadata["step"])
        formalization = read_json(store.artifact_path(step2))
        claim = next(item for item in formalization["graph"]["knowledges"] if item["id"] == "claim_A1")
        self.assertIn("anchor_paragraph_result", claim["source_anchor_ids"])
        candidates = CaptureStep2Tool.request.parameters["experiment_candidates"]
        self.assertEqual(1, len(candidates))
        self.assertEqual(["anchor_paragraph_intro", "anchor_paragraph_fig2", "anchor_paragraph_result", "anchor_paragraph_after"], [item["anchor_id"] for item in candidates[0]["paragraphs"]])

    def test_vision_response_creates_distinct_observation_proposals(self) -> None:
        request = ToolCallRequest(
            "call-1", "deepseek-v4-flash-vision-exp", "1", "review_step_2", [],
            {"source_anchor_ids": ["anchor_paragraph_result"], "experiment_candidates": []},
        )
        candidates = [{"figure": {"source_anchor_ids": ["anchor_figure_2"]}, "paragraphs": []}]
        raw = {"choices": [{"message": {"content": """{
          \"status\": \"claims_extracted\",
          \"claims\": [
            {\"S\": \"setting two\", \"A\": \"alternate ticket\", \"B\": \"random ticket\", \"M\": \"accuracy\", \"R\": \"lower\", \"content\": \"The alternate ticket had lower accuracy than the random ticket in setting two.\", \"paragraph_anchor_ids\": [\"anchor_paragraph_result\"], \"figure_anchor_ids\": [\"anchor_figure_2\"]},
            {\"S\": \"setting one\", \"A\": \"ticket\", \"B\": \"random ticket\", \"M\": \"accuracy\", \"R\": \"higher\", \"content\": \"In setting one, the ticket achieved higher accuracy than the random ticket.\", \"paragraph_anchor_ids\": [\"anchor_paragraph_result\"], \"figure_anchor_ids\": [\"anchor_figure_2\"]}
          ]
        }"""}}]}
        normalized = DeepSeekFlashVisionTool()._normalize(raw, request, candidates)
        proposals = normalized["snapshot_patch"]["knowledge"]
        self.assertEqual(2, len(proposals))
        self.assertEqual(["claim_O01", "claim_O02"], [item["id"] for item in proposals])
        self.assertTrue(all("role" + "s" not in item for item in proposals))
        self.assertTrue(all(item["source_anchor_ids"] == ["anchor_paragraph_result", "anchor_figure_2"] for item in proposals))
        self.assertEqual(
            "In setting one, the ticket achieved higher accuracy than the random ticket.",
            proposals[0]["content"]["canonical"],
        )

    def test_vision_prompt_defines_claim_fields_and_preserves_evidence_semantics(self) -> None:
        image = self.root / "figure2.png"
        image.write_bytes(b"fixture-image")
        tool = DeepSeekFlashVisionTool()
        tool.bind_artifacts({"artifact-figure2": (image, "image/png")})
        candidate = {
            "figure": {
                "artifact_id": "artifact-figure2",
                "label": "Figure 2",
                "source_anchor_ids": ["anchor_figure_2"],
            },
            "paragraphs": [{"anchor_id": "anchor_paragraph_result", "text": "Reported result."}],
        }
        prompt = tool._candidate_content(candidate)[0]["text"]
        self.assertIn("S (Setting) is the experimental setting, scope, or range", prompt)
        self.assertIn("A (Action) is the treatment, intervention, method, or experimental object", prompt)
        self.assertIn("B (Baseline) is the baseline or control", prompt)
        self.assertIn("M (Measure) is the measured metric", prompt)
        self.assertIn("R (Result) is only the observed numerical or directional outcome", prompt)
        self.assertIn("U (Uncertainty) is explicitly reported uncertainty information", prompt)
        self.assertIn("A change in pruning fraction or range is a change in setting", prompt)
        self.assertIn("for grammar and fluency", prompt)
        self.assertIn("do not concatenate fields into a fixed template", prompt)
        self.assertIn("must not add, remove, generalize, narrow, reverse, combine", prompt)

    def test_vision_insufficient_context_returns_mechanical_gate(self) -> None:
        request = ToolCallRequest("call-2", "deepseek-v4-flash-vision-exp", "1", "review_step_2", [], {"source_anchor_ids": []})
        raw = {"choices": [{"message": {"content": '{"status":"insufficient_context","needed_context":"Need the figure caption."}'}}]}
        normalized = DeepSeekFlashVisionTool()._normalize(raw, request, [])
        issue = normalized["snapshot_patch"]["review"]["issues"][0]
        self.assertEqual("EXPERIMENT_CONTEXT_INSUFFICIENT", issue["code"])

    def test_relation_weakpoint_is_preserved_through_step4(self) -> None:
        pipeline = self.pipeline_with_official_compiler("generic-confirmed-deduction-test")
        pipeline["stages"][3]["options"] = {"tool_plugin": "tests.test_automated_workflow:ConfirmDeductionTool"}
        store = RunStore.create(self.root / "runs-confirmed", pipeline, input_manifest=self.root / "manifest.json")
        run = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", run.status)
        formalization_ref = max((artifact for artifact in store.load_artifacts() if artifact.kind == "formalization"), key=lambda artifact: artifact.metadata["step"])
        formalization = read_json(store.artifact_path(formalization_ref))
        self.assertEqual([], formalization["graph"]["strategies"])
        proposal = next(item for item in formalization["workflow"]["proposals"] if item["id"] == "weakpoint_R1")
        self.assertEqual(["claim_A1"], proposal["payload"]["evidence_claim_ids"])
        self.assertEqual("claim_A2", proposal["payload"]["target_claim_id"])

    def test_step3_mechanically_parses_fixed_expression_operators_and_evidence(self) -> None:
        assertions = [
            {"id": f"A{index}", "number": index, "text_en": f"Claim {index}."}
            for index in range(1, 6)
        ]
        atomic_write_json(self.root / "claims.json", {"assertions": assertions, "relations": [
            {"id": "R_and", "connects": [1, 2, 3], "expression": "([1] 且 非 [2]) 推出 [3]"},
            {"id": "R_or", "connects": [1, 2], "expression": "[1] 或 [2]"},
            {"id": "R_equiv", "connects": [3, 4], "expression": "[3] 等价 [4]"},
            {"id": "R_contra", "connects": [4, 5], "expression": "[4] 不可同时成立 [5]"},
            {"id": "R_evidence", "connects": [2, 5], "expression": "[2] 是 [5] 的例子或证据"},
        ]})
        pipeline = self.pipeline_with_official_compiler("step3-fixed-expression-test")
        store = RunStore.create(self.root / "runs-step3-fixed-expression", pipeline, input_manifest=self.root / "manifest.json")
        run = run_pipeline(store.run_dir, max_stages=4)
        self.assertEqual("waiting", run.status)
        step3 = max((ref for ref in store.load_artifacts() if ref.kind == "formalization"), key=lambda ref: ref.metadata["step"])
        formalization = read_json(store.artifact_path(step3))
        self.assertEqual([], formalization["workflow"]["non_reasoning_links"])
        self.assertEqual({"conjunction", "disjunction", "negation", "equivalence", "contradiction"}, {item["type"] for item in formalization["graph"]["operators"]})
        proposals = {item["id"]: item for item in formalization["workflow"]["proposals"]}
        self.assertEqual({"weakpoint_R_and", "weakpoint_R_evidence"}, set(proposals))
        self.assertEqual(["claim_A1", "claim_A2"], proposals["weakpoint_R_and"]["payload"]["evidence_claim_ids"])
        self.assertEqual("claim_A3", proposals["weakpoint_R_and"]["payload"]["target_claim_id"])
        self.assertEqual("([1] 且 非 [2]) 推出 [3]", proposals["weakpoint_R_and"]["payload"]["expression"])

    def test_official_compiler_plugin_promotes_candidate_to_gaia_ir(self) -> None:
        pipeline = self.pipeline_with_official_compiler("generic-official-compiler-test")
        store = RunStore.create(self.root / "runs-compiler", pipeline, input_manifest=self.root / "manifest.json")
        run = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", run.status)
        artifacts = store.load_artifacts()
        official = [artifact for artifact in artifacts if artifact.kind == "gaia.ir"]
        self.assertEqual(1, len(official))
        self.assertEqual("official", official[0].metadata["contract_status"])
        self.assertEqual("ir-v1+official-test", official[0].metadata["ir_schema"])

    def test_official_compiler_output_fails_when_gaia_validator_is_unavailable(self) -> None:
        pipeline = self.pipeline_with_official_compiler("generic-missing-gaia-validator-test")
        store = RunStore.create(self.root / "runs-missing-validator", pipeline, input_manifest=self.root / "manifest.json")
        with patch(
            "pipeline_harness.domain.compiler._validate_with_official_gaia",
            side_effect=RuntimeError("official Gaia package is unavailable"),
        ):
            run = run_pipeline(store.run_dir)
        self.assertEqual("failed", run.status)
        artifacts = store.load_artifacts()
        self.assertFalse(any(ref.kind in {"gaia.ir", "knowledge.index"} for ref in artifacts))
        self.assertTrue(any(finding["code"] == "GAIA_COMPILER_OUTPUT_INVALID" for finding in run.findings))

    def test_builtin_pipeline_waits_for_explicit_official_compiler(self) -> None:
        store = RunStore.create(
            self.root / "runs-no-compiler",
            AUTOMATED_FORMALIZATION_PIPELINE,
            input_manifest=self.root / "manifest.json",
        )
        run = run_pipeline(store.run_dir)
        self.assertEqual("waiting", run.status)
        artifacts = store.load_artifacts()
        self.assertFalse(any(ref.kind in {"gaia.ir", "gaia.ir.candidate", "knowledge.index"} for ref in artifacts))
        self.assertTrue(any(finding["code"] == "GAIA_COMPILER_REQUIRED" for finding in run.findings))

    def test_invalid_edge_is_reported_and_projected_by_edge_id(self) -> None:
        pipeline = copy.deepcopy(AUTOMATED_FORMALIZATION_PIPELINE)
        pipeline["pipeline_id"] = "invalid-edge-validation-test"
        pipeline["stages"][2]["options"] = {"tool_plugin": "tests.test_automated_workflow:InvalidEdgeTool"}
        store = RunStore.create(self.root / "runs-invalid-edge", pipeline, input_manifest=self.root / "manifest.json")
        run = run_pipeline(store.run_dir)
        self.assertEqual("failed", run.status)
        artifacts = store.load_artifacts()
        invalid = [artifact for artifact in artifacts if artifact.kind == "formalization.invalid"]
        self.assertEqual(1, len(invalid))
        validation_ref = max((artifact for artifact in artifacts if artifact.kind == "formalization.validation"), key=lambda artifact: artifact.metadata["step"])
        report = read_json(store.artifact_path(validation_ref))
        self.assertEqual("failed", report["summary"]["status"])
        broken = [finding for finding in report["findings"] if finding["target"] == {"type": "edge", "id": "broken_edge"}]
        self.assertTrue(broken)
        self.assertTrue(broken[0]["location"]["json_pointer"].startswith("/non_reasoning_links/"))
        self.assertTrue(broken[0]["location"]["json_pointer"].endswith("/target"))
        view = project_run(store.run_dir)
        projected = [edge for edge in view.edges if edge.get("semantic_id") == "broken_edge"]
        self.assertEqual(1, len(projected))
        self.assertEqual("failed", projected[0]["validation_status"])


if __name__ == "__main__":
    unittest.main()
