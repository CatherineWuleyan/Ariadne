from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline_harness.checks import check_run
from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json, read_json
from pipeline_harness.synthetic import SYNTHETIC_PIPELINE


class ForkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fork_resumes_after_checkpoint_without_mutating_parent(self) -> None:
        source = self.root / "source.txt"
        source.write_text("fork input\n", encoding="utf-8")
        manifest = self.root / "inputs.json"
        atomic_write_json(
            manifest,
            {"artifacts": [{"path": "source.txt", "kind": "input.synthetic"}]},
        )
        parent = RunStore.create(
            self.root / "runs",
            SYNTHETIC_PIPELINE,
            input_manifest=manifest,
        )
        run_pipeline(parent.run_dir)
        parent_before = parent.load_run().to_dict()
        first_checkpoint = next(
            checkpoint
            for checkpoint in parent.list_checkpoints()
            if checkpoint.stage == "make-values"
        )
        parent_artifacts = parent.artifact_map()

        child = RunStore.fork(parent.run_dir, first_checkpoint.checkpoint_id)
        child_run = child.load_run()
        self.assertEqual(parent_before, parent.load_run().to_dict())
        self.assertEqual(parent_before["run_id"], child_run.parent_run_id)
        self.assertEqual(first_checkpoint.checkpoint_id, child_run.parent_checkpoint_id)
        self.assertEqual(1, child_run.next_stage_index)
        self.assertEqual("make-graph", child_run.current_stage)
        self.assertEqual(1, len(child_run.input_artifacts))
        child_input = child.artifact_map()[child_run.input_artifacts[0]]
        parent_input = parent_artifacts[parent_before["input_artifacts"][0]]
        self.assertEqual(parent_input.sha256, child_input.sha256)
        self.assertEqual(
            parent_input.artifact_id,
            child_input.metadata["_fork"]["source_artifact_id"],
        )

        child_checkpoint = child.load_checkpoint(str(child_run.last_checkpoint_id))
        self.assertEqual(first_checkpoint.stage, child_checkpoint.stage)
        self.assertEqual(1, len(child_checkpoint.artifacts))
        child_ref = child.artifact_map()[child_checkpoint.artifacts[0]]
        parent_ref = parent_artifacts[first_checkpoint.artifacts[0]]
        self.assertEqual(parent_ref.sha256, child_ref.sha256)
        self.assertEqual(parent_ref.artifact_id, child_ref.metadata["_fork"]["source_artifact_id"])

        completed = run_pipeline(child.run_dir)
        self.assertEqual("succeeded", completed.status)
        self.assertEqual([], check_run(child.run_dir))
        index_payload = read_json(child.artifact_index_path)
        copied_input = next(
            item
            for item in index_payload["artifacts"]
            if item["artifact_id"] == child_run.input_artifacts[0]
        )
        copied_input["metadata"].pop("_fork")
        atomic_write_json(child.artifact_index_path, index_payload)
        codes = {finding.code for finding in check_run(child.run_dir)}
        self.assertIn("FORK_SOURCE_MISSING", codes)

    def test_check_detects_missing_parent_checkpoint(self) -> None:
        parent = RunStore.create(self.root, SYNTHETIC_PIPELINE)
        run_pipeline(parent.run_dir, max_stages=1)
        checkpoint = parent.list_checkpoints()[0]
        child = RunStore.fork(parent.run_dir, checkpoint.checkpoint_id)
        child_run = child.load_run()
        child_run.parent_checkpoint_id = "checkpoint_missing"
        child.save_run(child_run)
        codes = {finding.code for finding in check_run(child.run_dir)}
        self.assertIn("PARENT_CHECKPOINT_MISSING", codes)

    def test_fork_freezes_additional_inputs_without_requiring_parent_provenance(self) -> None:
        parent = RunStore.create(self.root, SYNTHETIC_PIPELINE)
        run_pipeline(parent.run_dir, max_stages=1)
        checkpoint = parent.list_checkpoints()[0]
        review = self.root / "review.json"
        atomic_write_json(review, {"decision": "approved"})
        manifest = self.root / "review-manifest.json"
        atomic_write_json(
            manifest,
            {"artifacts": [{"path": "review.json", "kind": "review.decisions", "media_type": "application/json"}]},
        )
        child = RunStore.fork(
            parent.run_dir,
            checkpoint.checkpoint_id,
            additional_input_manifest=manifest,
        )
        refs = [child.artifact_map()[artifact_id] for artifact_id in child.load_run().input_artifacts]
        additional = next(ref for ref in refs if ref.kind == "review.decisions")
        self.assertNotIn("_fork", additional.metadata)
        self.assertEqual([], check_run(child.run_dir))


if __name__ == "__main__":
    unittest.main()
