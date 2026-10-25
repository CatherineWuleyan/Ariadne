import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec("gaia"), "official Gaia runtime is required")
class Step5CompilerTests(unittest.TestCase):
    def test_question_chain_accepts_native_and_audited_legacy_deduction_only(self):
        from pipeline_merge_question.question_layer import chain_up_adjacency

        edges = [
            {"id": "native", "source": "a", "target": "b", "semantic_type": "deduction",
             "reasoning_type": "deduction", "chainable": True},
            {"id": "legacy", "source": "b", "target": "c", "semantic_type": "infer"},
            {"id": "residual", "source": "x", "target": "c", "semantic_type": "infer",
             "reasoning_type": "infer", "chainable": False},
            {"id": "fixed", "source": "y", "target": "c", "semantic_type": "conjunction"},
        ]
        up = chain_up_adjacency(edges, {"legacy": {"reasoning_type": "deduction", "chainable": True}})
        self.assertEqual({"b": [("a", "deduction")], "c": [("b", "deduction")]}, dict(up))

    def test_preserves_external_qids_and_localizes_candidate_k(self):
        from pipeline_harness.domain.compiler import _compile_v2_formalization
        from pipelines.single_paper.agent_pipeline_v2.authoring import canonical_strategy

        external = "papers:paper_a::claim_1"
        candidate = "candidate_K_1"
        strategy = canonical_strategy({
            "scope": "local", "type": "deduction", "premises": [external],
            "conclusion": candidate, "background": [],
        })
        document = {
            "schema_version": "1.1.0",
            "revision": {"revision_id": "r4", "supersedes": None, "parent_hash": None, "content_hash": "ignored"},
            "package": {"paper_id": "integration:test", "namespace": "integration", "name": "test", "version": "1"},
            "graph": {"nodes": [external, candidate], "operators": [], "strategies": [strategy], "composes": []},
            "knowledges": {
                external: {"type": "claim", "content": {"canonical": "External paper claim."},
                           "source_anchor_ids": ["a1"], "metadata": {"package_provenance": ["papers:paper_a"]}},
                candidate: {"type": "claim", "content": {"canonical": "Bounded integration conclusion."},
                            "source_anchor_ids": []},
            },
            "workflow": {"source_records": [], "source_anchors": [], "weakpoints": [], "gaps": [],
                         "non_reasoning_links": [], "revisions": []},
        }
        compiled, _ = _compile_v2_formalization(document, namespace="integration", package_name="test")
        by_id = {item["id"]: item for item in compiled["knowledges"]}
        self.assertIn(external, by_id)
        self.assertIn("integration:test::candidate_K_1", by_id)
        self.assertNotIn("integration:test::papers:paper_a::claim_1", by_id)
        self.assertEqual(["papers:paper_a"], by_id[external]["metadata"]["package_provenance"])
        self.assertEqual(external, compiled["strategies"][0]["premises"][0])
        self.assertEqual("integration:test::candidate_K_1", compiled["strategies"][0]["conclusion"])

        # Step 6 sees the compiled checkpoint, not the Step 4 authoring file.
        # Its projection and answer chains must still retain the named type and
        # the source IDs that the final all-artifact view uses.
        from pipeline_harness.plugins import StageContext
        from pipeline_harness.store import RunStore, read_json
        from pipeline_merge_question.step6_question_layer import Step6QuestionLayerPlugin
        from pipeline_merge_question.view import MergeViewAdapter

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RunStore.create(root / "runs", {
                "pipeline_id": "question-checkpoint-test", "version": "1",
                "stages": [{"name": "step6_question_answer_layer",
                            "plugin": "pipeline_merge_question.step6_question_layer:Step6QuestionLayerPlugin"}],
            })
            paper_path = root / "paper.json"
            paper_path.write_text(json.dumps({
                "namespace": "papers", "package_name": "paper_a",
                "knowledges": [{"id": external, "content": "External paper claim.", "type": "claim"}],
                "operators": [], "strategies": [],
            }))
            paper_ref = store.register_file(paper_path, kind="gaia.ir", media_type="application/json", producer_stage="input")
            compiled_path = root / "compiled.json"
            compiled_path.write_text(json.dumps(compiled))
            compiled_ref = store.register_file(compiled_path, kind="gaia.ir", media_type="application/json",
                                               producer_stage="step5_compile_gaia_ir")
            authoring_path = root / "authoring.json"
            authoring_path.write_text(json.dumps(document))
            authoring_ref = store.register_file(authoring_path, kind="formalization", media_type="application/json",
                                                producer_stage="step4_formalize_integration")
            checkpoint_refs = [paper_ref, compiled_ref]
            projected = MergeViewAdapter().project(store, checkpoint_refs)
            full = MergeViewAdapter().project(store, [*checkpoint_refs, authoring_ref])
            signature = lambda view: {(edge["source"], edge["target"], edge["reasoning_type"])
                                      for edge in view.edges if edge.get("chainable")}
            self.assertEqual(signature(full), signature(projected))
            self.assertEqual(1, projected.metadata["chainable_edge_count"])
            self.assertIn("knowledge:candidate_K_1", {node["id"] for node in projected.nodes})

            fixture = root / "answers.json"
            fixture.write_text(json.dumps({"roots": [{"code": "R1", "statement": "A bounded answer."}],
                                           "links": {"R1": [{"claim_id": "knowledge:candidate_K_1",
                                                              "relation": "support", "reason": "Direct conclusion."}]}}))
            context = StageContext(run_id=store.load_run().run_id, run_dir=store.run_dir,
                                   work_dir=store.run_dir / "work", stage_name="step6_question_answer_layer",
                                   attempt=1, inputs=checkpoint_refs, options={"answers_fixture": str(fixture)})
            result = Step6QuestionLayerPlugin().run(context)
            self.assertEqual("succeeded", result.status, result.findings)
            layer = read_json(next(draft.path for draft in result.artifacts if draft.kind == "question.answer_layer"))
            self.assertEqual(1, layer["chainable_edge_count"])
            self.assertEqual(1, layer["roots"][0]["stats"]["chain_count"])


if __name__ == "__main__":
    unittest.main()
