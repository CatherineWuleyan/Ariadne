from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json, read_json
from pipeline_merge.step2 import _build_groups


def _ir(name: str, claims: list[tuple[str, str]], operator: dict | None = None) -> dict:
    knowledges = [
        {
            "id": f"papers:{name}::{claim_id}",
            "label": claim_id,
            "content": content,
            "metadata": {"source_knowledge_id": claim_id},
        }
        for claim_id, content in claims
    ]
    operators = []
    if operator is not None:
        operators.append({
            "operator_id": f"op_{name}",
            "operator": operator["operator"],
            "variables": [f"papers:{name}::{value}" for value in operator["variables"]],
            "conclusion": f"papers:{name}::{operator['conclusion']}",
        })
    return {
        "namespace": "papers",
        "package_name": name,
        "scope": "local",
        "ir_hash": "sha256:" + "0" * 64,
        "knowledges": knowledges,
        "operators": operators,
        "strategies": [],
        "composes": [],
        "formula_graphs": [],
    }


def _config(mode: str, selection: list[str]) -> dict:
    return {
        "pipeline_id": "pipeline-merge-step2-test",
        "version": "0.3.0",
        "stages": [
            {
                "name": "step0_select_scope",
                "plugin": "pipeline_merge.step0:Step0SelectScopePlugin",
                "options": {"mode": mode, "scope": {"domain": "test", "selection": selection}},
            },
            {
                "name": "step1_freeze_inputs",
                "plugin": "pipeline_merge.step1:Step1FreezeInputsPlugin",
                "options": {},
            },
            {
                "name": "step2_retrieve_domain_local",
                "plugin": "pipeline_merge.step2:Step2RetrieveDomainLocalPlugin",
                "options": {},
            },
        ],
    }


class Step2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        atomic_write_json(self.root / "new.json", _ir(
            "paper_new",
            [("claim_new", "sparse networks transfer across dataset settings")],
        ))
        atomic_write_json(self.root / "old.json", _ir(
            "paper_old",
            [
                ("claim_old", "sparse networks transfer across model settings"),
                ("claim_extra", "an unrelated observation"),
            ],
            {"operator": "equivalence", "variables": ["claim_old", "claim_extra"], "conclusion": "claim_old"},
        ))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, mode: str, selection: list[str]):
        manifest = self.root / "manifest.json"
        atomic_write_json(manifest, {"artifacts": [
            {"path": "new.json", "kind": "gaia.ir"},
            {"path": "old.json", "kind": "gaia.ir"},
        ]})
        store = RunStore.create(self.root / "runs", _config(mode, selection), input_manifest=manifest)
        with patch(
            "pipeline_merge.step1._validate_with_official_gaia",
            side_effect=lambda payload: (payload, "ir-v1+official-test"),
        ):
            run = run_pipeline(store.run_dir)
        return store, run

    def test_incremental_queries_new_package_only_and_expands_one_hop(self) -> None:
        store, run = self._run("incremental", ["papers:paper_new"])
        self.assertEqual("succeeded", run.status)
        context_ref = next(ref for ref in store.load_artifacts() if ref.kind == "integration.local_context")
        context = read_json(store.artifact_path(context_ref))
        self.assertEqual(["papers:paper_new::claim_new"], [item["qid"] for item in context["query_seeds"]])
        self.assertTrue(context["direct_hits"])
        self.assertTrue(all(item["package"] == "papers:paper_old" for item in context["direct_hits"]))
        self.assertEqual(["papers:paper_new"], context["excluded_query_packages"])
        self.assertTrue(any(item["kind"] == "operator" for item in context["one_hop_nodes"]))
        self.assertTrue(any(item["kind"] == "knowledge" for item in context["one_hop_nodes"]))
        self.assertEqual(1, len(context["groups"]))
        group = context["groups"][0]
        self.assertEqual(["papers:paper_new::claim_new"], group["seed_qids"])
        self.assertIn("papers:paper_old::claim_old", group["proposition_qids"])
        self.assertTrue(group["one_hop_node_ids"])

    def test_bootstrap_does_not_query_same_package(self) -> None:
        store, run = self._run("bootstrap", ["papers:paper_new", "papers:paper_old"])
        self.assertEqual("succeeded", run.status)
        ref = next(ref for ref in store.load_artifacts() if ref.kind == "integration.local_context")
        context = read_json(store.artifact_path(ref))
        for hit in context["direct_hits"]:
            self.assertNotEqual(hit["package"], next(
                seed["package"] for seed in context["query_seeds"] if seed["qid"] in hit["query_seed_qids"]
            ))

    def test_no_cross_package_hit_still_emits_empty_context(self) -> None:
        atomic_write_json(self.root / "old.json", _ir("paper_old", [("claim_old", "completely unrelated words")]))
        store, run = self._run("incremental", ["papers:paper_new"])
        self.assertEqual("succeeded", run.status)
        ref = next(ref for ref in store.load_artifacts() if ref.kind == "integration.local_context")
        context = read_json(store.artifact_path(ref))
        self.assertEqual([], context["direct_hits"])
        self.assertEqual([], context["one_hop_nodes"])
        self.assertEqual([], context["groups"])

    def test_groups_allow_overlap_but_merge_near_duplicates(self) -> None:
        seeds = [
            {"qid": "papers:a::s1", "package": "papers:a"},
            {"qid": "papers:a::s2", "package": "papers:a"},
        ]
        hits = [
            {"qid": "papers:b::h1", "package": "papers:b", "query_seed_qids": ["papers:a::s1", "papers:a::s2"]},
            {"qid": "papers:c::h2", "package": "papers:c", "query_seed_qids": ["papers:a::s1"]},
            {"qid": "papers:d::h3", "package": "papers:d", "query_seed_qids": ["papers:a::s2"]},
        ]
        groups = _build_groups(seeds, hits, [])
        self.assertEqual(2, len(groups))
        proposition_sets = [set(group["proposition_qids"]) for group in groups]
        self.assertTrue(any("papers:b::h1" in values and "papers:c::h2" in values for values in proposition_sets))
        self.assertTrue(any("papers:b::h1" in values and "papers:d::h3" in values for values in proposition_sets))


if __name__ == "__main__":
    unittest.main()
