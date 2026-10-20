from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline_harness.checks import check_run
from pipeline_harness.domain.graph import audit_formalization_connectivity
from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, read_json
from pipeline_harness.synthetic import SYNTHETIC_PIPELINE
from pipeline_harness.view.connectivity import audit_view_connectivity, prune_orphan_nodes
from pipeline_harness.view.model import ViewDocument, search_view
from pipeline_harness.view.projector import project_run


class ViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RunStore.create(self.root, SYNTHETIC_PIPELINE)
        run_pipeline(self.store.run_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_projects_standalone_view_and_searches_unicode(self) -> None:
        view = project_run(self.store.run_dir)
        viewer = self.store.run_dir / "views/viewer.html"
        self.assertTrue(viewer.is_file())
        html = viewer.read_text(encoding="utf-8")
        self.assertNotIn("__VIEW_DATA__", html)
        self.assertIn("SYNTH_NODE_001", html)
        self.assertEqual(3, len(search_view(view, "合成条目")))
        exact = search_view(view, "ＳＹＮＴＨ＿ＮＯＤＥ＿００１")
        self.assertEqual("SYNTH_NODE_001", exact[0]["id"])

    def test_unfilled_template_explains_how_to_generate_a_view(self) -> None:
        template = (
            Path(__file__).parents[1]
            / "pipeline_harness"
            / "view"
            / "viewer.html"
        ).read_text(encoding="utf-8")
        self.assertIn("This is the Viewer template", template)
        self.assertIn("pipeline_harness view", template)
        self.assertIn('<option value="standard" selected>Standard</option>', template)
        self.assertNotIn('<option value="audit">', template)
        self.assertIn("get('dev') === '1'", template)
        self.assertIn("'3':new Set(['claims','weakpoints','operators'])", template)
        self.assertIn("const operators=view.nodes.filter(node => String(node.step) === step && node.kind === 'operator');", template)
        self.assertIn("equivalence:'↔'", template)
        self.assertNotIn("equivalence:'≡'", template)
        self.assertNotIn('<ellipse class="node-shape" cx="${w/2}" cy="${h/2}" rx="${w/2-1}"', template)
        self.assertIn(".edge.equivalence { marker-start", template)
        self.assertIn("function directOperatorEdges", template)
        self.assertNotIn("visual_reorientation:true", template)
        self.assertIn("function layoutConstraintEdges", template)
        self.assertIn("function connectedComponents", template)
        self.assertIn("function componentLayout", template)
        self.assertIn("engine:'dagre-components'", template)
        self.assertIn("function isAlternativePlaceholder", template)
        self.assertIn("eoPair?14:groupsFor(edge).length?8:1", template)
        self.assertNotIn("function relationalLayoutGroups", template)
        self.assertIn("item.kind==='weakpoint'", template)
        self.assertIn("toggleWeakpoint(item)", template)
        self.assertIn('id="detail"', template)
        self.assertIn("propertyRow('reasoning type',reasoningType)", template)

    def test_clean_projection_passes_check(self) -> None:
        project_run(self.store.run_dir)
        self.assertEqual([], check_run(self.store.run_dir))

    def test_check_detects_stale_view_hash(self) -> None:
        project_run(self.store.run_dir)
        view_path = self.store.run_dir / "views/view_model.json"
        payload = read_json(view_path)
        payload["title"] = "tampered"
        view_path.write_text(__import__("json").dumps(payload), encoding="utf-8")
        codes = {finding.code for finding in check_run(self.store.run_dir)}
        self.assertIn("VIEW_HASH_MISMATCH", codes)

    def test_prune_orphan_nodes_drops_terminal_helpers_and_their_dead_edges(self) -> None:
        document = ViewDocument(
            title="connectivity",
            source_artifacts=["artifact_1"],
            nodes=[
                {"id": "k:a", "label": "a", "layer": "claims", "step": 4, "min_granularity": "overview"},
                {"id": "k:b", "label": "b", "layer": "claims", "step": 4, "min_granularity": "overview"},
                {"id": "op:caret", "label": "^", "layer": "operators", "step": 4, "kind": "operator", "min_granularity": "standard"},
                # Terminal formalizer helper: the compiler emits it as a
                # Knowledge node but no operator or strategy consumes it.
                {"id": "k:__equivalence_result_deadbeef", "label": "helper", "layer": "helpers", "step": 4, "min_granularity": "standard"},
            ],
            edges=[
                {
                    "id": "edge:a:op", "source": "k:a", "target": "op:caret",
                    "label": "input", "layer": "operators", "min_granularity": "standard",
                },
            ],
            search_documents=[
                {"id": "search:a", "title": "a", "text": "a", "tags": [], "refs": ["k:a"]},
                {"id": "search:helper", "title": "h", "text": "h", "tags": [], "refs": ["k:__equivalence_result_deadbeef"]},
            ],
            layers=[{"id": "claims", "label": "Claims"}, {"id": "operators", "label": "Operators"}],
        )
        repaired, report = prune_orphan_nodes(document)
        surviving = {node["id"] for node in repaired.nodes}
        self.assertEqual(1, report["removed_helper_nodes"])
        self.assertNotIn("k:__equivalence_result_deadbeef", surviving)
        self.assertEqual(["edge:a:op"], [edge["id"] for edge in repaired.edges])
        self.assertEqual(["search:a"], [entry["id"] for entry in repaired.search_documents])
        # Every isolated node, including a public claim, is hidden from the
        # rendered projection.
        self.assertNotIn("k:b", surviving)
        self.assertEqual([], report["standalone_claim_ids"])
        ids = {node["id"] for node in repaired.nodes}
        self.assertTrue(all(edge["source"] in ids and edge["target"] in ids for edge in repaired.edges))

    def test_prune_orphan_nodes_drops_edges_whose_endpoint_disappeared(self) -> None:
        document = ViewDocument(
            title="cascade",
            source_artifacts=["artifact_1"],
            nodes=[
                {"id": "k:a", "label": "a", "layer": "claims", "step": 4, "min_granularity": "overview"},
                {"id": "k:__disjunction_result_cafe", "label": "helper", "layer": "helpers", "step": 4, "min_granularity": "standard"},
            ],
            edges=[
                {
                    "id": "edge:murky", "source": "k:a", "target": "k:__disjunction_result_cafe",
                    "label": "input", "layer": "operators", "min_granularity": "standard",
                },
            ],
            search_documents=[],
            layers=[{"id": "claims", "label": "Claims"}],
        )
        # A helper with one edge is not an orphan, so nothing is removed.
        _, report = prune_orphan_nodes(document)
        self.assertEqual(0, report["removed_helper_nodes"])
        # But an edge to a node that the projection omitted must be dropped.
        document.nodes.append({"id": "k:ghost-target", "label": "g", "layer": "claims", "step": 4, "min_granularity": "overview"})
        document.edges.append({"id": "edge:ghost", "source": "k:ghost-target", "target": "k:does-not-exist", "label": "input", "layer": "operators", "min_granularity": "standard"})
        repaired, report = prune_orphan_nodes(document)
        self.assertEqual(1, report["removed_edges"])
        self.assertEqual(["edge:murky"], [edge["id"] for edge in repaired.edges])

    def test_audit_view_connectivity_reports_components_and_floating_nodes(self) -> None:
        document = ViewDocument(
            title="audit",
            source_artifacts=["artifact_1"],
            nodes=[
                {"id": "k:a", "label": "a", "layer": "claims", "step": 4, "min_granularity": "overview"},
                {"id": "k:orphan", "label": "orphan", "layer": "claims", "step": 4, "min_granularity": "overview"},
                {"id": "op:1", "label": "^", "layer": "operators", "step": 4, "kind": "operator", "min_granularity": "standard"},
            ],
            edges=[
                {
                    "id": "edge:a:op", "source": "k:a", "target": "op:1",
                    "label": "input", "layer": "operators", "min_granularity": "standard",
                },
            ],
            search_documents=[],
            layers=[{"id": "claims", "label": "Claims"}],
        )
        report = audit_view_connectivity(document, step=4)
        self.assertEqual(1, report["standalone_claim_count"])
        self.assertEqual(["k:orphan"], report["standalone_claim_ids"])
        self.assertEqual(0, report["dangling_operator_count"])
        self.assertEqual(1, report["component_count"])
        self.assertEqual(2, report["largest_component_size"])

    def test_formalization_connectivity_flags_floating_claims(self) -> None:
        document = {
            "graph": {
                "nodes": ["claim_1", "claim_2", "claim_3"],
                "operators": [
                    {"id": "op_1", "type": "equivalence", "variables": ["claim_1", "claim_2"], "conclusion": "h_1"}
                ],
                "strategies": [],
            }
        }
        report = audit_formalization_connectivity(document)
        self.assertEqual(["claim_3"], report["floating_node_ids"])
        self.assertEqual(2, report["component_count"])
        self.assertFalse(report["connected"])
        self.assertFalse(report["healthy"])


if __name__ == "__main__":
    unittest.main()
