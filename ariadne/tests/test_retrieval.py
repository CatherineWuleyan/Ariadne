"""Tests for retrieval parsing and offline materialisation (no network)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ariadne import retrieval
from ariadne.config import Config, Runtime
from ariadne.util import AriadneError


def _runtime(tmp: Path, **overrides) -> Runtime:
    config = Config.load(tmp / "config.json")
    values = dict(
        config=config,
        model_api_key="test-key",
        model_base_url="https://api.example.com/v1",
        model="test-model",
        merge_model="test-model",
        bohr_profile="ariadne-test",
        bohr_access_key="",
        bohr_host="https://open.bohrium.com",
        auto_approve_spend=True,
        payment_source="photon",
        python=sys.executable,
        bohr_bin="/usr/local/bin/bohr",
    )
    values.update(overrides)
    return Runtime(**values)


class SearchCommandTest(unittest.TestCase):
    def test_search_passes_profile_billing_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            payload = {
                "ok": True,
                "data": {
                    "papers": {
                        "papers:paper_1": {
                            "paper_id": "123456789",
                            "en_title": "Aircraft simulation",
                            "doi": "10.48550/arxiv.2501.00001",
                            "authors": ["A", "B"],
                        }
                    }
                },
            }
            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(payload), stderr=""
                )

            with mock.patch("ariadne.retrieval.run", side_effect=fake_run):
                records = retrieval.search("aircraft", 2, root, runtime)

            command = captured["cmd"]
            self.assertIn("--profile", command)
            self.assertEqual(command[command.index("--profile") + 1], "ariadne-test")
            self.assertIn("--yes", command)
            self.assertIn("--payment-source", command)
            self.assertEqual(command[-2:], ["-o", "json"])
            self.assertEqual(records[0]["lkm_id"], "123456789")
            self.assertEqual(records[0]["arxiv"], "2501.00001")
            self.assertEqual(records[0]["authors"], "A, B")
            self.assertTrue((root / "01_retrieval" / "search_raw.json").is_file())

    def test_variables_fallback(self) -> None:
        payload = {
            "ok": True,
            "data": {
                "papers": {},
                "variables": [
                    {"paper_id": "123456789", "title": "First paper"},
                    {"paper_id": "987654321", "title": "Second paper"},
                    {"paper_id": "123456789", "title": "duplicate"},
                ],
            },
        }
        records = retrieval._records_from_search(payload)
        self.assertEqual([item["lkm_id"] for item in records],
                         ["123456789", "987654321"])


class GraphNormalisationTest(unittest.TestCase):
    def test_bare_graph_is_wrapped(self) -> None:
        raw = {"nodes": [{"id": "n1"}], "edges": []}
        graph = retrieval._normalise_graph_payload(raw)
        self.assertEqual(graph["code"], 0)
        self.assertEqual(graph["data"]["papers"][0]["graph"]["nodes"][0]["id"], "n1")

    def test_items_envelope_is_converted(self) -> None:
        raw = {"ok": True, "data": {"items": [{"graph": {"nodes": []}}]}}
        graph = retrieval._normalise_graph_payload(raw)
        self.assertEqual(graph["data"]["papers"], [{"graph": {"nodes": []}}])

    def test_papers_envelope_is_preserved(self) -> None:
        raw = {"code": 0, "data": {"papers": [{"graph": {"nodes": []}}]}}
        graph = retrieval._normalise_graph_payload(raw)
        self.assertEqual(graph, raw)


class OfflineMaterialiseTest(unittest.TestCase):
    def test_spec_without_lkm_id_gets_stable_local_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph_path = root / "graph.json"
            text_path = root / "paper.md"
            graph_path.write_text(
                json.dumps({
                    "data": {"papers": [{
                        "paper": {"en_title": "Local paper"},
                        "graph": {"nodes": [{"kind": "conclusion", "content": "C"}],
                                  "edges": []},
                    }]}
                }),
                encoding="utf-8",
            )
            text_path.write_text("# Local paper\n", encoding="utf-8")
            spec = root / "papers.json"
            spec.write_text(json.dumps({"papers": [{
                "title": "Local paper",
                "graph_path": str(graph_path),
                "ocr_path": str(text_path),
            }]}), encoding="utf-8")
            runtime = _runtime(root)
            run_dir = root / "run"
            papers = retrieval.retrieve(
                "q", run_dir, paper_spec=spec, runtime=runtime, fulltext=False
            )
            self.assertEqual(len(papers), 1)
            self.assertTrue(papers[0]["lkm_id"].startswith("local_"))
            self.assertEqual(papers[0]["title"], "Local paper")
            self.assertTrue((run_dir / papers[0]["graph_path"]).is_file())
            self.assertTrue((run_dir / papers[0]["ocr_path"]).is_file())


class QueryPlanningTest(unittest.TestCase):
    def test_deterministic_fallback_queries(self) -> None:
        queries, planner = retrieval.plan_queries(
            "如何利用知识图谱辅助实验设计？",
            question_en="How can knowledge graphs support experiment design?",
            domain="knowledge graphs, experiment design",
            max_queries=4,
            use_query_planner=False,
        )
        self.assertEqual(planner, "fallback")
        self.assertLessEqual(len(queries), 4)
        sources = [item["source"] for item in queries]
        self.assertEqual(sources[0], "english")
        self.assertIn("question", sources)
        keyword = next(item["query"] for item in queries if item["source"] == "keywords")
        self.assertIn("知识图谱", keyword)
        self.assertIn("实验设计", keyword)

    def test_llm_queries_are_added_and_limited(self) -> None:
        def fake_call(prompt):
            return {"queries": [
                "knowledge graph experiment design",
                "scientific decision support knowledge graph",
                "automated experiment planning",
            ]}

        queries, planner = retrieval.plan_queries(
            "如何利用知识图谱辅助实验设计？",
            question_en="How can knowledge graphs support experiment design?",
            max_queries=4,
            call_json=fake_call,
        )
        self.assertEqual(planner, "llm")
        self.assertLessEqual(len(queries), 4)
        self.assertEqual(
            [item["query"] for item in queries if item["source"] == "llm"],
            ["knowledge graph experiment design", "scientific decision support knowledge graph"],
        )

    def test_llm_failure_is_non_fatal(self) -> None:
        def broken(prompt):
            raise RuntimeError("planner offline")

        queries, planner = retrieval.plan_queries(
            "知识图谱 实验设计", use_query_planner=True, call_json=broken
        )
        self.assertEqual(planner, "fallback")
        self.assertTrue(queries)
        self.assertTrue(all(item["source"] != "llm" for item in queries))


class CandidateRankingTest(unittest.TestCase):
    def test_multi_query_hit_outranks_single_query_rich_graph(self) -> None:
        two_hits = {"query_positions": [0, 1], "search_score": 0.5,
                    "node_count": 10, "conclusion_count": 2}
        one_hit = {"query_positions": [0], "search_score": 0.9,
                   "node_count": 30, "conclusion_count": 5}
        self.assertGreater(retrieval._candidate_rank(two_hits),
                           retrieval._candidate_rank(one_hit))

    def test_search_many_merges_and_ranks(self) -> None:
        def fake_search(query, index, top_k, run_dir, runtime, **kwargs):
            if index == 0:
                return ([{"lkm_id": "1", "title": "A", "search_score": 0.2},
                         {"lkm_id": "2", "title": "B", "search_score": 0.1}], "")
            return ([{"lkm_id": "1", "title": "A", "search_score": 0.8},
                     {"lkm_id": "3", "title": "C", "search_score": 0.2}], "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._search_one", side_effect=fake_search):
                candidates, audits = retrieval.search_many(
                    [{"query": "q1", "source": "english"},
                     {"query": "q2", "source": "llm"}],
                    5, root, runtime,
                )
        self.assertEqual(len(audits), 2)
        self.assertEqual(candidates[0]["record"]["lkm_id"], "1")
        self.assertEqual(candidates[0]["query_positions"], [0, 1])
        self.assertAlmostEqual(candidates[0]["search_score"], 0.8)

    def test_search_scores_come_from_variables(self) -> None:
        payload = {
            "ok": True,
            "data": {
                "papers": {"paper:1": {"en_title": "A"}},
                "variables": [{"paper_id": "1", "rerank_score": 0.7}],
            },
        }
        records = retrieval._records_from_search(payload)
        self.assertAlmostEqual(records[0]["search_score"], 0.7)


def _graph(nodes: int, conclusions: int = 1) -> dict:
    graph_nodes = [
        {"kind": "conclusion", "content": f"conclusion {index}"}
        for index in range(conclusions)
    ]
    graph_nodes += [
        {"kind": "highlight", "content": f"highlight {index}"}
        for index in range(max(0, nodes - conclusions))
    ]
    return {"code": 0, "data": {"papers": [{
        "graph": {"nodes": graph_nodes, "edges": []},
        "paper": {"en_title": "Paper"},
    }]}}


def _candidate(pid: str) -> dict:
    return {"record": {"lkm_id": pid, "title": pid}, "query_positions": [0],
            "search_score": 0.5}


class DedupeTest(unittest.TestCase):
    def test_doi_versions_collapse(self) -> None:
        a = retrieval._dedupe_key({"doi": "10.32920/ryerson.14652537"})
        b = retrieval._dedupe_key({"doi": "10.32920/ryerson.14652537.v1"})
        self.assertEqual(a, b)

    def test_titles_collapse(self) -> None:
        a = retrieval._dedupe_key({"title": "Aircraft Design: A Survey"})
        b = retrieval._dedupe_key({"title": "aircraft design - a survey"})
        self.assertEqual(a, b)


class RelevanceTest(unittest.TestCase):
    def test_topic_phrases_beat_generic_hits(self) -> None:
        queries = [{"query": "knowledge graph experiment design decision support"}]
        relevant = retrieval._relevance(
            {"title": "Automating discovery through Knowledge Graphs and LLM Agents",
             "abstract": "scientific experiment design and decision support"}, queries)
        generic = retrieval._relevance(
            {"title": "Towards Continuous Experiment-driven MLOps",
             "abstract": "deployment pipelines and monitoring"}, queries)
        self.assertGreater(relevant, generic)
        self.assertGreaterEqual(relevant, 0.2)


class DedupeTest(unittest.TestCase):
    def test_doi_versions_collapse(self) -> None:
        a = retrieval._dedupe_key({"doi": "10.32920/ryerson.14652537"})
        b = retrieval._dedupe_key({"doi": "10.32920/ryerson.14652537.v1"})
        self.assertEqual(a, b)

    def test_titles_collapse(self) -> None:
        a = retrieval._dedupe_key({"title": "Aircraft Design: A Survey"})
        b = retrieval._dedupe_key({"title": "aircraft design - a survey"})
        self.assertEqual(a, b)


class RelevanceTest(unittest.TestCase):
    def test_aircraft_paper_scores_above_gearshift(self) -> None:
        queries = [{"query": "How can aircraft simulation design be carried out?"}]
        aircraft = retrieval._relevance(
            {"title": "A Simulation-Based Performance Analysis Tool for Aircraft Design Workflows"},
            queries,
        )
        off_topic = retrieval._relevance(
            {"title": "Multi-Objective Optimal Gearshift Control for Multispeed Transmission Electric Vehicles"},
            queries,
        )
        self.assertGreater(aircraft, 0.2)
        self.assertLess(off_topic, 0.1)


class PdfResolutionTest(unittest.TestCase):
    def test_arxiv_pdf_url(self) -> None:
        urls = retrieval._pdf_urls({"arxiv": "2501.00001"})
        self.assertIn("https://arxiv.org/pdf/2501.00001", urls)

    def test_openalex_finds_repository_pdf(self) -> None:
        payload = {
            "best_oa_location": {"pdf_url": "https://repo.example/a.pdf"},
            "locations": [{"pdf_url": "https://repo.example/b.pdf"}],
            "open_access": {"oa_url": "https://doi.org/10.1000/x"},
        }

        with mock.patch("ariadne.retrieval._http_json", return_value=payload), \
                mock.patch("ariadne.retrieval._landing_pdf_urls", return_value=[]):
            urls = retrieval._openalex_work_urls(doi="10.1000/x")
        self.assertEqual(urls[:2], ["https://repo.example/a.pdf",
                                    "https://repo.example/b.pdf"])

    def test_doi_is_treated_as_extractable_source(self) -> None:
        self.assertTrue(retrieval._can_extract({"doi": "10.1000/x"}))
        self.assertFalse(retrieval._can_extract({"lkm_id": "1"}))


class ExtractGraphTest(unittest.TestCase):
    def test_extract_submit_and_result_are_parsed(self) -> None:
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "submit" in cmd:
                payload = {"ok": True, "data": {"task_id": "task-1",
                                                "status": "succeeded",
                                                "cache_hit": False}}
            else:
                payload = {"ok": True, "data": {
                    "graph": {"nodes": [{"kind": "conclusion",
                                         "content": "Extracted conclusion"}],
                              "edges": []},
                    "addressed_problems": [], "open_questions": [],
                }}
            return subprocess.CompletedProcess(cmd, 0,
                                               stdout=json.dumps(payload),
                                               stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            dest = root / "probe" / "1.json"
            record = {"lkm_id": "1", "title": "Paper", "arxiv": "2501.00001"}
            with mock.patch("ariadne.retrieval._download_pdf",
                            return_value=root / "source.pdf"), \
                    mock.patch("ariadne.retrieval.run", side_effect=fake_run):
                path, graph, meta = retrieval._extract_graph(record, dest, root, runtime)
                self.assertTrue(path.is_file())
                self.assertEqual(retrieval._graph_stats(graph)["node_count"], 1)
        self.assertEqual(meta["graph_source"], "lkm_extract")
        self.assertEqual(meta["extract_task_id"], "task-1")
        self.assertTrue(any("submit" in call for call in calls))
        self.assertTrue(any("result" in call for call in calls))


class ProbeSelectionTest(unittest.TestCase):
    def test_skips_empty_and_thin_graphs(self) -> None:
        graphs = {
            "thin": _graph(2, 0),
            "rich1": _graph(6, 2),
            "rich2": _graph(8, 3),
        }

        def fake_fetch(record, dest, run_dir, runtime):
            pid = record["lkm_id"]
            if pid == "empty":
                raise retrieval.GraphUnavailable("empty", reason="empty_graph")
            graph = graphs[pid]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(graph), encoding="utf-8")
            return dest, graph

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._fetch_graph", side_effect=fake_fetch):
                selected, audit = retrieval.probe_and_select(
                    [_candidate("empty"), _candidate("thin"),
                     _candidate("rich1"), _candidate("rich2")],
                    root, runtime, top_k=2, candidate_limit=4,
                    min_papers=2, min_graph_nodes=3,
                )
            self.assertEqual({item["record"]["lkm_id"] for item in selected},
                             {"rich1", "rich2"})
            reasons = {item["lkm_id"]: item["skip_reason"]
                       for item in audit["candidates"]}
            self.assertEqual(reasons["empty"], "empty_graph")
            self.assertEqual(reasons["thin"], "thin_graph")
            self.assertTrue((root / "01_retrieval" / "selection.json").is_file())

    def test_raises_when_too_few_usable_papers(self) -> None:
        def fake_fetch(record, dest, run_dir, runtime):
            graph = _graph(5, 1)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(graph), encoding="utf-8")
            return dest, graph

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._fetch_graph", side_effect=fake_fetch):
                with self.assertRaises(AriadneError):
                    retrieval.probe_and_select(
                        [_candidate("only")], root, runtime, top_k=2,
                        candidate_limit=2, min_papers=2, min_graph_nodes=3,
                    )

    def test_empty_native_graph_can_be_built_with_lkm_extract(self) -> None:
        def fake_fetch(record, dest, run_dir, runtime):
            raise retrieval.GraphUnavailable("empty", reason="empty_graph")

        def fake_extract(record, dest, run_dir, runtime, pdf=None):
            graph = _graph(6, 2)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(graph), encoding="utf-8")
            return dest, graph, {
                "graph_source": "lkm_extract",
                "extract_task_id": "task-1",
                "extract_status": "succeeded",
            }

        candidate = _candidate("p1")
        candidate["record"]["arxiv"] = "2501.00001"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._fetch_graph", side_effect=fake_fetch), \
                    mock.patch("ariadne.retrieval._download_pdf",
                               return_value=root / "source.pdf"), \
                    mock.patch("ariadne.retrieval._extract_graph", side_effect=fake_extract):
                selected, audit = retrieval.probe_and_select(
                    [candidate], root, runtime, top_k=1, candidate_limit=1,
                    min_papers=1, min_graph_nodes=3,
                    use_lkm_extract=True, max_extractions=1,
                )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["graph_source"], "lkm_extract")
        self.assertEqual(audit["extract_attempts"], 1)
        self.assertEqual(audit["extracted"], 1)

    def test_batch_builds_multiple_missing_graphs(self) -> None:
        def fake_fetch(record, dest, run_dir, runtime):
            raise retrieval.GraphUnavailable("empty", reason="empty_graph")

        def fake_extract(record, dest, run_dir, runtime, pdf=None):
            graph = _graph(6, 2)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(graph), encoding="utf-8")
            return dest, graph, {
                "graph_source": "lkm_extract",
                "extract_task_id": "task-" + str(record["lkm_id"]),
                "extract_status": "succeeded",
            }

        candidates = []
        for pid in ("p1", "p2"):
            candidate = _candidate(pid)
            candidate["record"]["arxiv"] = "2501.00001"
            candidates.append(candidate)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._fetch_graph", side_effect=fake_fetch), \
                    mock.patch("ariadne.retrieval._download_pdf",
                               return_value=root / "source.pdf"), \
                    mock.patch("ariadne.retrieval._extract_graph", side_effect=fake_extract):
                selected, audit = retrieval.probe_and_select(
                    candidates, root, runtime, top_k=2, candidate_limit=2,
                    min_papers=2, min_graph_nodes=3,
                    use_lkm_extract=True, max_extractions=2,
                )
        self.assertEqual(len(selected), 2)
        self.assertEqual(audit["extract_attempts"], 2)
        self.assertEqual(audit["extracted"], 2)

    def test_does_not_fill_with_off_topic_papers(self) -> None:
        def fake_fetch(record, dest, run_dir, runtime):
            graph = _graph(5, 1)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(graph), encoding="utf-8")
            return dest, graph

        candidates = []
        for index in range(5):
            candidate = _candidate(f"p{index}")
            candidate["relevance"] = 0.4 if index < 3 else 0.0
            candidates.append(candidate)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._fetch_graph", side_effect=fake_fetch):
                selected, audit = retrieval.probe_and_select(
                    candidates, root, runtime, top_k=5, candidate_limit=5,
                    min_papers=2, min_graph_nodes=3, min_relevance=0.2,
                )
        self.assertEqual(len(selected), 3)
        self.assertEqual(audit["fill_floor"], 0.1)
        self.assertEqual(audit["filled"], 0)

    def test_fills_top_k_from_remaining_graphs(self) -> None:
        def fake_fetch(record, dest, run_dir, runtime):
            graph = _graph(5, 1)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(graph), encoding="utf-8")
            return dest, graph

        candidates = []
        for index in range(5):
            candidate = _candidate(f"p{index}")
            candidate["relevance"] = 0.4 if index < 3 else 0.1
            candidates.append(candidate)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._fetch_graph", side_effect=fake_fetch):
                selected, audit = retrieval.probe_and_select(
                    candidates, root, runtime, top_k=5, candidate_limit=5,
                    min_papers=2, min_graph_nodes=3, min_relevance=0.2,
                )
        self.assertEqual(len(selected), 5)
        self.assertEqual(audit["preferred"], 3)

    def test_extends_probe_when_first_pass_has_too_few_graphs(self) -> None:
        def fake_fetch(record, dest, run_dir, runtime):
            pid = record["lkm_id"]
            if pid in {"e1", "e2", "e3", "e4"}:
                raise retrieval.GraphUnavailable("empty", reason="empty_graph")
            graph = _graph(5, 1)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(graph), encoding="utf-8")
            return dest, graph

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._fetch_graph", side_effect=fake_fetch):
                selected, audit = retrieval.probe_and_select(
                    [_candidate(pid) for pid in ("e1", "e2", "e3", "e4", "g1", "g2")],
                    root, runtime, top_k=2, candidate_limit=2,
                    min_papers=2, min_graph_nodes=3,
                )
        self.assertEqual(len(selected), 2)
        self.assertEqual(audit["probed"], 6)
        self.assertGreater(audit["max_probe"], audit["first_pass"])

    def test_materialise_selected_writes_text_and_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            probe = root / "01_retrieval" / "probe" / "1.json"
            probe.parent.mkdir(parents=True)
            probe.write_text(json.dumps(_graph(5, 2)), encoding="utf-8")
            selected = [{
                "record": {"lkm_id": "1", "title": "Paper", "abstract": ""},
                "probe_path": "01_retrieval/probe/1.json",
                "query_positions": [0], "rank_score": 4.2,
                "node_count": 5, "conclusion_count": 2,
            }]
            papers = retrieval.materialise_selected(
                selected, root, runtime, fulltext=False
            )
            self.assertEqual(papers[0]["lkm_id"], "1")
            self.assertTrue((root / papers[0]["graph_path"]).is_file())
            text = (root / papers[0]["ocr_path"]).read_text(encoding="utf-8")
            self.assertIn("conclusion 0", text)


class BohrCommandTest(unittest.TestCase):
    def test_command_without_profile_or_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(
                Path(tmp), bohr_profile="", auto_approve_spend=False,
                payment_source="",
            )
            from ariadne.config import bohr_command

            command = bohr_command(runtime, "lkm", "search", "q")
            self.assertEqual(command[0], "/usr/local/bin/bohr")
            self.assertNotIn("--profile", command)
            self.assertNotIn("--yes", command)
            self.assertNotIn("--payment-source", command)


if __name__ == "__main__":
    unittest.main()
