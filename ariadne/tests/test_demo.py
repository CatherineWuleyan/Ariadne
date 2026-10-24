"""Smoke test: the offline demo produces both deliverables."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ariadne.demo import run_demo


class DemoTest(unittest.TestCase):
    def test_demo_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_demo(Path(tmp))
            viewer = Path(result["viewer"])
            chains = Path(result["chains"])
            self.assertTrue(viewer.is_file())
            self.assertTrue(chains.is_file())
            text = chains.read_text(encoding="utf-8")
            self.assertIn("# 指向 R1–R5 的所有多步推理链", text)
            self.assertIn("R4-01", text)
            self.assertIn("same-paper", text)

            record = json.loads((Path(result["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "complete")
            self.assertIn("started_at", record)
            self.assertIn("04_question", record["stages"])
            reproducibility = record["reproducibility"]
            self.assertIn("commit", reproducibility["code"])
            self.assertIn("view_sha256", reproducibility["inputs"])


if __name__ == "__main__":
    unittest.main()
