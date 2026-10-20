from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline_harness.checks import check_run
from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import (
    RunStore,
    atomic_write_json,
    read_json,
    sha256_file,
    sha256_json,
)
from pipeline_harness.synthetic import SYNTHETIC_PIPELINE


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registers_copy_without_changing_source(self) -> None:
        source = self.root / "source.json"
        source.write_text('{"value": 1}\n', encoding="utf-8")
        original_hash = sha256_file(source)
        store = RunStore.create(self.root / "runs", SYNTHETIC_PIPELINE)
        ref = store.register_file(
            source,
            kind="test.input",
            media_type="application/json",
            producer_stage="input",
        )
        self.assertEqual(original_hash, sha256_file(source))
        self.assertEqual(original_hash, ref.sha256)
        self.assertNotEqual(source.resolve(), store.artifact_path(ref))
        self.assertEqual(source.read_bytes(), store.artifact_path(ref).read_bytes())

    def test_checkpoint_rejects_unknown_artifact(self) -> None:
        store = RunStore.create(self.root / "runs", SYNTHETIC_PIPELINE)
        with self.assertRaisesRegex(ValueError, "unknown artifact"):
            store.create_checkpoint("test", ["artifact_missing"])

    def test_create_freezes_declared_inputs_and_config(self) -> None:
        source = self.root / "source.txt"
        source.write_text("external input\n", encoding="utf-8")
        source_hash = sha256_file(source)
        manifest = self.root / "inputs.json"
        atomic_write_json(
            manifest,
            {
                "artifacts": [
                    {
                        "path": "source.txt",
                        "kind": "input.synthetic",
                        "media_type": "text/plain",
                        "metadata": {"fixture": True},
                    }
                ]
            },
        )

        store = RunStore.create(
            self.root / "runs",
            SYNTHETIC_PIPELINE,
            input_manifest=manifest,
        )
        run = store.load_run()
        self.assertEqual(sha256_json(SYNTHETIC_PIPELINE), run.config_sha256)
        self.assertEqual(1, len(run.input_artifacts))
        ref = store.artifact_map()[run.input_artifacts[0]]
        self.assertEqual("input.synthetic", ref.kind)
        self.assertEqual("input", ref.producer_stage)
        self.assertEqual(source_hash, ref.sha256)
        self.assertEqual(source_hash, sha256_file(source))
        normalized = read_json(store.input_manifest_path)
        self.assertEqual(
            run.input_artifacts,
            [item["artifact_id"] for item in normalized["artifacts"]],
        )
        self.assertEqual([], check_run(store.run_dir))

    def test_manifest_glob_expands_variable_number_of_files(self) -> None:
        figures = self.root / "figures"
        figures.mkdir()
        (figures / "figure-a.jpg").write_bytes(b"jpg-a")
        (figures / "figure-b.png").write_bytes(b"png-b")
        manifest = self.root / "inputs.json"
        atomic_write_json(manifest, {"artifacts": [{
            "path_glob": "figures/*",
            "kind": "source.original_figure",
            "required": False,
            "metadata": {"version": "1"},
        }]})

        store = RunStore.create(self.root / "runs", SYNTHETIC_PIPELINE, input_manifest=manifest)
        refs = [store.artifact_map()[artifact_id] for artifact_id in store.load_run().input_artifacts]
        self.assertEqual(["image/jpeg", "image/png"], [ref.media_type for ref in refs])
        self.assertEqual([1, 2], [ref.metadata["sequence"] for ref in refs])

    def test_optional_manifest_glob_may_match_zero_files(self) -> None:
        manifest = self.root / "inputs.json"
        atomic_write_json(manifest, {"artifacts": [{
            "path_glob": "figures/*",
            "kind": "source.original_figure",
            "required": False,
        }]})
        store = RunStore.create(self.root / "runs", SYNTHETIC_PIPELINE, input_manifest=manifest)
        self.assertEqual([], store.load_run().input_artifacts)

    def test_check_detects_tampered_config_and_input_manifest(self) -> None:
        source = self.root / "source.txt"
        source.write_text("external input\n", encoding="utf-8")
        manifest = self.root / "inputs.json"
        atomic_write_json(
            manifest,
            {"artifacts": [{"path": "source.txt", "kind": "input.synthetic"}]},
        )
        store = RunStore.create(
            self.root / "runs",
            SYNTHETIC_PIPELINE,
            input_manifest=manifest,
        )
        run_payload = read_json(store.run_path)
        run_payload["config"]["version"] = "tampered"
        atomic_write_json(store.run_path, run_payload)
        input_payload = read_json(store.input_manifest_path)
        input_payload["artifacts"][0]["sha256"] = "0" * 64
        atomic_write_json(store.input_manifest_path, input_payload)
        input_ref = store.artifact_map()[run_payload["input_artifacts"][0]]
        store.artifact_path(input_ref).write_text("tampered copy\n", encoding="utf-8")
        codes = {finding.code for finding in check_run(store.run_dir)}
        self.assertIn("CONFIG_HASH_MISMATCH", codes)
        self.assertIn("INPUT_MANIFEST_MISMATCH", codes)
        self.assertIn("ARTIFACT_HASH_MISMATCH", codes)
        with self.assertRaisesRegex(ValueError, "frozen hash"):
            run_pipeline(store.run_dir)

    def test_check_detects_tampered_artifact(self) -> None:
        source = self.root / "source.txt"
        source.write_text("original", encoding="utf-8")
        store = RunStore.create(self.root / "runs", SYNTHETIC_PIPELINE)
        ref = store.register_file(
            source,
            kind="test.input",
            media_type="text/plain",
            producer_stage="input",
        )
        store.artifact_path(ref).write_text("tampered", encoding="utf-8")
        codes = {finding.code for finding in check_run(store.run_dir)}
        self.assertIn("ARTIFACT_HASH_MISMATCH", codes)


if __name__ == "__main__":
    unittest.main()
