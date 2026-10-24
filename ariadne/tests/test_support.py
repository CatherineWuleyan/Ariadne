"""Attaching a new candidate to a frozen graph (no model call, no retrieval)."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ariadne.support import (
    STATUS_CONTRADICTED,
    STATUS_SUPPORTED,
    STATUS_UNRESOLVED,
    build_candidate,
    render_markdown,
    run_support,
)


def toy_view() -> dict:
    return {
        "title": "toy",
        "nodes": [
            {"id": "knowledge:papers:paper_a::claim_1", "kind": "claim", "layer": "claims",
             "summary": "Model calibration against measured flight data reduced prediction error.",
             "details": {"content": "Model calibration against measured flight data reduced prediction error."}},
            {"id": "knowledge:papers:paper_a::claim_2", "kind": "claim", "layer": "claims",
             "summary": "Aerodynamic delta models are identified from doublet manoeuvres.",
             "details": {"content": "Aerodynamic delta models are identified from doublet manoeuvres."}},
            {"id": "knowledge:papers:paper_b::claim_1", "kind": "claim", "layer": "claims",
             "summary": "Hardware-in-the-loop testing validated the controller before flight test.",
             "details": {"content": "Hardware-in-the-loop testing validated the controller before flight test."}},
        ],
        "edges": [],
    }


def fake_judge(links):
    """A call_json stand-in that returns one fixed judgement."""
    def call(prompt: str) -> dict:
        assert "CANDIDATES" in prompt
        return {"root_id": "C1", "links": links}
    return call


class BuildCandidateTests(unittest.TestCase):
    def test_empty_statement_is_rejected(self) -> None:
        from ariadne.util import AriadneError

        with self.assertRaises(AriadneError):
            build_candidate("   ")

    def test_shape_matches_what_the_question_layer_expects(self) -> None:
        candidate = build_candidate("Calibrate against flight data", code="C7")
        for key in ("id", "code", "title_zh", "title_en", "statement", "scope",
                    "criteria", "answer_type", "seed_claim_ids"):
            self.assertIn(key, candidate)
        self.assertEqual(candidate["code"], "C7")
        self.assertEqual(candidate["id"], "candidate:c7")


class RunSupportTests(unittest.TestCase):
    def _run(self, links, *, out=None):
        view = toy_view()
        with tempfile.TemporaryDirectory() as tmp:
            view_path = Path(tmp) / "view.json"
            view_path.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
            before = hashlib.sha256(view_path.read_bytes()).hexdigest()
            report = run_support(
                view_path,
                build_candidate("Calibrate the model against measured flight data"),
                out=out,
                call_json=fake_judge(links),
            )
            after = hashlib.sha256(view_path.read_bytes()).hexdigest()
        return report, before, after

    def test_support_makes_the_candidate_supported(self) -> None:
        report, before, after = self._run([
            {"claim_id": "knowledge:papers:paper_a::claim_1", "relation": "support",
             "reason": "reports calibration against measured data"},
        ])
        self.assertEqual(report["status"], STATUS_SUPPORTED)
        self.assertEqual(len(report["links"]["support"]), 1)
        self.assertFalse(report["scope"]["searched"])
        self.assertFalse(report["next"]["retrieval_recommended"])
        self.assertEqual(before, after, "the frozen view must not be modified")

    def test_no_support_is_reported_as_unsearched_not_unsupported_forever(self) -> None:
        report, _, _ = self._run([])
        self.assertEqual(report["status"], STATUS_UNRESOLVED)
        self.assertTrue(report["next"]["retrieval_recommended"])
        self.assertIn("no LKM search was performed", report["next"]["reason"])
        self.assertFalse(report["scope"]["searched"])

    def test_only_opposition_is_contradicted(self) -> None:
        report, _, _ = self._run([
            {"claim_id": "knowledge:papers:paper_b::claim_1", "relation": "oppose",
             "reason": "reports the opposite ordering under matched conditions"},
        ])
        self.assertEqual(report["status"], STATUS_CONTRADICTED)
        self.assertEqual(report["links"]["oppose"][0]["claim_id"],
                         "knowledge:papers:paper_b::claim_1")

    def test_invented_claim_ids_are_dropped(self) -> None:
        report, _, _ = self._run([
            {"claim_id": "knowledge:papers:paper_z::claim_9", "relation": "support",
             "reason": "this id is not in the graph"},
        ])
        self.assertEqual(report["status"], STATUS_UNRESOLVED)
        self.assertEqual(report["links"]["support"], [])

    def test_conflicting_relations_on_one_claim_are_dropped(self) -> None:
        report, _, _ = self._run([
            {"claim_id": "knowledge:papers:paper_a::claim_2", "relation": "support",
             "reason": "supports it"},
            {"claim_id": "knowledge:papers:paper_a::claim_2", "relation": "oppose",
             "reason": "contradicts it"},
        ])
        self.assertEqual(report["status"], STATUS_UNRESOLVED)

    def test_scope_counts_what_the_judge_actually_saw(self) -> None:
        report, _, _ = self._run([])
        scope = report["scope"]
        self.assertEqual(scope["node_count"], 3)
        self.assertEqual(scope["edge_count"], 0)
        self.assertGreater(scope["claims_offered_to_judge"], 0)
        self.assertLessEqual(scope["claims_offered_to_judge"], scope["node_count"])

    def test_writes_report_files_when_asked(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            report, _, _ = self._run([], out=Path(out))
            self.assertTrue((Path(out) / "support.json").is_file())
            markdown = (Path(out) / "support.md").read_text(encoding="utf-8")
        self.assertIn("support audit", markdown)
        self.assertIn("searched LKM: no", markdown)
        self.assertEqual(report["status"], STATUS_UNRESOLVED)

    def test_markdown_distinguishes_absent_from_unsearched(self) -> None:
        report, _, _ = self._run([])
        text = render_markdown(report)
        self.assertIn("claims offered to the judge", text)
        self.assertIn("absence here", text + report["scope"]["retrieval"])


class ExpansionTests(unittest.TestCase):
    """Expansion fetches graphs only; it must not silently spend or mutate."""

    def _graph(self, pid: str) -> dict:
        return {"code": 0, "data": {"papers": [{
            "paper": {"id": pid, "en_title": f"Paper {pid}"},
            "graph": {"nodes": [
                {"id": "c1", "kind": "conclusion",
                 "content": "Calibration against measured flight data reduced prediction error substantially."},
                {"id": "c2", "kind": "claim",
                 "content": "Doublet manoeuvres identify aerodynamic delta models from flight test data."},
                {"id": "tiny", "kind": "claim", "content": "short"},
            ], "edges": []},
        }]}}

    def test_expansion_adds_marked_nodes_and_reports_cost(self) -> None:
        from unittest import mock
        from ariadne.support import expand
        from ariadne.config import Config, Runtime

        runtime = Runtime(config=Config.load(Path("/nonexistent/config.json")),
                          model_api_key="k", model_base_url="https://example/v1",
                          model="m", merge_model="m", sources={})
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("ariadne.retrieval.search",
                            return_value=[{"lkm_id": "p1", "title": "Paper one"}]), \
                    mock.patch("ariadne.retrieval._fetch_graph",
                               return_value=(None, self._graph("p1"))):
                merged, audit = expand(
                    toy_view(), build_candidate("Calibrate the model"), runtime=runtime,
                    run_dir=Path(tmp), max_papers=2, yes=True,
                )
        added = [n for n in merged["nodes"] if (n.get("details") or {}).get("origin") == "lkm_expand"]
        # The 7-character node is dropped, so two usable nodes per paper survive.
        self.assertEqual(len(added), 2)
        self.assertTrue(all(n["id"].startswith("knowledge:papers:expand_p1::") for n in added))
        self.assertEqual(audit["papers_used"], 1)
        self.assertEqual(audit["nodes_added"], 2)
        self.assertIn("not processed claims", audit["note"])

    def test_retrieved_links_are_marked_and_scope_says_searched(self) -> None:
        from unittest import mock
        from ariadne.config import Config, Runtime

        overlay = [{
            "id": "knowledge:papers:expand_p1::c1", "kind": "claim", "layer": "claims",
            "summary": "Calibration against measured flight data reduced prediction error.",
            "details": {"content": "Calibration against measured flight data reduced prediction error.",
                        "origin": "lkm_expand"},
        }]
        merged = toy_view()
        merged["nodes"] = merged["nodes"] + overlay
        audit = {"query": "calibrate", "papers_requested": 1, "papers_used": 1,
                 "nodes_added": 1, "papers": [], "skipped": [], "estimated_cost_cny": 0.1,
                 "note": "raw LKM graph nodes, not processed claims"}
        runtime = Runtime(config=Config.load(Path("/nonexistent/config.json")),
                          model_api_key="k", model_base_url="https://example/v1",
                          model="m", merge_model="m", sources={})
        judge = fake_judge([{"claim_id": "knowledge:papers:expand_p1::c1",
                             "relation": "support", "reason": "reports the same calibration"}])
        with tempfile.TemporaryDirectory() as tmp:
            view_path = Path(tmp) / "view.json"
            view_path.write_text(json.dumps(toy_view()), encoding="utf-8")
            with mock.patch("ariadne.support.expand", return_value=(merged, audit)):
                report = run_support(
                    view_path, build_candidate("Calibrate the model"),
                    call_json=judge, runtime=runtime,
                    expand_with={"run_dir": tmp, "max_papers": 1, "yes": True},
                )
        self.assertTrue(report["scope"]["searched"])
        self.assertEqual(report["scope"]["retrieval"]["nodes_added"], 1)
        self.assertEqual(report["links"]["support"][0]["origin"], "retrieval")
        self.assertEqual(report["status"], STATUS_SUPPORTED)

    def test_expansion_requires_explicit_approval(self) -> None:
        from ariadne.config import Config, Runtime
        from ariadne.support import expand
        from ariadne.util import AriadneError

        runtime = Runtime(config=Config.load(Path("/nonexistent/config.json")),
                          model_api_key="k", model_base_url="https://example/v1",
                          model="m", merge_model="m", sources={})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AriadneError) as caught:
                expand(toy_view(), build_candidate("candidate"), runtime=runtime,
                       run_dir=Path(tmp), max_papers=2, yes=False)
        self.assertIn("billable", str(caught.exception))
        self.assertIn("--yes", str(caught.exception))


class CliWiringTests(unittest.TestCase):
    def test_support_subcommand_is_registered(self) -> None:
        from ariadne.cli import build_parser

        args = build_parser().parse_args(
            ["support", "--view", "view.json", "--candidate", "run a calibration sweep"]
        )
        self.assertEqual(args.func.__name__, "cmd_support")
        self.assertEqual(args.view, "view.json")
        self.assertEqual(args.code, "C1")
        self.assertFalse(args.json)
        self.assertIsNone(args.expand)

    def test_expand_accepts_an_optional_paper_count(self) -> None:
        from ariadne.cli import build_parser

        bare = build_parser().parse_args(["support", "--view", "v.json", "--candidate", "c", "--expand"])
        sized = build_parser().parse_args(
            ["support", "--view", "v.json", "--candidate", "c", "--expand", "7", "--yes"]
        )
        self.assertEqual(bare.expand, 4)
        self.assertEqual(sized.expand, 7)
        self.assertTrue(sized.yes)


if __name__ == "__main__":
    unittest.main()
