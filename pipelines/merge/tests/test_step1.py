from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json


def _ir(namespace: str, name: str, claim: str) -> dict:
    qid = f"{namespace}:{name}::{claim}"
    return {
        "namespace": namespace,
        "package_name": name,
        "scope": "local",
        "ir_hash": "sha256:" + "0" * 64,
        "knowledges": [{
            "id": qid,
            "label": claim,
            "content": f"content for {name}",
            "metadata": {"source_knowledge_id": claim},
        }],
        "operators": [],
        "strategies": [],
        "composes": [],
        "formula_graphs": [],
    }


def _config(mode: str, selection: list[str]) -> dict:
    return {
        "pipeline_id": f"pipeline-merge-step1-{mode}",
        "version": "0.1.0",
        "stages": [
            {
                "name": "step0_select_scope",
                "plugin": "pipeline_merge.step0:Step0SelectScopePlugin",
                "options": {
                    "mode": mode,
                    "scope": {"domain": "test-domain", "selection": selection},
                },
            },
            {
                "name": "step1_freeze_inputs",
                "plugin": "pipeline_merge.step1:Step1FreezeInputsPlugin",
                "options": {},
            },
        ],
    }


class Step1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        atomic_write_json(self.root / "a.json", _ir("papers", "paper_a", "claim_a"))
        atomic_write_json(self.root / "b.json", _ir("papers", "paper_b", "claim_b"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manifest(self, *items: dict) -> Path:
        path = self.root / "manifest.json"
        atomic_write_json(path, {"artifacts": list(items)})
        return path

    def _run(self, config: dict, manifest: Path):
        store = RunStore.create(self.root / "runs", config, input_manifest=manifest)
        validator = patch(
            "pipeline_merge.step1._validate_with_official_gaia",
            side_effect=lambda payload: (payload, "ir-v1+official-test"),
        )
        validator.start()
        self.addCleanup(validator.stop)
        return store, run_pipeline(store.run_dir)

    def test_bootstrap_accepts_only_required_gaia_ir_inputs(self) -> None:
        manifest = self._manifest(
            {"path": "a.json", "kind": "gaia.ir"},
            {"path": "b.json", "kind": "gaia.ir"},
        )
        store, run = self._run(
            _config("bootstrap", ["papers:paper_a", "papers:paper_b"]), manifest
        )
        self.assertEqual("succeeded", run.status)
        self.assertEqual(2, run.next_stage_index)
        self.assertEqual(2, len(store.list_checkpoints()))
        self.assertEqual([], store.list_checkpoints()[-1].artifacts)
        self.assertEqual(2, len(store.load_artifacts()))

    def test_incremental_treats_selected_package_as_delta(self) -> None:
        manifest = self._manifest(
            {"path": "a.json", "kind": "gaia.ir"},
            {"path": "b.json", "kind": "gaia.ir"},
        )
        _, run = self._run(_config("incremental", ["paper_b"]), manifest)
        self.assertEqual("succeeded", run.status)

    def test_optional_formalization_can_supply_legacy_paper_id_alias(self) -> None:
        formalization = {
            "package": {
                "namespace": "papers",
                "name": "paper_a",
                "paper_id": "1001",
            }
        }
        atomic_write_json(self.root / "formalization.json", formalization)
        manifest = self._manifest(
            {"path": "a.json", "kind": "gaia.ir"},
            {"path": "b.json", "kind": "gaia.ir"},
            {"path": "formalization.json", "kind": "formalization"},
        )
        with patch("pipeline_merge.step1._validate_compiler_input", return_value=True):
            _, run = self._run(
                _config("bootstrap", ["1001", "papers:paper_b"]), manifest
            )
        self.assertEqual("succeeded", run.status)

    def test_optional_knowledge_index_is_bound_by_existing_content(self) -> None:
        index = {
            "schema_name": "gaia.knowledge.index",
            "schema_version": "1.0.0",
            "source": {"snapshot_id": "revision-a", "step": 4, "snapshot_hash": "x"},
            "entries": [{
                "knowledge_id": "claim_a",
                "canonical_text": "content for paper_a",
                "first_seen_step": 1,
                "current_step": 4,
                "visibility": "public",
                "incoming_reasoning": [],
                "outgoing_reasoning": [],
                "non_reasoning_links": [],
                "source_anchor_ids": [],
            }],
            "external_id_map": {},
            "source_anchors": [],
        }
        atomic_write_json(self.root / "index.json", index)
        manifest = self._manifest(
            {"path": "a.json", "kind": "gaia.ir"},
            {"path": "b.json", "kind": "gaia.ir"},
            {"path": "index.json", "kind": "knowledge.index"},
        )
        _, run = self._run(
            _config("bootstrap", ["papers:paper_a", "papers:paper_b"]), manifest
        )
        self.assertEqual("succeeded", run.status)

        index["entries"][0]["canonical_text"] = "content from another package"
        atomic_write_json(self.root / "index.json", index)
        manifest = self._manifest(
            {"path": "a.json", "kind": "gaia.ir"},
            {"path": "b.json", "kind": "gaia.ir"},
            {"path": "index.json", "kind": "knowledge.index"},
        )
        _, run = self._run(
            _config("bootstrap", ["papers:paper_a", "papers:paper_b"]), manifest
        )
        self.assertEqual("failed", run.status)
        self.assertEqual("STEP1_OPTIONAL_BINDING_INVALID", run.findings[-1]["code"])

    def test_incremental_requires_an_unselected_baseline_package(self) -> None:
        manifest = self._manifest({"path": "b.json", "kind": "gaia.ir"})
        _, run = self._run(_config("incremental", ["papers:paper_b"]), manifest)
        self.assertEqual("failed", run.status)
        self.assertEqual("STEP1_MODE_INPUT_MISMATCH", run.findings[-1]["code"])

    def test_missing_gaia_ir_fails_even_when_optional_input_exists(self) -> None:
        atomic_write_json(self.root / "index.json", {"schema_name": "not-an-index"})
        manifest = self._manifest({"path": "index.json", "kind": "knowledge.index"})
        _, run = self._run(
            _config("bootstrap", ["papers:paper_a", "papers:paper_b"]), manifest
        )
        self.assertEqual("failed", run.status)
        self.assertEqual("STEP1_GAIA_IR_REQUIRED", run.findings[-1]["code"])

    def test_tampered_frozen_input_is_rejected(self) -> None:
        manifest = self._manifest(
            {"path": "a.json", "kind": "gaia.ir"},
            {"path": "b.json", "kind": "gaia.ir"},
        )
        store = RunStore.create(
            self.root / "runs",
            _config("bootstrap", ["papers:paper_a", "papers:paper_b"]),
            input_manifest=manifest,
        )
        ref = store.load_artifacts()[0]
        store.artifact_path(ref).write_text("{}\n", encoding="utf-8")
        with patch(
            "pipeline_merge.step1._validate_with_official_gaia",
            side_effect=lambda payload: (payload, "ir-v1+official-test"),
        ):
            run = run_pipeline(store.run_dir)
        self.assertEqual("failed", run.status)
        self.assertEqual("STEP1_INPUT_HASH_MISMATCH", run.findings[-1]["code"])

    def test_official_validator_is_required(self) -> None:
        manifest = self._manifest(
            {"path": "a.json", "kind": "gaia.ir"},
            {"path": "b.json", "kind": "gaia.ir"},
        )
        store = RunStore.create(
            self.root / "runs",
            _config("bootstrap", ["papers:paper_a", "papers:paper_b"]),
            input_manifest=manifest,
        )
        with patch(
            "pipeline_merge.step1._validate_with_official_gaia",
            side_effect=RuntimeError("official Gaia package is unavailable"),
        ):
            run = run_pipeline(store.run_dir)
        self.assertEqual("failed", run.status)
        self.assertEqual("GAIA_VALIDATOR_UNAVAILABLE", run.findings[-1]["code"])

    def test_unresolved_legacy_paper_id_is_not_guessed(self) -> None:
        manifest = self._manifest(
            {"path": "a.json", "kind": "gaia.ir"},
            {"path": "b.json", "kind": "gaia.ir"},
        )
        _, run = self._run(_config("bootstrap", ["1001", "1002"]), manifest)
        self.assertEqual("failed", run.status)
        self.assertEqual("STEP1_SELECTION_UNRESOLVED", run.findings[-1]["code"])


if __name__ == "__main__":
    unittest.main()
