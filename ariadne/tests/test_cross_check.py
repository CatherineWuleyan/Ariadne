"""Tests for multi-source cross-validation in retrieval (no network)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ariadne import retrieval, sources
from ariadne.config import Config, Runtime


def _runtime(tmp: Path) -> Runtime:
    return Runtime(
        config=Config.load(tmp / "config.json"),
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


def _candidate(pid: str, title: str, doi: str = "",
               relevance: float = 0.5, **extra) -> dict:
    candidate = {
        "record": {"lkm_id": pid, "title": title, "doi": doi, "source": "lkm"},
        "query_positions": [0],
        "search_score": 0.5,
        "relevance": relevance,
    }
    candidate.update(extra)
    return candidate


def _graph(nodes: int = 6, conclusions: int = 2) -> dict:
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


def _search_audit(enabled: bool = True) -> dict:
    return {
        "enabled": enabled,
        "queries": ["aircraft design"],
        "sources": [],
        "records": 0,
    }


class CrossCheckCandidateTest(unittest.TestCase):
    def test_matching_source_is_recorded_and_boosts_rank(self) -> None:
        candidates = {
            "lkm:1": _candidate("1", "Aircraft Design Survey", "10.1/x"),
            "lkm:2": _candidate("2", "Unrelated Paper", "10.2/y"),
        }
        index = sources.SourceIndex([
            sources.SourceRecord(
                "openalex", "Aircraft Design Survey", doi="10.1/x"
            ),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                retrieval.literature, "collect",
                return_value=(index, _search_audit()),
            ), mock.patch.object(
                retrieval.literature, "lookup_many", return_value=([], []),
            ):
                audit = retrieval.cross_check_candidates(
                    candidates, [{"query": "aircraft design"}], root
                )
            confirmed = candidates["lkm:1"]
            self.assertEqual(confirmed["corroboration"], 1)
            self.assertEqual(confirmed["cross_check"]["status"],
                             "single_source")
            self.assertEqual(confirmed["verified_sources"], ["openalex"])
            self.assertEqual(candidates["lkm:2"]["corroboration"], 0)
            self.assertGreater(
                retrieval._candidate_rank(confirmed),
                retrieval._candidate_rank(candidates["lkm:2"]),
            )
            payload = json.loads(
                (root / "01_retrieval" / "sources.json").read_text()
            )
            self.assertTrue(payload["enabled"])
            self.assertEqual(audit["candidates"][0]["corroboration"], 1)

    def test_unverified_doi_is_resolved_directly(self) -> None:
        candidates = {
            "lkm:1": _candidate(
                "1", "Aircraft Design Survey", "https://doi.org/10.1/X"
            ),
        }
        confirmed = sources.SourceRecord(
            "crossref", "Aircraft Design Survey", doi="10.1/x"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                retrieval.literature, "collect",
                return_value=(sources.SourceIndex(), _search_audit()),
            ), mock.patch.object(
                retrieval.literature, "lookup_many",
                return_value=([confirmed], [{"doi": "10.1/x", "source": "crossref",
                                             "found": True}]),
            ) as lookup:
                audit = retrieval.cross_check_candidates(
                    candidates, [{"query": "aircraft design"}], root
                )
            self.assertEqual(candidates["lkm:1"]["corroboration"], 1)
            self.assertTrue(
                candidates["lkm:1"]["cross_check"]["lookup_used"]
            )
            self.assertEqual(candidates["lkm:1"]["verified_sources"],
                             ["crossref"])
            lookup.assert_called_once()
            self.assertEqual(len(audit["lookups"]), 1)

    def test_discovery_adds_papers_two_sources_agree_on(self) -> None:
        candidates = {
            "lkm:1": _candidate("1", "Known Paper", "10.1/x"),
        }
        index = sources.SourceIndex([
            sources.SourceRecord(
                "openalex", "Missed Flight Control Paper", doi="10.9/z"
            ),
            sources.SourceRecord(
                "crossref", "Missed Flight Control Paper", doi="10.9/z"
            ),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                retrieval.literature, "collect",
                return_value=(index, _search_audit()),
            ), mock.patch.object(
                retrieval.literature, "lookup_many", return_value=([], []),
            ):
                audit = retrieval.cross_check_candidates(
                    candidates, [{"query": "flight control"}], root,
                    discover=True, discovery_limit=2,
                )
            external = [
                entry for entry in candidates.values() if entry.get("external")
            ]
            self.assertEqual(len(external), 1)
            self.assertEqual(external[0]["record"]["source"], "external")
            self.assertEqual(external[0]["corroboration"], 2)
            self.assertEqual(external[0]["cross_check"]["status"],
                             "multi_source")
            self.assertEqual(audit["discovery"]["added"], 1)
            self.assertEqual(audit["discovery"]["papers"][0]["doi"], "10.9/z")

    def test_discovery_is_off_by_default(self) -> None:
        candidates = {"lkm:1": _candidate("1", "Known Paper", "10.1/x")}
        index = sources.SourceIndex([
            sources.SourceRecord(
                "openalex", "Missed Paper", doi="10.9/z"
            ),
            sources.SourceRecord("crossref", "Missed Paper", doi="10.9/z"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                retrieval.literature, "collect",
                return_value=(index, _search_audit()),
            ), mock.patch.object(
                retrieval.literature, "lookup_many", return_value=([], []),
            ):
                audit = retrieval.cross_check_candidates(
                    candidates, [{"query": "q"}], root
                )
        self.assertEqual(len(candidates), 1)
        self.assertFalse(audit["discovery"]["requested"])

    def test_disabled_collect_returns_without_touching_candidates(self) -> None:
        candidates = {"lkm:1": _candidate("1", "Paper", "10.1/x")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                retrieval.literature, "collect",
                return_value=(sources.SourceIndex(), _search_audit(False)),
            ):
                audit = retrieval.cross_check_candidates(
                    candidates, [{"query": "q"}], root
                )
        self.assertFalse(audit["enabled"])
        self.assertNotIn("corroboration", candidates["lkm:1"])


class SelectionTest(unittest.TestCase):
    def _fetch(self, record, dest, run_dir, runtime):
        graph = _graph()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(graph), encoding="utf-8")
        return dest, graph

    def test_corroborated_paper_is_preferred_over_higher_relevance(self) -> None:
        corroborated = _candidate(
            "confirmed", "Aircraft Design Survey", "10.1/x",
            relevance=0.8, corroboration=2,
        )
        unverified = _candidate(
            "unverified", "Gearshift Control", "10.2/y",
            relevance=0.95, corroboration=0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._fetch_graph",
                            side_effect=self._fetch):
                selected, audit = retrieval.probe_and_select(
                    [unverified, corroborated], root, runtime,
                    top_k=1, candidate_limit=2, min_papers=1,
                    min_graph_nodes=3, min_corroboration=1,
                )
        self.assertEqual(selected[0]["record"]["lkm_id"], "confirmed")
        self.assertEqual(audit["corroboration_floor"], 1)
        self.assertEqual(audit["pool_tier"], 1)

    def test_selection_falls_back_when_no_paper_is_corroborated(self) -> None:
        first = _candidate("a", "Paper A", "10.1/x", relevance=0.8)
        second = _candidate("b", "Paper B", "10.2/y", relevance=0.7)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._fetch_graph",
                            side_effect=self._fetch):
                selected, audit = retrieval.probe_and_select(
                    [first, second], root, runtime,
                    top_k=1, candidate_limit=2, min_papers=1,
                    min_graph_nodes=3, min_corroboration=1,
                )
        self.assertEqual(len(selected), 1)
        self.assertEqual(audit["pool_tier"], 2)

    def test_external_candidate_skips_native_graph_probe(self) -> None:
        external = {
            "record": {
                "lkm_id": "",
                "title": "Missed Flight Control Paper",
                "doi": "10.9/z",
                "arxiv": None,
                "abstract": "",
                "authors": "",
                "source": "external",
                "pdf_url": "https://example.org/paper.pdf",
            },
            "query_positions": [],
            "search_score": 0.0,
            "relevance": 0.9,
            "corroboration": 2,
            "external": True,
        }

        def fake_extract(record, dest, run_dir, runtime, pdf=None):
            graph = _graph()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(graph), encoding="utf-8")
            return dest, graph, {
                "graph_source": "lkm_extract",
                "extract_task_id": "task-9",
                "extract_status": "succeeded",
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch(
                "ariadne.retrieval._fetch_graph",
                side_effect=AssertionError("external papers have no LKM graph"),
            ), mock.patch(
                "ariadne.retrieval._download_pdf",
                return_value=root / "source.pdf",
            ), mock.patch(
                "ariadne.retrieval._extract_graph", side_effect=fake_extract,
            ):
                selected, audit = retrieval.probe_and_select(
                    [external], root, runtime, top_k=1, candidate_limit=1,
                    min_papers=1, min_graph_nodes=3,
                    use_lkm_extract=True, max_extractions=1,
                )
        self.assertEqual(len(selected), 1)
        self.assertEqual(audit["external_candidates"], 1)
        self.assertEqual(audit["extract_attempts"], 1)
        self.assertEqual(selected[0]["graph_source"], "lkm_extract")


class SearchManyHookTest(unittest.TestCase):
    def test_enrich_runs_before_ranking_and_shows_in_audit(self) -> None:
        def fake_search(query, index, top_k, run_dir, runtime, **kwargs):
            return ([{"lkm_id": "1", "title": "Aircraft Design Survey",
                      "doi": "10.1/x", "search_score": 0.2}], "")

        def enrich(candidates):
            for candidate in candidates.values():
                candidate["corroboration"] = 2
                candidate["cross_check"] = {"status": "multi_source"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._search_one",
                            side_effect=fake_search):
                candidates, _ = retrieval.search_many(
                    [{"query": "aircraft design", "source": "llm"}],
                    5, root, runtime, enrich=enrich,
                )
            payload = json.loads(
                (root / "01_retrieval" / "candidates.json").read_text()
            )
        self.assertEqual(candidates[0]["corroboration"], 2)
        self.assertAlmostEqual(
            candidates[0]["rank_score"],
            round(retrieval._candidate_rank(candidates[0]), 4),
        )
        self.assertEqual(payload["candidates"][0]["corroboration"], 2)


class RetrieveIntegrationTest(unittest.TestCase):
    def _run(self, cross, *, min_corroboration=1):
        seen: dict = {}

        def fake_search(query, index, top_k, run_dir, runtime, **kwargs):
            return ([{"lkm_id": "1", "title": "Aircraft Design Survey",
                      "doi": "10.1/x", "search_score": 0.4}], "")

        def fake_probe(candidates, run_dir, runtime, **kwargs):
            seen["floor"] = kwargs.get("min_corroboration")
            return [], {"candidates": [], "selected": []}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._search_one",
                            side_effect=fake_search), \
                    mock.patch("ariadne.retrieval.cross_check_candidates",
                               side_effect=cross), \
                    mock.patch("ariadne.retrieval.probe_and_select",
                               side_effect=fake_probe), \
                    mock.patch("ariadne.retrieval.materialise_selected",
                               return_value=[]):
                retrieval.retrieve(
                    "aircraft design", root, top_k=1, runtime=runtime,
                    use_query_planner=False, cross_check=True,
                    min_corroboration=min_corroboration,
                )
        return seen

    def test_cross_check_runs_before_selection(self) -> None:
        calls: list = []

        def cross(candidates, queries, run_dir, **kwargs):
            calls.append(dict(candidates))
            return {"enabled": True, "candidates": [], "sources": []}

        seen = self._run(cross, min_corroboration=2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(seen["floor"], 2)

    def test_cross_check_failure_never_blocks_retrieval(self) -> None:
        def cross(candidates, queries, run_dir, **kwargs):
            raise RuntimeError("all sources rate limited")

        seen = self._run(cross)
        self.assertEqual(seen["floor"], 0)

    def test_cross_check_can_be_disabled(self) -> None:
        seen: dict = {}

        def fake_probe(candidates, run_dir, runtime, **kwargs):
            seen["floor"] = kwargs.get("min_corroboration")
            return [], {"candidates": [], "selected": []}

        def fake_search(query, index, top_k, run_dir, runtime, **kwargs):
            return ([{"lkm_id": "1", "title": "Paper"}], "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _runtime(root)
            with mock.patch("ariadne.retrieval._search_one",
                            side_effect=fake_search), \
                    mock.patch("ariadne.retrieval.cross_check_candidates") as cross, \
                    mock.patch("ariadne.retrieval.probe_and_select",
                               side_effect=fake_probe), \
                    mock.patch("ariadne.retrieval.materialise_selected",
                               return_value=[]):
                retrieval.retrieve(
                    "aircraft design", root, top_k=1, runtime=runtime,
                    use_query_planner=False, cross_check=False,
                )
        cross.assert_not_called()
        self.assertEqual(seen["floor"], 0)


if __name__ == "__main__":
    unittest.main()
