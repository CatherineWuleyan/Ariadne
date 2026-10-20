from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore

from pipeline_merge.step0 import validate_step0_options


ROOT = Path(__file__).resolve().parents[1]


def _config(mode: str, selection: list[str]) -> dict:
    return {
        "pipeline_id": f"pipeline-merge-step0-{mode}",
        "version": "0.1.0",
        "stages": [{
            "name": "step0_select_scope",
            "plugin": "pipeline_merge.step0:Step0SelectScopePlugin",
            "options": {
                "mode": mode,
                "scope": {
                    "domain": "test-domain",
                    "selection": selection,
                },
            },
        }],
    }


class Step0Tests(unittest.TestCase):
    def test_checked_in_config_runs_without_artifacts(self) -> None:
        config = json.loads((ROOT / "pipeline.step0.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), config)
            run = run_pipeline(store.run_dir)
            self.assertEqual("succeeded", run.status)
            self.assertEqual(1, run.next_stage_index)
            self.assertEqual([], store.load_artifacts())
            self.assertEqual([], store.list_checkpoints()[0].artifacts)
            self.assertEqual(
                config["stages"][0]["options"],
                run.config["stages"][0]["options"],
            )

    def test_all_three_manual_modes_are_accepted(self) -> None:
        cases = {
            "bootstrap": ["paper-a", "paper-b"],
            "incremental": ["paper-new"],
            "reconcile": ["knowledge-qid"],
        }
        for mode, selection in cases.items():
            with self.subTest(mode=mode):
                normalized = validate_step0_options(
                    _config(mode, selection)["stages"][0]["options"]
                )
                self.assertEqual(mode, normalized["mode"])
                self.assertEqual(selection, normalized["scope"]["selection"])

    def test_invalid_mode_fails_closed(self) -> None:
        config = _config("automatic", ["paper-a", "paper-b"])
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary), config)
            run = run_pipeline(store.run_dir)
            self.assertEqual("failed", run.status)
            self.assertEqual("STEP0_SELECTION_INVALID", run.findings[0]["code"])
            self.assertEqual([], store.load_artifacts())

    def test_bootstrap_requires_two_unique_packages(self) -> None:
        for selection in ([], ["paper-a"], ["paper-a", "paper-a"]):
            with self.subTest(selection=selection):
                with self.assertRaises(ValueError):
                    validate_step0_options(
                        _config("bootstrap", copy.deepcopy(selection))["stages"][0]["options"]
                    )

    def test_scope_shape_is_exact_and_strings_are_canonical(self) -> None:
        options = _config("incremental", ["paper-new"])["stages"][0]["options"]
        options["scope"]["extra"] = True
        with self.assertRaises(ValueError):
            validate_step0_options(options)

        options = _config("incremental", [" paper-new"])["stages"][0]["options"]
        with self.assertRaises(ValueError):
            validate_step0_options(options)


if __name__ == "__main__":
    unittest.main()
