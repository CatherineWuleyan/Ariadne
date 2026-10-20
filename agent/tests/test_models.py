from __future__ import annotations

import unittest

from pipeline_harness.models import ArtifactRef, Finding, RunRecord
from pipeline_harness.view.model import ViewDocument


class ModelTests(unittest.TestCase):
    def test_finding_rejects_unknown_severity(self) -> None:
        with self.assertRaisesRegex(ValueError, "severity"):
            Finding(code="BAD", severity="fatal", message="bad")

    def test_artifact_rejects_invalid_hash(self) -> None:
        with self.assertRaisesRegex(ValueError, "sha256"):
            ArtifactRef(
                artifact_id="artifact_1",
                kind="test",
                path="artifacts/value.json",
                sha256="not-a-hash",
                media_type="application/json",
                producer_stage="test",
            )

    def test_run_rejects_unknown_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "status"):
            RunRecord(
                run_id="run_1",
                pipeline_id="test",
                pipeline_version="1",
                status="done",
                current_stage=None,
                config={},
                config_sha256="0" * 64,
                created_at="now",
                updated_at="now",
            )

    def test_view_rejects_dangling_edge(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing endpoint"):
            ViewDocument.from_dict(
                {
                    "title": "test",
                    "source_artifacts": [],
                    "nodes": [{"id": "A", "label": "A"}],
                    "edges": [{"id": "E", "source": "A", "target": "B"}],
                    "search_documents": [],
                    "layers": [],
                }
            )


if __name__ == "__main__":
    unittest.main()
