"""Tests for independent literature sources (no network access)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ariadne import sources


OPENALEX_PAYLOAD = {
    "results": [{
        "id": "https://openalex.org/W1",
        "title": "Aircraft Design Survey",
        "doi": "https://doi.org/10.1234/ABC",
        "publication_year": 2024,
        "primary_location": {
            "source": {"display_name": "Journal of Aircraft"},
            "landing_page_url": "https://arxiv.org/abs/2501.00001",
        },
        "best_oa_location": {"pdf_url": "https://example.org/a.pdf"},
        "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
        "abstract_inverted_index": {"Aircraft": [0], "design": [1]},
    }],
}

CROSSREF_PAYLOAD = {
    "message": {"items": [{
        "DOI": "10.1234/abc",
        "title": ["Aircraft Design Survey"],
        "issued": {"date-parts": [[2023, 1]]},
        "container-title": ["Journal of Aircraft"],
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "link": [{"URL": "https://example.org/a.pdf",
                  "content-type": "application/pdf"}],
    }]},
}

ARXIV_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2501.00001v2</id>
    <title>Aircraft Design Survey</title>
    <summary>An abstract.</summary>
    <published>2025-01-01T00:00:00Z</published>
    <author><name>Ada Lovelace</name></author>
    <arxiv:doi>10.1234/abc</arxiv:doi>
    <link href="http://arxiv.org/abs/2501.00001v2" rel="alternate"/>
    <link href="http://arxiv.org/pdf/2501.00001v2" title="pdf"/>
  </entry>
</feed>"""

S2_PAYLOAD = {
    "data": [{
        "title": "Aircraft Design Survey",
        "externalIds": {"DOI": "10.1234/abc", "ArXiv": "2501.00001"},
        "year": 2024,
        "venue": "Journal of Aircraft",
        "authors": [{"name": "Ada Lovelace"}],
        "openAccessPdf": {"url": "https://example.org/a.pdf"},
        "url": "https://www.semanticscholar.org/paper/abc",
    }],
}


class NormalisationTest(unittest.TestCase):
    def test_doi_prefix_and_case_are_removed(self) -> None:
        self.assertEqual(
            sources.normalise_doi("https://doi.org/10.1234/ABC."), "10.1234/abc"
        )
        self.assertEqual(sources.normalise_doi("doi: 10.1234/abc"), "10.1234/abc")
        self.assertEqual(sources.normalise_doi(None), "")

    def test_arxiv_prefix_and_version_are_removed(self) -> None:
        self.assertEqual(
            sources.normalise_arxiv("arXiv:2501.00001v2"), "2501.00001"
        )
        self.assertEqual(
            sources.normalise_arxiv("https://arxiv.org/pdf/2501.00001.pdf"),
            "2501.00001",
        )

    def test_arxiv_id_is_recovered_from_urls_and_dois(self) -> None:
        self.assertEqual(
            sources.arxiv_from_urls("https://arxiv.org/abs/2501.00001v1"),
            "2501.00001",
        )
        self.assertEqual(sources.normalise_doi("10.48550/arXiv.2501.00001"),
                         "10.48550/arxiv.2501.00001")


class TitleMatchTest(unittest.TestCase):
    def test_case_punctuation_and_markup_are_tolerated(self) -> None:
        self.assertTrue(sources.same_title(
            "Aircraft Design: A Survey", "aircraft design - a survey"
        ))
        self.assertTrue(sources.same_title(
            "<i>Aircraft</i> Design &amp; Control", "Aircraft Design and Control"
        ))

    def test_different_papers_do_not_match(self) -> None:
        self.assertFalse(sources.same_title(
            "Aircraft Design Survey", "Aircraft Design Optimization"
        ))
        self.assertFalse(sources.same_title(
            "Learning to Reason with Graphs", "Learning to Reason with Tools"
        ))

    def test_short_titles_need_an_exact_match(self) -> None:
        self.assertTrue(sources.same_title("Graph Learning", "graph learning"))
        self.assertFalse(sources.same_title("Graph Learning", "Graph Reasoning"))

    def test_chinese_titles_compare_compactly(self) -> None:
        self.assertTrue(sources.same_title("飞机仿真设计", "飞机仿真设计"))
        self.assertFalse(sources.same_title("飞机仿真设计", "飞机结构设计"))


class SourceIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.index = sources.SourceIndex([
            sources.SourceRecord(
                "openalex", "Aircraft Design Survey", doi="10.1234/abc"
            ),
            sources.SourceRecord("crossref", "Aircraft design - a survey"),
            sources.SourceRecord(
                "arxiv", "Aircraft Design Survey", arxiv="2501.00001"
            ),
        ])

    def test_doi_match_prefers_the_strongest_reason(self) -> None:
        match = self.index.match(
            doi="https://doi.org/10.1234/ABC",
            title="Aircraft Design Survey",
        )
        self.assertEqual(match["openalex"], "doi")
        self.assertEqual(match["crossref"], "title")
        self.assertEqual(match["arxiv"], "title")

    def test_sources_are_deduplicated_per_source(self) -> None:
        records = [
            sources.SourceRecord(
                "openalex", "Aircraft Design Survey", doi="10.1234/abc"
            ),
            sources.SourceRecord(
                "openalex", "Aircraft Design Survey", doi="10.1234/abc"
            ),
        ]
        index = sources.SourceIndex(records)
        self.assertEqual(
            index.sources_for(doi="10.1234/abc"), ["openalex"]
        )

    def test_discovery_requires_two_independent_sources(self) -> None:
        index = sources.SourceIndex([
            sources.SourceRecord(
                "openalex", "Missed Paper on Flight Control", doi="10.9/z"
            ),
            sources.SourceRecord(
                "crossref", "Missed Paper on Flight Control", doi="10.9/z"
            ),
            sources.SourceRecord("s2", "Only One Source Paper", doi="10.8/y"),
        ])
        found = index.discover(min_sources=2)
        self.assertEqual([item["record"].doi for item in found], ["10.9/z"])
        self.assertEqual(found[0]["sources"], ["crossref", "openalex"])

    def test_discovery_skips_known_papers(self) -> None:
        index = sources.SourceIndex([
            sources.SourceRecord(
                "openalex", "Known Paper", doi="10.9/z"
            ),
            sources.SourceRecord("crossref", "Known Paper", doi="10.9/z"),
        ])
        self.assertEqual(index.discover(exclude_dois=["10.9/z"]), [])
        self.assertEqual(index.discover(exclude_titles=["Known Paper"]), [])


class AdapterParsingTest(unittest.TestCase):
    def test_openalex_search(self) -> None:
        with mock.patch.object(sources, "_json",
                               return_value=OPENALEX_PAYLOAD) as fetch:
            records = sources.search_openalex("aircraft design", 3)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.doi, "10.1234/abc")
        self.assertEqual(record.arxiv, "2501.00001")
        self.assertEqual(record.venue, "Journal of Aircraft")
        self.assertEqual(record.abstract, "Aircraft design")
        self.assertEqual(record.pdf_url, "https://example.org/a.pdf")
        self.assertIn("per-page=3", fetch.call_args.args[0])

    def test_crossref_search(self) -> None:
        with mock.patch.object(sources, "_json",
                               return_value=CROSSREF_PAYLOAD):
            records = sources.search_crossref("aircraft design", 3)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].year, 2023)
        self.assertEqual(records[0].authors, "Ada Lovelace")
        self.assertEqual(records[0].pdf_url, "https://example.org/a.pdf")

    def test_arxiv_search_builds_terms_and_parses_atom(self) -> None:
        with mock.patch.object(sources, "_get",
                               return_value=ARXIV_FEED) as fetch:
            records = sources.search_arxiv("aircraft design", 3)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].arxiv, "2501.00001")
        self.assertEqual(records[0].doi, "10.1234/abc")
        self.assertEqual(records[0].pdf_url,
                         "http://arxiv.org/pdf/2501.00001v2")
        self.assertIn("all%3Aaircraft", fetch.call_args.args[0])

    def test_semantic_scholar_search(self) -> None:
        with mock.patch.object(sources, "_json", return_value=S2_PAYLOAD):
            records = sources.search_semantic_scholar("aircraft design", 3)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].arxiv, "2501.00001")
        self.assertEqual(records[0].pdf_url, "https://example.org/a.pdf")

    def test_arxiv_doi_lookup_uses_the_id_list_endpoint(self) -> None:
        with mock.patch.object(sources, "_get",
                               return_value=ARXIV_FEED) as fetch:
            record = sources.lookup_arxiv("10.48550/arXiv.2501.00001")
        self.assertIsNotNone(record)
        self.assertIn("id_list=2501.00001", fetch.call_args.args[0])

    def test_transport_failure_is_reported(self) -> None:
        with mock.patch.object(
            sources, "_get",
            side_effect=sources.SourceUnavailable("HTTP 429"),
        ):
            with self.assertRaises(sources.SourceUnavailable):
                sources.search_arxiv("aircraft", 3)
        with mock.patch.object(
            sources, "_json",
            side_effect=sources.SourceUnavailable("HTTP 429"),
        ):
            with self.assertRaises(sources.SourceUnavailable):
                sources.search_openalex("aircraft", 3)


class CollectTest(unittest.TestCase):
    def test_collect_fans_out_and_survives_one_broken_source(self) -> None:
        def fake_openalex(query: str, limit: int):
            return [sources.SourceRecord(
                "openalex", f"Paper {query}", doi="10.1/a"
            )]

        def broken(query: str, limit: int):
            raise RuntimeError("429 too many requests")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(sources.SOURCES, {
                "openalex": sources.Source("openalex", fake_openalex),
                "crossref": sources.Source("crossref", broken),
            }, clear=False):
                index, audit = sources.collect(
                    [{"query": "q1"}, {"query": "q2"}],
                    per_query=4,
                    source_names="openalex,crossref",
                    run_dir=root,
                )
            self.assertEqual(len(index.records), 1)
            by_name = {item["name"]: item for item in audit["sources"]}
            self.assertEqual(by_name["openalex"]["records"], 1)
            self.assertIn("429", by_name["crossref"]["error"])
            self.assertTrue(
                (root / "01_retrieval" / "sources" / "openalex.json").is_file()
            )

    def test_collect_is_disabled_without_sources(self) -> None:
        index, audit = sources.collect([{"query": "q"}], source_names="")
        self.assertFalse(audit["enabled"])
        self.assertEqual(index.records, [])

    def test_lookup_many_resolves_dois(self) -> None:
        def fake_lookup(doi: str):
            return sources.SourceRecord(
                "openalex", "Aircraft Design Survey", doi=doi
            )

        with mock.patch.dict(sources.SOURCES, {
            "openalex": sources.Source("openalex", lambda q, n: [], fake_lookup),
        }, clear=False):
            records, audit = sources.lookup_many(
                ["https://doi.org/10.1/A", "10.1/a"],
                source_names="openalex",
            )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].doi, "10.1/a")
        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]["found"])


class SourceSelectionTest(unittest.TestCase):
    def test_unknown_sources_are_dropped_at_runtime(self) -> None:
        self.assertEqual(
            sources.resolve_source_names("openalex,typo,crossref"),
            ["openalex", "crossref"],
        )

    def test_parse_source_names_preserves_order_without_duplicates(self) -> None:
        self.assertEqual(
            sources.parse_source_names("arxiv, openalex,arxiv"),
            ["arxiv", "openalex"],
        )


if __name__ == "__main__":
    unittest.main()
