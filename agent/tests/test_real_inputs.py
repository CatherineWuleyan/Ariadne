import json
import tempfile
import unittest
from pathlib import Path

from pipeline_harness.real_inputs import REAL_INPUT_PIPELINE, RealInputImporter
from pipeline_harness.runner import run_pipeline
from pipeline_harness.store import RunStore, atomic_write_json


class RealInputTests(unittest.TestCase):
    def test_importer_uses_manifest_refs_and_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "paper.md").write_text("# paper\n", encoding="utf-8")
            atomic_write_json(root / "claims.json", {"assertions": [], "relations": []})
            atomic_write_json(root / "graph.json", {"code": 0, "data": {"papers": [{"graph": {"nodes": [], "edges": []}}]}})
            atomic_write_json(root / "manifest.json", {"artifacts": [
                {"path": "paper.md", "kind": "source.paper_text", "media_type": "text/markdown"},
                {"path": "claims.json", "kind": "source.clean_claims", "media_type": "application/json"},
                {"path": "graph.json", "kind": "source.lkm_coarse_graph", "media_type": "application/json"},
            ]})
            store = RunStore.create(root / "runs", REAL_INPUT_PIPELINE, input_manifest=root / "manifest.json")
            run = run_pipeline(store.run_dir)
            self.assertEqual("succeeded", run.status)
            bundles = [artifact for artifact in store.load_artifacts() if artifact.kind == "input.bundle"]
            self.assertEqual(1, len(bundles))
            payload = json.loads(store.artifact_path(bundles[0]).read_text(encoding="utf-8"))
            self.assertEqual(
                {"source.paper_text", "source.clean_claims", "source.lkm_coarse_graph"},
                set(payload["sources"]),
            )
            self.assertTrue(all(payload["sources"][kind][0]["artifact_id"] for kind in payload["sources"]))


if __name__ == "__main__":
    unittest.main()
