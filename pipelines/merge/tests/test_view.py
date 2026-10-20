"""The merge projection must label every node with the frozen concise scheme."""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline_harness.models import ArtifactRef
from pipeline_merge.view import (
    LABEL_LETTERS,
    MergeViewAdapter,
    _is_observation_qid,
    _node_kind_for_paper_claim,
)

DIGEST = "a" * 64


def _knowledge(qid, content, *, kind="claim", metadata=None):
    return {"id": qid, "content": content, "type": kind, "label": qid.split("::")[-1],
            "metadata": metadata or {}, "source_anchor_ids": []}


def _paper(name, knowledges):
    return {"namespace": "papers", "package_name": name, "package_name_note": "",
            "knowledges": knowledges, "operators": [], "strategies": []}


def _integration(name, knowledges, operators):
    return {"package": {"namespace": "integration", "name": name},
            "knowledges": knowledges, "graph": {"operators": operators, "strategies": []}}

class _Store:
    """Minimal RunStore stand-in: the adapter only resolves artifact paths."""

    def __init__(self, paths):
        self._paths = paths

    def artifact_path(self, ref):
        return self._paths[ref.artifact_id]


class MergeViewLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _project(self, paper_payloads, integration_payload):
        documents = {}
        refs = []
        for index, payload in enumerate(paper_payloads):
            artifact_id = f"artifact_paper_{index}"
            path = self.root / f"{artifact_id}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            documents[artifact_id] = path
            refs.append(ArtifactRef(artifact_id, "gaia.ir", f"inputs/{artifact_id}.json",
                                    DIGEST, "application/json", "input"))
        integration_id = "artifact_integration"
        path = self.root / f"{integration_id}.json"
        path.write_text(json.dumps(integration_payload), encoding="utf-8")
        documents[integration_id] = path
        refs.append(ArtifactRef(integration_id, "formalization", f"work/{integration_id}.json",
                                DIGEST, "application/json", "step4_formalize_integration"))
        return MergeViewAdapter().project(_Store(documents), refs)

    def _fixture(self):
        alpha = _paper("alpha", [
            _knowledge("papers:alpha::claim_1", "paper claim one"),
            _knowledge("papers:alpha::claim_O01", "an observation"),
        ])
        beta = _paper("beta", [_knowledge("papers:beta::claim_2", "paper claim two")])
        integration = _integration("demo", {
            "integration:demo::candidate_K_aaaa": {
                "type": "claim", "content": {"canonical": "the domain conclusion"},
                "source_anchor_ids": [], "metadata": {"conclusion_level": 1}},
            "integration:demo::candidate_K_bbbb": {
                "type": "claim", "content": {"canonical": "a cluster conclusion"},
                "source_anchor_ids": []},
            "integration:demo::candidate_A_cccc": {
                "type": "claim", "content": {"canonical": "a summarized premise"},
                "source_anchor_ids": []},
        }, [
            {"type": "conjunction", "variables": [
                "papers:alpha::claim_1", "integration:demo::candidate_K_aaaa"]},
            {"type": "conjunction", "variables": [
                "papers:beta::claim_2", "integration:demo::candidate_K_bbbb"]},
            {"type": "conjunction", "variables": [
                "integration:demo::candidate_A_cccc", "integration:demo::candidate_K_aaaa"]},
        ])
        return [alpha, beta], integration

    def test_labels_are_concise_and_kind_specific(self):
        papers, integration = self._fixture()
        view = self._project(papers, integration)
        labels = {node["entity_id"] for node in view.nodes}
        self.assertIn("K1·L1", labels)     # level-tagged domain conclusion
        self.assertIn("C1", labels)        # untagged conclusion owned by a Package
        self.assertIn("A1", labels)        # summarized premise
        self.assertIn("O1", labels)        # claim_O01 is an observation
        self.assertIn("P1", labels)        # plain paper claims
        self.assertIn("P2", labels)
        self.assertEqual(len(labels), len(view.nodes), "labels must be unique")

    def test_original_ids_survive_in_details(self):
        papers, integration = self._fixture()
        view = self._project(papers, integration)
        by_label = {node["entity_id"]: node for node in view.nodes}
        self.assertEqual(by_label["K1·L1"]["details"]["qid"],
                         "integration:demo::candidate_K_aaaa")
        self.assertEqual(by_label["K1·L1"]["details"]["original_label"], "candidate_K_aaaa")
        self.assertEqual(by_label["P1"]["details"]["original_label"], "claim_1")
        self.assertEqual(len({node["id"] for node in view.nodes}), len(view.nodes))

    def test_label_scheme_is_reported_as_metadata(self):
        papers, integration = self._fixture()
        view = self._project(papers, integration)
        scheme = view.metadata["label_scheme"]
        self.assertIn("domain conclusion", scheme["K"])
        self.assertEqual(scheme["by_kind"].get("K"), 1)
        self.assertEqual(scheme["by_kind"].get("C"), 1)
        self.assertEqual(scheme["by_kind"].get("A"), 1)
        self.assertEqual(scheme["by_kind"].get("O"), 1)
        self.assertEqual(scheme["by_kind"].get("P"), 2)

    def test_search_documents_use_the_short_label(self):
        papers, integration = self._fixture()
        view = self._project(papers, integration)
        titles = {doc["title"] for doc in view.search_documents}
        self.assertIn("K1·L1", titles)
        self.assertIn("P1", titles)

    def test_observation_detection_does_not_claim_plain_claims(self):
        self.assertTrue(_is_observation_qid("papers:a::claim_O01"))
        self.assertTrue(_is_observation_qid("papers:a::claim_E12"))
        self.assertFalse(_is_observation_qid("papers:a::claim_1"))
        self.assertFalse(_is_observation_qid("papers:a::claim_Other"))

    def test_integration_owned_conclusion_is_not_a_paper_claim(self):
        """A cluster run re-exports its conclusions into the corpus Package."""
        self.assertEqual(_node_kind_for_paper_claim(
            "integration:corpus::candidate_K_aaaaaaaa", ["integration:corpus"]), "observation")
        self.assertEqual(_node_kind_for_paper_claim(
            "papers:a::claim_9", ["integration:lth_cluster_03"]), "observation")
        self.assertEqual(_node_kind_for_paper_claim(
            "papers:a::claim_9", ["papers:a"]), "paper_claim")
        self.assertEqual(_node_kind_for_paper_claim("papers:a::claim_9", []), "paper_claim")

    def test_integration_owned_conclusion_keeps_level_suffix(self):
        papers, integration = self._fixture()
        view = self._project(papers, integration)
        by_label = {node["entity_id"]: node for node in view.nodes}
        self.assertEqual(by_label["K1·L1"]["details"]["metadata"]["conclusion_level"], 1)
        self.assertNotIn("conclusion_level", by_label["C1"]["details"]["metadata"])


if __name__ == "__main__":
    unittest.main()
