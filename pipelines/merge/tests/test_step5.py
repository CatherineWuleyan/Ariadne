import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec("gaia"), "official Gaia runtime is required")
class Step5CompilerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
