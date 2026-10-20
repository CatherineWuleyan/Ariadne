from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from pipeline_harness.checks import check_run
from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json, read_json
from pipeline_harness.synthetic import SYNTHETIC_FAILURE_PIPELINE, SYNTHETIC_PIPELINE


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_runs_synthetic_pipeline_and_is_idempotent_after_success(self) -> None:
        store = RunStore.create(self.root, SYNTHETIC_PIPELINE)
        completed = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", completed.status)
        self.assertEqual(2, completed.next_stage_index)
        self.assertEqual(2, len(store.list_checkpoints()))
        rerun = run_pipeline(store.run_dir)
        self.assertEqual(completed.attempts, rerun.attempts)
        self.assertEqual(2, len(store.list_checkpoints()))

    def test_retries_failed_stage_from_last_checkpoint(self) -> None:
        store = RunStore.create(self.root, SYNTHETIC_FAILURE_PIPELINE)
        failed = run_pipeline(store.run_dir)
        self.assertEqual("failed", failed.status)
        self.assertEqual(1, failed.next_stage_index)
        self.assertEqual(1, len(store.list_checkpoints()))
        artifacts = store.artifact_map()
        diagnostic_ids = [
            artifact_id
            for artifact_id, artifact in artifacts.items()
            if artifact.kind == "synthetic.failure"
        ]
        self.assertEqual(1, len(diagnostic_ids))
        self.assertNotIn(diagnostic_ids[0], store.list_checkpoints()[0].artifacts)
        resumed = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", resumed.status)
        self.assertEqual(2, resumed.attempts["make-graph"])
        self.assertEqual(2, len(store.list_checkpoints()))

    def test_waiting_artifact_is_audit_only_and_resume_retries_stage(self) -> None:
        config = copy.deepcopy(SYNTHETIC_PIPELINE)
        config["pipeline_id"] = "synthetic-waiting"
        config["stages"][1]["options"] = {"wait_once": True}
        store = RunStore.create(self.root, config)
        waiting = run_pipeline(store.run_dir)
        self.assertEqual("waiting", waiting.status)
        self.assertEqual("synthetic_wait_once", waiting.waiting["payload"]["reason"])
        self.assertEqual(1, len(store.list_checkpoints()))
        artifacts = store.artifact_map()
        waiting_ids = [
            artifact_id
            for artifact_id, artifact in artifacts.items()
            if artifact.kind == "synthetic.waiting"
        ]
        self.assertEqual(1, len(waiting_ids))
        self.assertNotIn(waiting_ids[0], store.list_checkpoints()[0].artifacts)

        resumed = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", resumed.status)
        self.assertEqual(2, resumed.attempts["make-graph"])
        self.assertIsNone(resumed.waiting)
        checkpoint_artifacts = {
            artifact_id
            for checkpoint in store.list_checkpoints()
            for artifact_id in checkpoint.artifacts
        }
        self.assertNotIn(waiting_ids[0], checkpoint_artifacts)
        first_checkpoint = next(
            checkpoint
            for checkpoint in store.list_checkpoints()
            if checkpoint.stage == "make-values"
        )
        checkpoint_path = (
            store.run_dir / "checkpoints" / f"{first_checkpoint.checkpoint_id}.json"
        )
        checkpoint_payload = read_json(checkpoint_path)
        checkpoint_payload["artifacts"].append(waiting_ids[0])
        atomic_write_json(checkpoint_path, checkpoint_payload)
        codes = {finding.code for finding in check_run(store.run_dir)}
        self.assertIn("CHECKPOINT_NON_SUCCESS_ARTIFACT", codes)

    def test_external_input_is_available_to_every_stage(self) -> None:
        source = self.root / "source.txt"
        source.write_text("external input\n", encoding="utf-8")
        manifest = self.root / "inputs.json"
        atomic_write_json(
            manifest,
            {"artifacts": [{"path": "source.txt", "kind": "input.synthetic"}]},
        )
        config = copy.deepcopy(SYNTHETIC_PIPELINE)
        config["pipeline_id"] = "synthetic-with-input"
        for stage in config["stages"]:
            stage["options"]["require_input_kind"] = "input.synthetic"
        store = RunStore.create(self.root / "runs", config, input_manifest=manifest)
        frozen_ref = store.artifact_map()[store.load_run().input_artifacts[0]]
        source.write_text("changed after init\n", encoding="utf-8")
        completed = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", completed.status)
        self.assertEqual(b"external input\n", store.artifact_path(frozen_ref).read_bytes())

    def test_stage_names_are_opaque_to_runner(self) -> None:
        config = copy.deepcopy(SYNTHETIC_PIPELINE)
        config["pipeline_id"] = "renamed"
        config["stages"][0]["name"] = "arbitrary-alpha"
        config["stages"][1]["name"] = "arbitrary-beta"
        store = RunStore.create(self.root, config)
        completed = run_pipeline(store.run_dir)
        self.assertEqual("succeeded", completed.status)
        self.assertEqual({"arbitrary-alpha", "arbitrary-beta"}, set(completed.attempts))


if __name__ == "__main__":
    unittest.main()
