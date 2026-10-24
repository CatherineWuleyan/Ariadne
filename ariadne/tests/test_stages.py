"""The public checkout must contain every file Ariadne's full mode needs."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from ariadne import paper2ir
from ariadne.full import REQUIRED_FILES
from ariadne.util import REPO, new_run_dir


class RunDirectoryTest(unittest.TestCase):
    def test_relative_out_path_is_absolute(self) -> None:
        import os

        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                path = new_run_dir("question", Path("relative/run"))
            finally:
                os.chdir(previous)
            self.assertTrue(path.is_absolute())
            self.assertEqual(path, (Path(tmp) / "relative" / "run").resolve())


class Paper2IRRetryTest(unittest.TestCase):
    def test_transient_markers(self) -> None:
        self.assertTrue(paper2ir._is_transient("ConnectionResetError: [Errno 54]"))
        self.assertTrue(paper2ir._is_transient("HTTP Error 503: Service Unavailable"))
        self.assertFalse(paper2ir._is_transient("invalid JSON schema"))

    def test_finds_cached_claims_from_previous_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cached = (root / "outputs" / "ariadne_runs" / "run_x"
                      / "02_paper2ir" / "data" / "paper-1" / "claims_final.json")
            cached.parent.mkdir(parents=True)
            cached.write_text('{"claim": []}\n', encoding="utf-8")
            with mock.patch("ariadne.paper2ir.REPO", root):
                found = paper2ir._find_cached_claims("paper-1")
        self.assertEqual(found, cached)

    def test_retries_transient_step_failure(self) -> None:
        failed = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Connection reset by peer"
        )
        succeeded = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("ariadne.paper2ir.run", side_effect=[failed, succeeded]) as runner, \
                    mock.patch("ariadne.paper2ir.time.sleep") as sleeper:
                proc = paper2ir._run_step(
                    root / "step.py", "paper-1", root, "/usr/bin/python",
                    root / "step.log",
                )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(runner.call_count, 2)
        sleeper.assert_called_once()
        second_env = runner.call_args_list[1].kwargs["env"]
        self.assertEqual(second_env["DEEPSEEK_MODEL"], "deepseek-v4-flash")

    def test_cache_rejects_changed_inputs_and_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "outputs/ariadne_runs/run_x/02_paper2ir"
            cached = stage / "data/paper-1/claims_final.json"
            cached.parent.mkdir(parents=True)
            cached.write_text('{"claim": []}\n')
            inputs = root / "inputs"
            inputs.mkdir()
            pipeline = root / "pipeline"
            pipeline.mkdir()
            (stage / "pipeline").mkdir()
            for folder in (inputs, cached.parent):
                (folder / "graph.json").write_text('{"nodes": []}')
                (folder / "ocr.md").write_text("original")
            for folder in (pipeline, stage / "pipeline"):
                (folder / "step.py").write_text("# original")
            with mock.patch("ariadne.paper2ir.REPO", root):
                self.assertEqual(paper2ir._find_cached_claims(
                    "paper-1", inputs=inputs, pipeline=pipeline), cached)
                (inputs / "ocr.md").write_text("revised")
                self.assertIsNone(paper2ir._find_cached_claims(
                    "paper-1", inputs=inputs, pipeline=pipeline))
                (inputs / "ocr.md").write_text("original")
                (pipeline / "step.py").write_text("# revised")
                self.assertIsNone(paper2ir._find_cached_claims(
                    "paper-1", inputs=inputs, pipeline=pipeline))


class PublicDependenciesTest(unittest.TestCase):
    def test_required_files_exist(self) -> None:
        missing = sorted(rel for rel in REQUIRED_FILES if not (REPO / rel).is_file())
        self.assertEqual(missing, [])

    def test_clean_claims_copy_keeps_code_and_drops_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = paper2ir._copy_pipeline(Path(tmp) / "pipeline")
            self.assertTrue((destination / "run_full_pipeline.py").is_file())
            self.assertTrue((destination / "step1_process_conclusions").is_dir())
            self.assertFalse((destination / "data").exists())
            self.assertFalse((destination / "test_run_full_pipeline.py").exists())
            self.assertFalse((destination / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
