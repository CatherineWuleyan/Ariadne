from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_pipeline_v2.authoring import content_hash, validate
from agent_pipeline_v2.step1 import CLAIMS_FINAL_KIND, _relation_endpoints
from agent_pipeline_v2.compiler_projection import project_for_official_compiler
from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json, read_json
from pipeline_harness.view.projector import project_run


class Step1Tests(unittest.TestCase):
    def test_imports_all_claims_notes_and_relations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "paper_text.md").write_text("# Paper\n\nText.\n", encoding="utf-8")
            atomic_write_json(root / "claims_final.json", {
                "claim": [
                    {"number": 1, "conclusion": "c1", "text": "First conclusion.", "is_pure_data": True},
                    {"number": 2, "conclusion": "c1", "text": "Second conclusion."},
                    {"number": 3, "conclusion": "c1", "text": "Third conclusion."},
                    {"number": 4, "conclusion": "c1", "text": "Fourth conclusion."},
                ],
                "note": [{"number": 1, "conclusion": "c1", "text": "A note."}],
                "relation": [
                    {"conclusion": "c1", "connects": [4, 2, 3], "expression": "([3] 且 [2]) 推出 [4]"},
                    {"conclusion": "c1", "connects": [2, 1], "expression": "[1] 是 [2] 的例子或证据"},
                ],
            })
            atomic_write_json(root / "manifest.json", {"artifacts": [
                {"path": "paper_text.md", "kind": "source.paper_text", "media_type": "text/markdown"},
                {"path": "claims_final.json", "kind": CLAIMS_FINAL_KIND, "media_type": "application/json"},
            ]})
            pipeline = json.loads((Path(__file__).parents[1] / "pipeline.step1.json").read_text(encoding="utf-8"))
            store = RunStore.create(root / "runs", pipeline, input_manifest=root / "manifest.json")
            self.assertEqual("succeeded", run_pipeline(store.run_dir).status)
            formalization_ref = next(ref for ref in store.load_artifacts() if ref.kind == "formalization")
            formalization = read_json(store.artifact_path(formalization_ref))
            self.assertEqual("1.1.0", formalization["schema_version"])
            self.assertNotIn("proposals", formalization["workflow"])
            self.assertEqual([], formalization["workflow"]["weakpoints"])
            self.assertEqual([], formalization["workflow"]["revisions"])
            self.assertEqual({"claim_1", "claim_2", "claim_3", "claim_4", "note_1"}, set(formalization["knowledges"]))
            self.assertEqual("observation_claim", formalization["knowledges"]["claim_1"]["type"])
            self.assertEqual(["claim_1", "claim_2", "claim_3", "claim_4"], formalization["graph"]["nodes"])
            paper_anchors = [
                anchor for anchor in formalization["workflow"]["source_anchors"]
                if anchor["source_kind"] == "source.paper_text"
            ]
            self.assertEqual(["anchor_paragraph_p0001", "anchor_paragraph_p0002"], [item["anchor_id"] for item in paper_anchors])
            links = formalization["workflow"]["non_reasoning_links"]
            self.assertEqual(2, len(links))
            self.assertEqual((['claim_3', 'claim_2'], 'claim_4'), (links[0]["sources"], links[0]["target"]))
            # Notes are non-probabilistic background, not formal graph nodes.
            self.assertNotIn("note_1", formalization["graph"]["nodes"])
            projected = project_for_official_compiler(formalization)
            self.assertEqual(["claim_1", "claim_2", "claim_3", "claim_4"], [item["id"] for item in projected["graph"]["knowledges"]])
            view = project_run(store.run_dir)
            self.assertEqual(6, len(view.nodes))
            self.assertEqual(5, len(view.edges))
            relation_node = next(node for node in view.nodes if node["kind"] == "relation_display")
            self.assertTrue(relation_node["details"]["display_only"])
            viewer = (store.run_dir / "views" / "viewer.html").read_text(encoding="utf-8")
            self.assertIn(".node.relation-display .node-shape", viewer)

    def test_relation_direction_comes_from_expression_not_connects_order(self) -> None:
        known = {1, 2, 3, 4}
        self.assertEqual(
            [([2, 1], 4)],
            _relation_endpoints(
                {"connects": [4, 1, 2], "expression": "([2] 且 [1]) 推出 [4]"},
                known,
            ),
        )
        self.assertEqual(
            [([1], 2), ([1], 3)],
            _relation_endpoints(
                {"connects": [3, 1, 2], "expression": "[1] 是 ([2] 和 [3]) 的例子或证据"},
                known,
            ),
        )
        self.assertEqual(
            [([1], 3)],
            _relation_endpoints(
                {"connects": [3, 1], "expression": "[3] 矛盾 [1]"},
                known,
            ),
        )

    def test_relation_direction_rejects_contract_drift(self) -> None:
        with self.assertRaisesRegex(ValueError, "do not match connects"):
            _relation_endpoints(
                {"connects": [1, 3], "expression": "[1] 推出 [2]"},
                {1, 2, 3},
            )
        with self.assertRaisesRegex(ValueError, "unsupported relation expression"):
            _relation_endpoints(
                {"connects": [1, 2], "expression": "[1] supports [2]"},
                {1, 2},
            )

    def test_validates_a_weakpoint_without_a_relation_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "paper_text.md").write_text("# Paper\n\nText.\n", encoding="utf-8")
            atomic_write_json(root / "claims_final.json", {
                "claim": [
                    {"number": 1, "conclusion": "c1", "text": "First claim."},
                    {"number": 2, "conclusion": "c1", "text": "Second claim."},
                ],
                "note": [],
                "relation": [],
            })
            atomic_write_json(root / "manifest.json", {"artifacts": [
                {"path": "paper_text.md", "kind": "source.paper_text", "media_type": "text/markdown"},
                {"path": "claims_final.json", "kind": CLAIMS_FINAL_KIND, "media_type": "application/json"},
            ]})
            pipeline = json.loads((Path(__file__).parents[1] / "pipeline.step1.json").read_text(encoding="utf-8"))
            store = RunStore.create(root / "runs", pipeline, input_manifest=root / "manifest.json")
            self.assertEqual("succeeded", run_pipeline(store.run_dir).status)
            formalization_ref = next(ref for ref in store.load_artifacts() if ref.kind == "formalization")
            formalization = read_json(store.artifact_path(formalization_ref))
            formalization["workflow"]["weakpoints"] = [{
                "id": "weakpoint_1",
                "payload": {
                    "evidence_claim_ids": ["claim_1"],
                    "target_claim_id": "claim_2",
                    "reasoning_type": None,
                    "evidence_anchor_ids": ["anchor_claim_1"],
                    "expression": "[claim_1] 推出 [claim_2]",
                },
            }]
            formalization["revision"]["content_hash"] = content_hash(formalization)
            validate(formalization)

            formalization["workflow"]["proposals"] = []
            formalization["revision"]["content_hash"] = content_hash(formalization)
            with self.assertRaisesRegex(ValueError, "workflow fields"):
                validate(formalization)

    def test_rejects_relation_with_unknown_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "paper_text.md").write_text("[#paper] Paper text.\n", encoding="utf-8")
            atomic_write_json(root / "claims_final.json", {
                "claim": [{"number": 1, "conclusion": "c1", "text": "First conclusion."}],
                "note": [], "relation": [{"conclusion": "c1", "connects": [1, 2], "expression": "[1] implies [2]"}],
            })
            atomic_write_json(root / "manifest.json", {"artifacts": [
                {"path": "paper_text.md", "kind": "source.paper_text", "media_type": "text/markdown"},
                {"path": "claims_final.json", "kind": CLAIMS_FINAL_KIND, "media_type": "application/json"},
            ]})
            pipeline = json.loads((Path(__file__).parents[1] / "pipeline.step1.json").read_text(encoding="utf-8"))
            store = RunStore.create(root / "runs", pipeline, input_manifest=root / "manifest.json")
            self.assertEqual("failed", run_pipeline(store.run_dir).status)

    def test_resolves_internal_claim_references_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "paper_text.md").write_text("# Paper\n\nText.\n", encoding="utf-8")
            atomic_write_json(root / "claims_final.json", {
                "claim": [
                    {"number": 1, "conclusion": "c1", "text": "The base effect holds."},
                    {"number": 2, "conclusion": "c2", "text": "The transfer in claim 1 holds in setting B."},
                    {"number": 3, "conclusion": "c3", "text": "The result in Claim 2 also holds in setting C."},
                ],
                "note": [],
                "relation": [],
            })
            atomic_write_json(root / "manifest.json", {"artifacts": [
                {"path": "paper_text.md", "kind": "source.paper_text", "media_type": "text/markdown"},
                {"path": "claims_final.json", "kind": CLAIMS_FINAL_KIND, "media_type": "application/json"},
            ]})
            pipeline = json.loads((Path(__file__).parents[1] / "pipeline.step1.json").read_text(encoding="utf-8"))
            store = RunStore.create(root / "runs", pipeline, input_manifest=root / "manifest.json")
            self.assertEqual("succeeded", run_pipeline(store.run_dir).status)
            formalization_ref = next(ref for ref in store.load_artifacts() if ref.kind == "formalization")
            formalization = read_json(store.artifact_path(formalization_ref))
            claim_3 = formalization["knowledges"]["claim_3"]
            self.assertEqual(
                ["anchor_claim_3", "anchor_claim_2", "anchor_claim_1"],
                claim_3["source_anchor_ids"],
            )
            self.assertIn("[Referenced claim 2: The transfer in claim 1 holds in setting B.]", claim_3["content"]["canonical"])
            self.assertIn("[Referenced claim 1: The base effect holds.]", claim_3["content"]["canonical"])

    def test_rejects_unknown_internal_claim_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "paper_text.md").write_text("# Paper\n\nText.\n", encoding="utf-8")
            atomic_write_json(root / "claims_final.json", {
                "claim": [{"number": 1, "conclusion": "c1", "text": "The effect in claim 9 holds."}],
                "note": [],
                "relation": [],
            })
            atomic_write_json(root / "manifest.json", {"artifacts": [
                {"path": "paper_text.md", "kind": "source.paper_text", "media_type": "text/markdown"},
                {"path": "claims_final.json", "kind": CLAIMS_FINAL_KIND, "media_type": "application/json"},
            ]})
            pipeline = json.loads((Path(__file__).parents[1] / "pipeline.step1.json").read_text(encoding="utf-8"))
            store = RunStore.create(root / "runs", pipeline, input_manifest=root / "manifest.json")
            self.assertEqual("failed", run_pipeline(store.run_dir).status)


if __name__ == "__main__":
    unittest.main()
