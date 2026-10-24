"""Retrieve papers for a question and materialise their LKM graphs + text.

The live path is a small auditable retrieval pipeline rather than one search:

1. expand the question into several LKM queries (LLM planner + deterministic
   fallback queries);
2. run every query, merge and rank the candidate papers;
3. probe candidate paper graphs, skipping empty or conclusion-less graphs;
4. select the best graph-backed papers and materialise graph + text.

``--papers`` remains the deterministic offline path.

The ``bohr`` account comes from :mod:`ariadne.config`: either a dedicated
profile created by ``ariadne auth bohr`` or the user's active profile.  Billable
LKM calls only receive ``--yes`` after the user authorised spending (``--yes``
or ``bohr.auto_approve_spend``).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import sources as literature
from .config import Config, Runtime, bohr_command, resolve_runtime
from .util import AriadneError, REPO, read_json, run, stage_log, write_json

BOHR_TIMEOUT = 300
PDF_TIMEOUT = 900

DEFAULT_MAX_QUERIES = 5
DEFAULT_PER_QUERY = 8
DEFAULT_CANDIDATE_LIMIT = 16
DEFAULT_MIN_PAPERS = 2
DEFAULT_MIN_GRAPH_NODES = 3
DEFAULT_MIN_RELEVANCE = 0.2
DEFAULT_MAX_EXTRACTIONS = 5
DEFAULT_CROSS_CHECK = True
DEFAULT_CROSS_SOURCES = literature.DEFAULT_SOURCE_LIST
DEFAULT_MIN_CORROBORATION = 1
DEFAULT_CROSS_QUERIES = literature.DEFAULT_CROSS_QUERIES
DEFAULT_LOOKUP_BUDGET = 4
CROSS_SOURCE_WEIGHT = 1.0
_RELEVANCE_PHRASES = (
    "knowledge graph", "knowledge graphs", "knowledge model", "knowledge base",
    "graph reasoning", "experiment design", "experimental design",
    "experiment planning", "experiment configuration", "decision making",
    "decision support", "scientific discovery", "scientific experiment",
    "llm agent", "llm agents", "ai agent", "ai agents", "automated discovery",
    "research agent", "scientific agent",
    "aircraft design", "aircraft simulation", "flight simulation",
    "flight dynamics", "aerodynamic", "multidisciplinary design optimization",
    "hardware-in-the-loop",
)
_RELEVANCE_STOPWORDS = frozenset({
    "the", "and", "for", "with", "how", "can", "what", "why", "does", "into",
    "from", "that", "this", "are", "was", "were", "will", "would", "could",
    "support", "supporting", "using", "use", "used", "based", "toward",
    "towards", "about", "their", "its", "our", "your", "via", "per", "not",
})

# Chinese interrogative / filler phrases removed before the keyword fallback.
_STOP_PHRASES = sorted({
    "如何", "怎么", "怎样", "为什么", "哪些", "什么", "可以", "能够", "利用",
    "使用", "通过", "辅助", "以及", "请问", "帮我", "一下", "进行", "实现",
    "支持", "用于", "一个", "这种", "有哪些", "是否", "并且", "或者", "和",
    "与", "的", "吗", "呢", "？", "?", "。", "，", ",",
}, key=len, reverse=True)


class GraphUnavailable(AriadneError):
    """A paper exists in LKM but has no usable graph (not a fatal auth error)."""

    def __init__(self, message: str, *, reason: str = "unavailable") -> None:
        super().__init__(message)
        self.reason = reason


class ExtractUnavailable(AriadneError):
    """LKM extraction of a local/arXiv PDF could not produce a graph."""

    def __init__(self, message: str, *, reason: str = "extract_failed") -> None:
        super().__init__(message)
        self.reason = reason


def _runtime_or_resolve(runtime: Runtime | None) -> Runtime:
    if runtime is not None:
        return runtime
    return resolve_runtime(Config.load())


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _one_line(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


# --------------------------------------------------------------------------- #
# LKM search
# --------------------------------------------------------------------------- #

def _paper_id(value: Any, key: str = "") -> str:
    raw = str(value or key or "")
    if raw.startswith("paper:"):
        raw = raw.split(":", 1)[1]
    if re.fullmatch(r"local_[0-9a-f]{24}", raw):
        return raw
    match = re.search(r"(\d{6,})", raw)
    return match.group(1) if match else raw


def _arxiv_from_doi(doi: str) -> str | None:
    match = re.search(r"10\.48550/arxiv\.([0-9.]+)", str(doi or ""), re.I)
    return match.group(1) if match else None


def _authors(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value if item)
    return str(value or "")


def _normalise(record: Mapping[str, Any], key: str = "") -> dict[str, Any]:
    nested = record.get("paper")
    merged: dict[str, Any] = {}
    if isinstance(nested, Mapping):
        merged.update(nested)
    merged.update(record)
    pid = _paper_id(
        merged.get("lkm_id") or merged.get("paper_id") or merged.get("package_id")
        or merged.get("id"),
        key,
    )
    doi = str(merged.get("doi") or "")
    arxiv = (
        merged.get("arxiv") or merged.get("arxiv_id") or _arxiv_from_doi(doi)
    )
    title = str(
        merged.get("en_title") or merged.get("title") or merged.get("zh_title")
        or merged.get("paper_title") or pid
    )
    return {
        "lkm_id": pid,
        "title": title,
        "doi": doi,
        "arxiv": str(arxiv) if arxiv else None,
        "abstract": str(
            merged.get("en_abstract") or merged.get("abstract")
            or merged.get("summary") or ""
        ),
        "authors": _authors(merged.get("authors")),
        "source": "lkm",
        "graph_path": merged.get("graph_path"),
        "ocr_path": merged.get("ocr_path"),
        "pdf_path": merged.get("pdf_path"),
        "pdf_url": merged.get("pdf_url"),
        "search_score": _float(
            merged.get("rerank_score") or merged.get("score")
        ),
    }


def _search_scores(data: Mapping[str, Any]) -> dict[str, float]:
    """Collect the best rerank/lexical score per paper from search variables."""
    scores: dict[str, float] = {}
    for variable in data.get("variables") or []:
        if not isinstance(variable, Mapping):
            continue
        provenance = variable.get("provenance") or {}
        representative = provenance.get("representative_lcn") or {}
        pid = _paper_id(
            variable.get("paper_id") or provenance.get("package_id")
            or representative.get("package_id") or representative.get("local_id")
        )
        if not pid:
            continue
        score = _float(variable.get("rerank_score") or variable.get("score"))
        scores[pid] = max(scores.get(pid, 0.0), score)
    return scores


def _records_from_search(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Accept the documented LKM envelope plus a few older variants."""
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    records: list[dict[str, Any]] = []

    papers = data.get("papers")
    if isinstance(papers, Mapping):
        for key, value in papers.items():
            if isinstance(value, Mapping):
                records.append(_normalise(value, str(key)))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Mapping):
                        records.append(_normalise(item, str(key)))
    elif isinstance(papers, list):
        for item in papers:
            if isinstance(item, Mapping):
                records.append(_normalise(item))

    if not records:
        variables = data.get("variables") or data.get("items") or data.get("results") or []
        if isinstance(variables, Mapping):
            variables = [
                dict(value, _key=key) if isinstance(value, Mapping) else value
                for key, value in variables.items()
            ]
        if isinstance(variables, list):
            grouped: dict[str, dict[str, Any]] = {}
            for item in variables:
                if not isinstance(item, Mapping):
                    continue
                provenance = item.get("provenance") or {}
                representative = provenance.get("representative_lcn") or {}
                pid = _paper_id(
                    item.get("paper_id") or item.get("package_id")
                    or representative.get("package_id") or item.get("_key")
                )
                if not pid:
                    continue
                grouped.setdefault(pid, _normalise(item))
            records.extend(grouped.values())

    records = [record for record in records
               if record.get("lkm_id") or record.get("graph_path")]

    scores = _search_scores(data)
    for record in records:
        record["search_score"] = max(
            _float(record.get("search_score")),
            scores.get(str(record.get("lkm_id")), 0.0),
        )

    # Deduplicate without losing the first (highest-ranked) record.
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        pid = record.get("lkm_id") or record.get("title") or ""
        if pid in seen:
            continue
        seen.add(str(pid))
        deduped.append(record)
    return deduped


def _search_error_message(tail: str, log: Path) -> str:
    if "CONFIRMATION_REQUIRED" in tail:
        return (
            "LKM search needs billing confirmation. Re-run with `--yes` "
            "or set `bohr.auto_approve_spend=true`. " + f"See {log}"
        )
    return (
        "bohr lkm search failed. Check `ariadne auth bohr` / `bohr auth login`, "
        f"or use --papers. See {log}\n{tail}"
    )


def _search_one(query: str, index: int, top_k: int, run_dir: Path,
                runtime: Runtime, *, fatal: bool, raw_name: str
                ) -> tuple[list[dict[str, Any]], str]:
    command = bohr_command(
        runtime,
        "lkm", "search", query,
        "--top-k", str(max(1, min(top_k, 100))),
        "--mode", "hybrid",
        "--scopes", "conclusion,abstract",
    )
    log = stage_log(run_dir, f"01_search_{index:02d}")
    proc = run(command, log=log, timeout=BOHR_TIMEOUT, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        if fatal:
            raise AriadneError(_search_error_message(tail, log))
        return [], tail
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        message = f"LKM returned non-JSON output; see {log}"
        if fatal:
            raise AriadneError(message) from exc
        return [], message
    write_json(run_dir / "01_retrieval" / raw_name, payload)
    if payload.get("ok") is False:
        message = f"LKM rejected the search: {payload.get('error')}"
        if fatal:
            raise AriadneError(message)
        return [], message
    records = _records_from_search(payload)
    if fatal and not records:
        raise AriadneError(
            "LKM search returned no papers; try a broader question or --papers. "
            f"Raw response: {run_dir / '01_retrieval' / raw_name}"
        )
    return records[:top_k], ""


def search(question: str, top_k: int, run_dir: Path,
           runtime: Runtime | None = None) -> list[dict[str, Any]]:
    """Single-query search kept for compatibility and direct tests."""
    runtime = _runtime_or_resolve(runtime)
    records, _ = _search_one(
        question, 0, top_k, run_dir, runtime, fatal=True, raw_name="search_raw.json"
    )
    return records[:top_k]


def _candidate_rank(candidate: Mapping[str, Any]) -> float:
    positions = sorted({int(pos) for pos in candidate.get("query_positions") or []})
    hits = len(positions)
    early = sum(1.0 / (pos + 1) for pos in positions)
    search_score = min(max(_float(candidate.get("search_score")), 0.0), 1.0)
    nodes = min(int(candidate.get("node_count") or 0), 60) / 60.0
    conclusions = min(int(candidate.get("conclusion_count") or 0), 10) / 10.0
    relevance = min(max(_float(candidate.get("relevance")), 0.0), 1.0)
    # Independent agreement is capped at two extra sources: it should break
    # ties and lift corroborated papers, not drown out query/graph evidence.
    corroboration = min(int(candidate.get("corroboration") or 0), 2)
    return (
        2.0 * hits
        + 1.5 * early
        + 1.0 * search_score
        + 2.0 * relevance
        + 1.0 * nodes
        + 0.5 * conclusions
        + CROSS_SOURCE_WEIGHT * corroboration
    )


def _dedupe_key(record: Mapping[str, Any]) -> str:
    """Collapse DOI/title duplicates (LKM often has several paper records)."""
    doi = re.sub(r"\.v\d+$", "", str(record.get("doi") or "").strip().lower())
    if doi:
        return "doi:" + doi
    title = re.sub(r"[^a-z0-9]+", " ", str(record.get("title") or "").lower()).strip()
    if title:
        return "title:" + title
    return "id:" + str(record.get("lkm_id") or "")


def _relevance(record: Mapping[str, Any], queries: list[dict[str, Any]]) -> float:
    """Cheap lexical relevance used before/alongside graph probing.

    LKM's rerank score is useful but broad multi-query hits can otherwise
    promote generic papers.  This score rewards question terms and a small
    set of phrases that are central to the requested topic.
    """
    text = f"{record.get('title') or ''} {record.get('abstract') or ''}".lower()
    if not text.strip():
        return 0.0
    terms: set[str] = set()
    for item in queries:
        for token in re.findall(r"[a-z][a-z0-9+.-]{2,}", str(item.get("query") or "").lower()):
            if token not in _RELEVANCE_STOPWORDS:
                terms.add(token)
    if not terms:
        return 0.0
    coverage = sum(1 for term in terms if re.search(rf"\b{re.escape(term)}\b", text)) / max(1, min(len(terms), 12))
    phrase_hits = sum(1 for phrase in _RELEVANCE_PHRASES if phrase in text)
    phrase = min(phrase_hits / 3.0, 1.0)
    return round(0.55 * coverage + 0.45 * phrase, 4)


def _candidate_audit(candidate: Mapping[str, Any]) -> dict[str, Any]:
    record = candidate.get("record") or {}
    return {
        "lkm_id": record.get("lkm_id") or "",
        "title": record.get("title") or "",
        "doi": record.get("doi") or "",
        "source": record.get("source") or "",
        "query_positions": list(candidate.get("query_positions") or []),
        "best_rank": candidate.get("best_rank"),
        "search_score": round(_float(candidate.get("search_score")), 6),
        "relevance": round(_float(candidate.get("relevance")), 4),
        "rank_score": round(_float(candidate.get("rank_score")), 4),
        "corroboration": int(candidate.get("corroboration") or 0),
        "cross_check": dict(candidate.get("cross_check") or {}),
        "discovered_by": list(candidate.get("discovered_by") or []),
        "node_count": int(candidate.get("node_count") or 0),
        "conclusion_count": int(candidate.get("conclusion_count") or 0),
        "skip_reason": candidate.get("skip_reason") or "",
        "skip_detail": _one_line(candidate.get("skip_detail") or ""),
        "extract_attempted": bool(candidate.get("extract_attempted")),
        "extract_skip_reason": candidate.get("extract_skip_reason") or "",
        "graph_source": candidate.get("graph_source") or "",
        "extract_task_id": candidate.get("extract_task_id") or "",
        "extract_status": candidate.get("extract_status") or "",
        "probe_path": candidate.get("probe_path") or "",
    }


def search_many(queries: list[dict[str, Any]], per_query: int, run_dir: Path,
                runtime: Runtime, *, enrich: Any = None
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run every planned query and merge the ranked paper candidates.

    ``enrich`` is an optional callback that annotates the merged candidate
    mapping in place (cross-source verification does this) before relevance
    scoring and ranking, so the resulting order uses the extra evidence.
    """
    candidates: dict[str, dict[str, Any]] = {}
    audits: list[dict[str, Any]] = []
    for index, item in enumerate(queries):
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        records, error = _search_one(
            query, index, per_query, run_dir, runtime,
            fatal=False, raw_name=f"search_raw_{index:02d}.json",
        )
        audits.append({
            "index": index,
            "query": query,
            "source": item.get("source") or "",
            "records": len(records),
            "error": _one_line(error),
        })
        if error:
            print(f"[retrieve] query {index + 1} failed: {_one_line(error)[:180]}",
                  flush=True)
        for rank, record in enumerate(records):
            key = _dedupe_key(record)
            entry = candidates.get(key)
            if entry is None:
                entry = {
                    "record": record,
                    "query_positions": [],
                    "best_rank": rank,
                    "search_score": _float(record.get("search_score")),
                }
                candidates[key] = entry
            else:
                # Keep the richer record when the same DOI appears twice.
                old = entry.get("record") or {}
                for field in ("pdf_url", "pdf_path", "arxiv", "ocr_path", "graph_path"):
                    if record.get(field) and not old.get(field):
                        old[field] = record[field]
            entry["query_positions"].append(index)
            entry["best_rank"] = min(entry["best_rank"], rank)
            entry["search_score"] = max(
                entry["search_score"], _float(record.get("search_score"))
            )

    if not candidates:
        write_json(
            run_dir / "01_retrieval" / "selection.json",
            {"search": audits, "candidates": [], "selected": [], "probed": 0},
        )
        details = "; ".join(
            f"{audit['query']}: {audit['error']}" for audit in audits if audit["error"]
        )
        if "CONFIRMATION_REQUIRED" in details:
            raise AriadneError(
                "LKM search needs billing confirmation. Re-run with `--yes` "
                "or set `bohr.auto_approve_spend=true`."
            )
        raise AriadneError(
            "LKM search returned no papers for any planned query. "
            f"Search audit: {run_dir / '01_retrieval' / 'selection.json'}"
            + (f"\n{details}" if details else "")
        )

    for candidate in candidates.values():
        candidate["relevance"] = _relevance(candidate.get("record") or {}, queries)
    if enrich is not None:
        enrich(candidates)
    ordered = sorted(candidates.values(), key=_candidate_rank, reverse=True)
    for candidate in ordered:
        candidate["rank_score"] = round(_candidate_rank(candidate), 4)
    write_json(
        run_dir / "01_retrieval" / "candidates.json",
        {
            "queries": len(queries),
            "candidates": [_candidate_audit(candidate) for candidate in ordered],
        },
    )
    return ordered, audits


# --------------------------------------------------------------------------- #
# independent-source cross-validation
# --------------------------------------------------------------------------- #

def _cross_status(count: int) -> str:
    if count >= 2:
        return "multi_source"
    if count == 1:
        return "single_source"
    return "unverified"


def _apply_cross_match(candidate: dict[str, Any], match: Mapping[str, str],
                       *, lookup_used: bool = False) -> None:
    candidate["corroboration"] = len(match)
    candidate["verified_sources"] = sorted(match)
    candidate["cross_check"] = {
        "sources": sorted(match),
        "matched_by": dict(sorted(match.items())),
        "corroboration": len(match),
        "status": _cross_status(len(match)),
        "lookup_used": bool(lookup_used),
    }


def _external_candidate(record: literature.SourceRecord,
                        source_names: Iterable[str]) -> dict[str, Any]:
    """Turn an independently discovered paper into a normal candidate."""
    sources = sorted(set(source_names))
    normalized = {
        "lkm_id": "",
        "title": record.title,
        "doi": record.doi,
        "arxiv": record.arxiv or None,
        "abstract": record.abstract,
        "authors": record.authors,
        "source": "external",
        "graph_path": None,
        "ocr_path": None,
        "pdf_path": None,
        "pdf_url": record.pdf_url,
        "search_score": 0.0,
    }
    return {
        "record": normalized,
        "query_positions": [],
        "best_rank": None,
        "search_score": 0.0,
        "external": True,
        "discovered_by": sources,
        "corroboration": len(sources),
        "verified_sources": sources,
        "cross_check": {
            "sources": sources,
            "matched_by": {name: "discovery" for name in sources},
            "corroboration": len(sources),
            "status": "multi_source",
            "lookup_used": False,
            "discovered": True,
        },
        "relevance": 0.0,
    }


def cross_check_candidates(
    candidates: dict[str, dict[str, Any]],
    queries: list[dict[str, Any]],
    run_dir: Path,
    *,
    source_names: str = DEFAULT_CROSS_SOURCES,
    per_query: int = DEFAULT_PER_QUERY,
    min_corroboration: int = DEFAULT_MIN_CORROBORATION,
    discover: bool = False,
    discovery_limit: int = 4,
    lookup_budget: int = DEFAULT_LOOKUP_BUDGET,
) -> dict[str, Any]:
    """Annotate candidates with agreement from independent literature sources.

    The candidate mapping is mutated: every entry gains ``corroboration``,
    ``verified_sources`` and ``cross_check``.  With ``discover=True`` papers
    that several independent sources agree on but LKM missed are appended.
    """
    names = literature.resolve_source_names(source_names)
    index, audit = literature.collect(
        queries, per_query=per_query, source_names=names, run_dir=run_dir,
        max_queries=DEFAULT_CROSS_QUERIES,
    )
    audit["min_corroboration"] = int(min_corroboration)
    if not audit.get("enabled"):
        return audit

    for candidate in candidates.values():
        record = candidate.get("record") or {}
        _apply_cross_match(candidate, index.match(
            doi=record.get("doi"),
            title=record.get("title"),
            arxiv=record.get("arxiv"),
        ))

    # Query-level search cannot guarantee that a candidate appears in the top
    # results of every source; resolve the strongest unverified DOIs directly.
    pending = sorted(
        (
            candidate for candidate in candidates.values()
            if candidate.get("corroboration") == 0
            and (candidate.get("record") or {}).get("doi")
        ),
        key=_candidate_rank, reverse=True,
    )[:max(0, int(lookup_budget))]
    if pending:
        records, lookups = literature.lookup_many(
            [(candidate["record"] or {}).get("doi") for candidate in pending],
            source_names=names, limit=len(pending),
        )
        index.extend(records)
        for candidate in pending:
            record = candidate.get("record") or {}
            _apply_cross_match(candidate, index.match(
                doi=record.get("doi"),
                title=record.get("title"),
                arxiv=record.get("arxiv"),
            ), lookup_used=True)
        audit["lookups"] = lookups

    added: list[dict[str, Any]] = []
    if discover:
        known_dois = [
            (candidate.get("record") or {}).get("doi")
            for candidate in candidates.values()
        ]
        known_arxiv = [
            (candidate.get("record") or {}).get("arxiv")
            for candidate in candidates.values()
        ]
        known_titles = [
            (candidate.get("record") or {}).get("title")
            for candidate in candidates.values()
        ]
        for item in index.discover(
            exclude_dois=known_dois,
            exclude_arxiv=known_arxiv,
            exclude_titles=known_titles,
            min_sources=2,
            limit=discovery_limit,
        ):
            record = item["record"]
            key = "external:" + _dedupe_key(record.as_dict())
            if key in candidates:
                continue
            entry = _external_candidate(record, item["sources"])
            candidates[key] = entry
            known_dois.append(record.doi)
            known_arxiv.append(record.arxiv)
            known_titles.append(record.title)
            added.append({
                "key": key,
                "title": record.title,
                "doi": record.doi,
                "sources": item["sources"],
            })
    audit["discovery"] = {
        "requested": bool(discover),
        "added": len(added),
        "papers": added,
    }
    audit["candidates"] = [
        {
            "key": key,
            "lkm_id": (candidate.get("record") or {}).get("lkm_id") or "",
            "title": (candidate.get("record") or {}).get("title") or "",
            "doi": (candidate.get("record") or {}).get("doi") or "",
            "sources": list((candidate.get("cross_check") or {}).get("sources") or []),
            "status": (candidate.get("cross_check") or {}).get("status") or "",
            "corroboration": int(candidate.get("corroboration") or 0),
            "external": bool(candidate.get("external")),
        }
        for key, candidate in candidates.items()
    ]
    write_json(run_dir / "01_retrieval" / "sources.json", audit)
    return audit


# --------------------------------------------------------------------------- #
# query planning
# --------------------------------------------------------------------------- #

def _keyword_query(question: str) -> str:
    text = str(question or "")
    for phrase in _STOP_PHRASES:
        text = text.replace(phrase, " ")
    text = re.sub(r"[^\w\u4e00-\u9fff+.-]+", " ", text)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+.-]{1,}|[\u4e00-\u9fff]{2,4}", text)
    tokens = [token for token in tokens if token.lower() not in {"and", "the", "of"}]
    return " ".join(tokens[:12])


def _query_prompt(question: str, question_en: str, domain: str,
                  count: int) -> str:
    return (
        "You plan literature retrieval for a scientific knowledge graph. "
        "Produce short English search queries that will find papers about the "
        "question's mechanisms, methods, systems, and application setting. "
        "Do not write full sentences, boolean operators, or quotation marks.\n\n"
        f"Question (Chinese): {question}\n"
        f"Question (English): {question_en or '(none)'}\n"
        f"Domain: {domain or '(none)'}\n\n"
        f"Return exactly JSON with {count} queries: {{\"queries\":[\"...\"]}}"
    )


def _llm_queries(question: str, question_en: str, domain: str, count: int,
                 *, call_json: Any = None) -> list[str]:
    if count <= 0:
        return []
    prompt = _query_prompt(question, question_en, domain, count)
    try:
        if call_json is None:
            from pipeline_merge_question.step3 import _post_json
            payload = _post_json(prompt)
        else:
            payload = call_json(prompt)
    except Exception as exc:  # noqa: BLE001 - planner failure must fall back
        print(
            f"[retrieve] query planner unavailable ({str(exc)[:160]}); "
            "using deterministic queries",
            flush=True,
        )
        return []
    if not isinstance(payload, Mapping):
        return []
    items = payload.get("queries")
    if not isinstance(items, list):
        return []
    queries: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        query = item.strip().strip("\"'")
        if 5 <= len(query) <= 180 and query.lower() not in {
            value.lower() for value in queries
        }:
            queries.append(query)
        if len(queries) >= count:
            break
    return queries


def plan_queries(question: str, *, question_en: str = "", domain: str = "",
                 max_queries: int = DEFAULT_MAX_QUERIES,
                 use_query_planner: bool = True,
                 call_json: Any = None) -> tuple[list[dict[str, Any]], str]:
    """Build a small, auditable query set.  LLM failure is non-fatal."""
    max_queries = max(1, min(int(max_queries), 8))
    queries: list[dict[str, Any]] = []

    def add(text: str, source: str) -> None:
        value = " ".join(str(text or "").split()).strip()
        if not value:
            return
        if len(value) > 180:
            value = value[:180].rsplit(" ", 1)[0].strip()
        if not value:
            return
        if value.lower() in {item["query"].lower() for item in queries}:
            return
        queries.append({"query": value, "source": source})

    # English queries generally retrieve better from the mostly-English corpus,
    # so put them first; the original question is still always present.
    if question_en:
        add(question_en, "english")
    add(question, "question")

    if use_query_planner and len(queries) < max_queries:
        for query in _llm_queries(
            question, question_en, domain, max_queries - len(queries),
            call_json=call_json,
        ):
            add(query, "llm")

    if len(queries) < max_queries:
        add(_keyword_query(question), "keywords")
    if len(queries) < max_queries and domain:
        add(domain, "domain")

    planner = "llm" if any(item["source"] == "llm" for item in queries) else "fallback"
    return queries[:max_queries], planner


# --------------------------------------------------------------------------- #
# paper graph probing and selection
# --------------------------------------------------------------------------- #

def read_spec(path: Path) -> list[dict[str, Any]]:
    try:
        payload = read_json(path)
    except json.JSONDecodeError as exc:
        raise AriadneError(f"invalid JSON in --papers file {path}: {exc.msg}") from exc
    except OSError as exc:
        raise AriadneError(f"cannot read --papers file {path}: {exc}") from exc
    items = payload.get("papers") if isinstance(payload, Mapping) else payload
    if not isinstance(items, list):
        raise AriadneError("--papers must be a list, or {papers:[...]}")
    records = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        record = _normalise(item)
        record["source"] = "spec"
        if not record["lkm_id"] and not record.get("graph_path"):
            continue
        records.append(record)
    if not records:
        raise AriadneError("--papers contains no usable entries")
    return records


def _normalise_graph_payload(raw: Any) -> dict[str, Any]:
    """Wrap a graph in the ``{"code":0,"data":{"papers":[...]}}`` shape."""
    if isinstance(raw, list):
        return {"code": 0, "data": {"papers": raw}}
    if not isinstance(raw, Mapping):
        raise AriadneError("graph file must contain a JSON object or list")

    data = raw.get("data")
    if isinstance(data, Mapping):
        if "papers" in data:
            result = dict(raw)
            result.setdefault("code", 0)
            return result
        if "items" in data:
            return {"code": 0, "data": {"papers": data["items"]}}
    if isinstance(data, list):
        return {"code": 0, "data": {"papers": data}}
    if "papers" in raw:
        return {"code": 0, "data": {"papers": raw["papers"]}}
    if "items" in raw:
        return {"code": 0, "data": {"papers": raw["items"]}}
    if isinstance(raw.get("graph"), Mapping):
        return {"code": 0, "data": {"papers": [raw]}}
    if "nodes" in raw or "edges" in raw:
        return {"code": 0, "data": {"papers": [{"graph": raw, "paper": {}}]}}
    return {"code": 0, "data": {"papers": [raw]}}


def _local_paper_id(paper: Mapping[str, Any]) -> str:
    candidate = paper.get("lkm_id") or ""
    if candidate:
        return str(candidate)
    raw_path = str(paper.get("graph_path") or paper.get("ocr_path") or "")
    stem = Path(raw_path).stem
    if re.fullmatch(r"\d{6,}", stem):
        return stem
    # External records have no local path; their DOI or title keeps the id
    # stable across runs and distinct from every other paper.
    identity = (
        raw_path or str(paper.get("doi") or "") or str(paper.get("title") or "")
    )
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
    return f"local_{digest}"


def _resolve_local(value: Any) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = REPO / path
    return path


def _fetch_graph(paper: Mapping[str, Any], dest: Path, run_dir: Path,
                 runtime: Runtime) -> tuple[Path, dict[str, Any]]:
    local = paper.get("graph_path")
    if local:
        source = _resolve_local(local)
        if not source.is_file():
            raise AriadneError(f"graph_path not found: {source}")
        graph = _normalise_graph_payload(read_json(source))
        if not ((graph.get("data") or {}).get("papers") or []):
            raise GraphUnavailable(
                f"graph file has no paper items: {source}", reason="empty_graph"
            )
        write_json(dest, graph)
        return dest, graph
    pid = paper["lkm_id"]
    if not pid:
        raise AriadneError(f"paper {paper.get('title')} has no lkm_id/graph_path")
    command = bohr_command(runtime, "lkm", "graph", "--paper-id", str(pid))
    log = stage_log(run_dir, f"01_graph_{pid}")
    proc = run(command, log=log, timeout=BOHR_TIMEOUT, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        if "CONFIRMATION_REQUIRED" in tail:
            raise AriadneError(
                "LKM graph retrieval needs billing confirmation. Re-run with `--yes` "
                f"or set `bohr.auto_approve_spend=true`. See {log}"
            )
        # A missing/empty graph is a normal per-paper rejection even when the
        # CLI exits non-zero.  Parse the JSON envelope before failing the run.
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping) and payload.get("ok") is False:
            message = str((payload.get("error") or {}).get("message") or "")
            if "empty" in message.lower() or "no graph" in message.lower():
                raise GraphUnavailable(
                    f"LKM returned an empty graph for {pid}: {message}",
                    reason="empty_graph",
                )
            raise AriadneError(
                f"LKM returned no graph for {pid}: {payload.get('error')}"
            )
        raise AriadneError(f"bohr lkm graph failed for {pid}; see {log}\n{tail}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AriadneError(f"LKM graph for {pid} was not JSON; see {log}") from exc
    if payload.get("ok") is False:
        message = str((payload.get("error") or {}).get("message") or "")
        if "empty" in message.lower() or "no graph" in message.lower():
            raise GraphUnavailable(
                f"LKM returned an empty graph for {pid}: {message}",
                reason="empty_graph",
            )
        raise AriadneError(f"LKM returned no graph for {pid}: {payload.get('error')}")
    data = payload.get("data") or {}
    items = data.get("items") if isinstance(data, Mapping) else None
    if not items and isinstance(data, Mapping):
        items = data.get("papers")
    if not items and isinstance(data, list):
        items = data
    if not items:
        raise GraphUnavailable(
            f"LKM returned an empty graph for {pid}", reason="empty_graph"
        )
    graph = {"code": 0, "data": {"papers": items}}
    write_json(dest, graph)
    return dest, graph


def _graph_stats(graph: Mapping[str, Any]) -> dict[str, Any]:
    items = (graph.get("data") or {}).get("papers") or []
    nodes: list[Any] = []
    edges: list[Any] = []
    title = ""
    for item in items:
        if not isinstance(item, Mapping):
            continue
        paper_graph = item.get("graph") or {}
        if not title:
            meta = item.get("paper") or {}
            title = str(meta.get("en_title") or meta.get("zh_title") or "")
        nodes.extend(paper_graph.get("nodes") or [])
        edges.extend(paper_graph.get("edges") or [])
    conclusions = [
        node for node in nodes
        if isinstance(node, Mapping)
        and str(node.get("kind") or "").lower() == "conclusion"
    ]
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "conclusion_count": len(conclusions),
        "title": title,
    }


MAX_PDF_BYTES = 64 * 1024 * 1024


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


PDF_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)
PDF_TIMEOUT = 45


def _norm_title(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _http_json(url: str, timeout: int = 10) -> Mapping[str, Any] | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": PDF_UA})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return payload if isinstance(payload, Mapping) else None
    except Exception:  # noqa: BLE001 - PDF discovery is best-effort
        return None


def _http_page(url: str, timeout: int = 15) -> tuple[bytes, str]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": PDF_UA})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read(2_000_000), response.geturl()
    except Exception:  # noqa: BLE001
        return b"", url


def _landing_pdf_urls(url: str) -> list[str]:
    """Read a publisher landing page and extract its PDF / download links."""
    if not url:
        return []
    body, final = _http_page(url)
    if not body:
        return []
    text = body.decode("utf-8", errors="replace")
    found: list[str] = []
    for pattern in (
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
    ):
        found.extend(re.findall(pattern, text, re.I))
    for link in re.findall(r'href=["\']([^"\']+)["\']', text, re.I):
        low = link.lower()
        if ("pdf" in low or "download" in low) and not low.startswith(("#", "mailto:", "javascript:")):
            found.append(link)
    urls: list[str] = []
    for link in found:
        absolute = urljoin(final or url, link)
        if absolute not in urls:
            urls.append(absolute)
    return urls


def _mdpi_pdf_urls(doi: str) -> list[str]:
    match = re.match(r"10\.3390/([a-z]+)(\d)(\d{2})(\d{4})", doi or "", re.I)
    if not match:
        return []
    journal, volume, issue, article = match.groups()
    slug = f"{journal}-{int(volume):02d}-{article}"
    return [
        f"https://mdpi-res.com/d_attachment/{journal}/{slug}/"
        f"article_deploy/{slug}.pdf"
    ]


def _openalex_work_urls(doi: str = "", title: str = "") -> list[str]:
    payload = None
    if doi:
        payload = _http_json(
            "https://api.openalex.org/works/doi:" + quote(doi, safe="")
        )
    if payload is None and title:
        search = _http_json(
            "https://api.openalex.org/works?search="
            + quote(title, safe="") + "&per-page=5"
        )
        if search:
            wanted = _norm_title(title)
            for work in search.get("results") or []:
                if _norm_title(work.get("title")) == wanted:
                    payload = work
                    break
    if not isinstance(payload, Mapping):
        return []
    urls: list[str] = []
    landings: list[str] = []
    locations = [payload.get("best_oa_location")]
    locations.extend(payload.get("locations") or [])
    for location in locations:
        if not isinstance(location, Mapping):
            continue
        if location.get("pdf_url"):
            urls.append(str(location["pdf_url"]))
        landing = str(location.get("landing_page_url") or "")
        if landing:
            if landing.lower().split("?", 1)[0].endswith(".pdf"):
                urls.append(landing)
            else:
                landings.append(landing)
    # A publisher "pdf_url" can be a stale 401/403 link; the repository
    # landing page often exposes a working sibling PDF.  Parse one landing
    # even when OpenAlex already returned a PDF URL.
    for landing in list(dict.fromkeys(landings))[:1]:
        urls.extend(_landing_pdf_urls(landing))
    oa_url = (payload.get("open_access") or {}).get("oa_url")
    if oa_url:
        urls.append(str(oa_url))
    return urls


def _unpaywall_pdf_urls(doi: str) -> list[str]:
    payload = _http_json(
        "https://api.unpaywall.org/v2/" + quote(doi, safe="")
        + "?email=ariadne@example.org"
    )
    if not payload:
        return []
    urls: list[str] = []
    locations = [payload.get("best_oa_location")]
    locations.extend(payload.get("oa_locations") or [])
    for location in locations:
        if not isinstance(location, Mapping):
            continue
        if location.get("url_for_pdf"):
            urls.append(str(location["url_for_pdf"]))
    return urls


def _s2_pdf_urls(doi: str) -> list[str]:
    for attempt in range(2):
        payload = _http_json(
            "https://api.semanticscholar.org/graph/v1/paper/DOI:"
            + quote(doi, safe="") + "?fields=openAccessPdf",
            timeout=8,
        )
        if payload:
            pdf = (payload.get("openAccessPdf") or {}).get("url")
            return [str(pdf)] if pdf else []
        if attempt == 0:
            time.sleep(1)
    return []


def _crossref_pdf_urls(doi: str) -> list[str]:
    payload = _http_json(
        "https://api.crossref.org/works/" + quote(doi, safe="")
    )
    if not payload:
        return []
    message = payload.get("message") or {}
    return [
        str(link["URL"])
        for link in (message.get("link") or [])
        if isinstance(link, Mapping) and link.get("URL")
    ]


def _doi_landing_pdf_urls(doi: str) -> list[str]:
    return _landing_pdf_urls("https://doi.org/" + quote(doi, safe="/"))


def _pdf_urls(record: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    if record.get("pdf_url"):
        urls.append(str(record["pdf_url"]))
    arxiv = record.get("arxiv")
    if not arxiv:
        match = re.search(
            r"10\.48550/arxiv\.([0-9.]+)", str(record.get("doi") or ""), re.I
        )
        arxiv = match.group(1) if match else None
    if arxiv:
        urls.append(f"https://arxiv.org/pdf/{arxiv}")
    doi = str(record.get("doi") or "").strip()
    title = str(record.get("title") or "").strip()
    mdpi = _mdpi_pdf_urls(doi)
    if mdpi:
        return list(dict.fromkeys(urls + mdpi))
    if doi:
        urls.extend(_mdpi_pdf_urls(doi))
        urls.extend(_doi_landing_pdf_urls(doi))
        urls.extend(_openalex_work_urls(doi=doi))
        urls.extend(_unpaywall_pdf_urls(doi))
        urls.extend(_s2_pdf_urls(doi))
        urls.extend(_crossref_pdf_urls(doi))
    if title:
        urls.extend(_openalex_work_urls(title=title))
    if doi:
        urls.append(f"https://doi.org/{doi}")
    seen: set[str] = set()
    return [url for url in urls if not (url in seen or seen.add(url))]


def _can_extract(record: Mapping[str, Any]) -> bool:
    return bool(
        record.get("pdf_path") or record.get("pdf_url")
        or record.get("arxiv") or record.get("doi")
    )


def _pdf_url_groups(record: Mapping[str, Any]) -> Iterator[list[str]]:
    """Lazy URL groups: stop asking other services once a PDF downloads."""
    explicit = []
    if record.get("pdf_url"):
        explicit.append(str(record["pdf_url"]))
    arxiv = record.get("arxiv")
    if not arxiv:
        match = re.search(
            r"10\.48550/arxiv\.([0-9.]+)", str(record.get("doi") or ""), re.I
        )
        arxiv = match.group(1) if match else None
    if arxiv:
        explicit.append(f"https://arxiv.org/pdf/{arxiv}")
    if explicit:
        yield explicit
    doi = str(record.get("doi") or "").strip()
    title = str(record.get("title") or "").strip()
    if doi:
        mdpi = _mdpi_pdf_urls(doi)
        if mdpi:
            yield mdpi
        yield _doi_landing_pdf_urls(doi)
        yield _openalex_work_urls(doi=doi)
        yield _unpaywall_pdf_urls(doi)
        yield _s2_pdf_urls(doi)
        yield _crossref_pdf_urls(doi)
    if title:
        yield _openalex_work_urls(title=title)
    if doi:
        yield [f"https://doi.org/{doi}"]


def _download_with_curl(url: str, dest: Path) -> bool:
    """Publisher PDFs often need curl's TLS/redirect handling, not urllib."""
    curl = shutil.which("curl")
    if not curl:
        return False
    tmp = dest.with_suffix(".curl.tmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                curl, "-sS", "-L", "--max-time", str(PDF_TIMEOUT),
                "-A", PDF_UA, "-e", "https://www.google.com/",
                "-o", str(tmp), "-w", "%{http_code}", url,
            ],
            capture_output=True, text=True, timeout=PDF_TIMEOUT + 10,
        )
        if proc.returncode != 0 or not tmp.is_file():
            return False
        data = tmp.read_bytes()
        if not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
            return False
        shutil.move(str(tmp), str(dest))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _download_pdf(record: Mapping[str, Any], dest: Path) -> Path:
    local = record.get("pdf_path")
    if local:
        source = _resolve_local(local)
        if not source.is_file():
            raise ExtractUnavailable(
                f"pdf_path not found: {source}", reason="no_pdf"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return dest
    errors: list[str] = []
    seen: set[str] = set()
    found_group = False
    for group in _pdf_url_groups(record):
        if not group:
            continue
        found_group = True
        for url in group:
            if url in seen:
                continue
            seen.add(url)
            if _download_with_curl(url, dest):
                return dest
            payload = None
            last_error = ""
            for attempt in range(2):
                try:
                    request = urllib.request.Request(
                        url,
                        headers={"User-Agent": PDF_UA,
                                 "Referer": "https://www.google.com/"},
                    )
                    with urllib.request.urlopen(request, timeout=PDF_TIMEOUT) as response:  # noqa: S310
                        payload = response.read(MAX_PDF_BYTES + 1)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    retryable = any(
                        marker in last_error
                        for marker in ("404", "429", "timed out", "Timeout", "Connection")
                    )
                    if attempt == 0 and retryable:
                        time.sleep(1)
                        continue
                    break
            if payload is None:
                errors.append(f"{url}: {last_error[:120]}")
                continue
            if len(payload) > MAX_PDF_BYTES:
                errors.append(f"{url}: exceeds 64 MiB")
                continue
            if not payload.startswith(b"%PDF"):
                errors.append(f"{url}: not a PDF")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payload)
            return dest
    if not found_group:
        raise ExtractUnavailable(
            f"paper {record.get('lkm_id')} has no PDF source", reason="no_pdf"
        )
    raise ExtractUnavailable(
        f"cannot download PDF for {record.get('lkm_id')}: " + "; ".join(errors[-3:]),
        reason="download_failed",
    )


def _extract_graph(record: Mapping[str, Any], dest: Path, run_dir: Path,
                   runtime: Runtime, pdf: Path | None = None
                   ) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Build an LKM graph for a paper with no native graph.

    ``bohr lkm extract`` takes a local PDF; the graph is requested with
    ``--format graph`` so its nodes/edges match ``bohr lkm graph``.
    """
    pid = _local_paper_id(record)
    extract_dir = run_dir / "01_retrieval" / "extract" / pid
    extract_dir.mkdir(parents=True, exist_ok=True)
    if pdf is None:
        pdf = _download_pdf(record, extract_dir / "source.pdf")
    elif not Path(pdf).is_file():
        raise ExtractUnavailable(f"PDF missing: {pdf}", reason="no_pdf")
    submit = run(
        bohr_command(
            runtime, "lkm", "extract", "submit", str(pdf),
            "--wait", "--interval", "5s", "--timeout", "30m",
        ),
        log=stage_log(run_dir, f"01_extract_submit_{pid}"),
        timeout=2400, check=False,
    )
    submit_payload = _json_or_none(submit.stdout)
    write_json(extract_dir / "submit.json", submit_payload or {
        "returncode": submit.returncode,
        "stdout": (submit.stdout or "")[-4000:],
        "stderr": (submit.stderr or "")[-4000:],
    })
    if submit.returncode != 0 or not isinstance(submit_payload, Mapping):
        raise ExtractUnavailable(
            f"LKM extract submit failed for {pid}: "
            f"{(submit.stderr or submit.stdout or '').strip()[-400:]}",
            reason="submit_failed",
        )
    if submit_payload.get("ok") is False:
        error = submit_payload.get("error") or {}
        raise ExtractUnavailable(
            f"LKM extract submit rejected for {pid}: {error.get('message')}",
            reason="submit_rejected",
        )
    data = submit_payload.get("data") or {}
    task_id = data.get("task_id") or data.get("id")
    status = str(data.get("status") or "")
    cache_hit = bool(data.get("cache_hit"))
    if not task_id:
        raise ExtractUnavailable(
            f"LKM extract returned no task id for {pid}", reason="submit_failed"
        )
    if status and status not in {"succeeded", "success", "completed", "cached"}:
        raise ExtractUnavailable(
            f"LKM extract ended with status {status} for {pid}",
            reason="extract_partial",
        )

    result_payload = None
    result_proc = None
    for attempt in range(3):
        result_proc = run(
            bohr_command(
                runtime, "lkm", "extract", "result", str(task_id),
                "--format", "graph",
            ),
            log=stage_log(run_dir, f"01_extract_result_{pid}"),
            timeout=600, check=False,
        )
        result_payload = _json_or_none(result_proc.stdout)
        if (result_proc.returncode == 0
                and isinstance(result_payload, Mapping)
                and result_payload.get("ok") is not False):
            break
        message = str(((result_payload or {}).get("error") or {}).get("message") or "")
        if "NOT_READY" not in message and result_proc.returncode != 0:
            break
        if attempt < 2:
            time.sleep(5)
    write_json(extract_dir / "result.json", result_payload or {
        "returncode": result_proc.returncode if result_proc else None,
        "stdout": (result_proc.stdout if result_proc else "")[-4000:],
        "stderr": (result_proc.stderr if result_proc else "")[-4000:],
    })
    if (result_proc is None or result_proc.returncode != 0
            or not isinstance(result_payload, Mapping)
            or result_payload.get("ok") is False):
        error = (result_payload or {}).get("error") if isinstance(result_payload, Mapping) else {}
        raise ExtractUnavailable(
            f"LKM extract result failed for {pid}: {error or 'no JSON'}",
            reason="result_failed",
        )
    data = result_payload.get("data") or {}
    graph = data.get("graph") if isinstance(data.get("graph"), Mapping) else {}
    nodes = graph.get("nodes") or data.get("nodes") or []
    edges = graph.get("edges") or data.get("edges") or []
    if not nodes:
        raise ExtractUnavailable(
            f"LKM extract returned no graph nodes for {pid}",
            reason="empty_graph",
        )
    item = {
        "graph": {"nodes": nodes, "edges": edges},
        "paper": {
            "id": pid,
            "en_title": record.get("title") or pid,
            "doi": record.get("doi") or "",
            "authors": record.get("authors") or "",
            "en_abstract": record.get("abstract") or "",
        },
        "addressed_problems": data.get("addressed_problems") or [],
        "open_questions": data.get("open_questions") or [],
    }
    envelope = {"code": 0, "data": {"papers": [item]}}
    write_json(dest, envelope)
    return dest, envelope, {
        "graph_source": "lkm_extract",
        "extract_task_id": str(task_id),
        "extract_status": status or "succeeded",
        "extract_cache_hit": cache_hit,
    }


EXTRACTION_WORKERS = 3


def _probe_priority(candidate: Mapping[str, Any]) -> tuple[float, float]:
    """Relevance first, with a small independent-agreement tie-breaker."""
    corroboration = min(int(candidate.get("corroboration") or 0), 2)
    return (
        _float(candidate.get("relevance")) + 0.1 * corroboration,
        _candidate_rank(candidate),
    )


def probe_and_select(
    candidates: list[dict[str, Any]],
    run_dir: Path,
    runtime: Runtime,
    *,
    top_k: int,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    min_papers: int = DEFAULT_MIN_PAPERS,
    min_graph_nodes: int = DEFAULT_MIN_GRAPH_NODES,
    min_relevance: float = DEFAULT_MIN_RELEVANCE,
    min_corroboration: int = 0,
    use_lkm_extract: bool = True,
    max_extractions: int = DEFAULT_MAX_EXTRACTIONS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch native graphs, build missing ones in parallel, then select."""
    probe_root = run_dir / "01_retrieval" / "probe"
    probe_root.mkdir(parents=True, exist_ok=True)
    first_limit = max(int(candidate_limit), int(top_k), int(min_papers))
    available: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    extraction_attempts = 0
    extracted = 0
    external_candidates = 0
    audit: dict[str, Any] = {"probed": 0, "candidates": [], "selected": []}

    # Probe topical candidates first.  A broad paper can hit several queries
    # without being about the requested topic; relevance decides probe order.
    probe_order = sorted(
        candidates,
        key=_probe_priority,
        reverse=True,
    )
    # Probe the first batch for comparison; if it does not yield enough
    # graph-backed papers, extend the probe rather than failing the run.
    max_probe = min(len(probe_order), max(first_limit * 2, first_limit + 8))
    audit["first_pass"] = min(first_limit, max_probe)
    audit["max_probe"] = max_probe
    for candidate in probe_order[:max_probe]:
        record = candidate.get("record") or {}
        if record.get("source") == "external":
            # Independent sources only provide metadata: there is no LKM graph
            # to probe, so the paper goes straight to the PDF/extract path.
            candidate = dict(candidate)
            candidate["record"] = record
            candidate["graph_source"] = "external"
            candidate["skip_reason"] = "external"
            external_candidates += 1
            if use_lkm_extract and _can_extract(record):
                pending.append(candidate)
            elif use_lkm_extract:
                candidate["extract_skip_reason"] = "no_pdf"
            all_candidates.append(candidate)
            continue
        pid = record.get("lkm_id") or _local_paper_id(record)
        if not pid:
            continue
        candidate = dict(candidate)
        candidate["record"] = record
        dest = probe_root / f"{pid}.json"
        audit["probed"] += 1
        try:
            _, graph = _fetch_graph(record, dest, run_dir, runtime)
            stats = _graph_stats(graph)
            candidate.update(stats)
            candidate["probe_path"] = str(dest.relative_to(run_dir))
            candidate["graph_source"] = "native"
            if stats["node_count"] < int(min_graph_nodes):
                candidate["skip_reason"] = "thin_graph"
            elif stats["conclusion_count"] < 1:
                candidate["skip_reason"] = "no_conclusions"
            else:
                candidate["skip_reason"] = ""
                available.append(candidate)
        except GraphUnavailable as exc:
            candidate["skip_reason"] = exc.reason
            candidate["skip_detail"] = _one_line(exc)
            if use_lkm_extract and _can_extract(record):
                pending.append(candidate)
            elif use_lkm_extract:
                candidate["extract_skip_reason"] = "no_pdf"
        all_candidates.append(candidate)
        if audit["probed"] >= first_limit and len(available) >= int(min_papers):
            # Native graphs are enough only when they already cover top-k.
            # Otherwise keep probing because empty graphs may be buildable.
            if not use_lkm_extract or len(available) >= int(top_k):
                break

    # Download PDFs before spending an extraction call.  A closed-access
    # paper frequently has a DOI but no usable PDF; do not let it consume the
    # extraction budget.
    ready: list[tuple[dict[str, Any], Path, str]] = []
    pending.sort(
        key=_probe_priority,
        reverse=True,
    )
    for candidate in pending:
        if extraction_attempts >= int(max_extractions):
            candidate["extract_skip_reason"] = "budget"
            continue
        record = candidate.get("record") or {}
        pid = record.get("lkm_id") or _local_paper_id(record)
        extract_dir = run_dir / "01_retrieval" / "extract" / str(pid)
        try:
            pdf = _download_pdf(record, extract_dir / "source.pdf")
        except ExtractUnavailable as exc:
            candidate["extract_skip_reason"] = exc.reason
            candidate["skip_detail"] = _one_line(exc)
            print(
                f"[retrieve] no usable PDF for {pid}: {_one_line(exc)[:160]}",
                flush=True,
            )
            continue
        extraction_attempts += 1
        candidate["extract_attempted"] = True
        ready.append((candidate, pdf, pid))

    if ready:
        print(
            f"[retrieve] building {len(ready)} missing graph(s) with "
            f"bohr lkm extract ({min(EXTRACTION_WORKERS, len(ready))} workers)",
            flush=True,
        )
        with ThreadPoolExecutor(
            max_workers=min(EXTRACTION_WORKERS, len(ready))
        ) as pool:
            futures = {}
            for candidate, pdf, pid in ready:
                futures[pool.submit(
                    _extract_graph,
                    candidate.get("record") or {},
                    probe_root / f"{pid}.json",
                    run_dir,
                    runtime,
                    pdf,
                )] = candidate
            for future in as_completed(futures):
                candidate = futures[future]
                record = candidate.get("record") or {}
                pid = str(record.get("lkm_id") or "")
                try:
                    _, graph, extract_meta = future.result()
                    extracted += 1
                    candidate.update(extract_meta)
                    stats = _graph_stats(graph)
                    candidate.update(stats)
                    candidate["probe_path"] = str(
                        (probe_root / f"{pid}.json").relative_to(run_dir)
                    )
                    if stats["node_count"] < int(min_graph_nodes):
                        candidate["skip_reason"] = "thin_graph"
                        candidate["skip_detail"] = "extracted graph is too thin"
                    elif stats["conclusion_count"] < 1:
                        candidate["skip_reason"] = "no_conclusions"
                        candidate["skip_detail"] = "extracted graph has no conclusions"
                    else:
                        candidate["skip_reason"] = ""
                        available.append(candidate)
                except ExtractUnavailable as exc:
                    candidate["extract_skip_reason"] = exc.reason
                    candidate["skip_detail"] = _one_line(exc)
                    print(
                        f"[retrieve] LKM extraction skipped for {pid}: "
                        f"{_one_line(exc)[:180]}", flush=True,
                    )
                except AriadneError as exc:
                    candidate["extract_skip_reason"] = "extract_failed"
                    candidate["skip_detail"] = _one_line(exc)
                    print(
                        f"[retrieve] LKM extraction failed for {pid}: "
                        f"{_one_line(exc)[:180]}", flush=True,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one paper
                    candidate["extract_skip_reason"] = "extract_failed"
                    candidate["skip_detail"] = _one_line(exc)
                    print(
                        f"[retrieve] LKM extraction crashed for {pid}: "
                        f"{_one_line(exc)[:180]}", flush=True,
                    )

    audit["candidates"] = [_candidate_audit(item) for item in all_candidates]
    available.sort(key=_candidate_rank, reverse=True)
    for candidate in available:
        candidate["rank_score"] = round(_candidate_rank(candidate), 4)
    corroboration_floor = max(0, int(min_corroboration))
    preferred = [
        candidate for candidate in available
        if _float(candidate.get("relevance")) >= float(min_relevance)
        and int(candidate.get("corroboration") or 0) >= corroboration_floor
    ]
    relevant = [
        candidate for candidate in available
        if _float(candidate.get("relevance")) >= float(min_relevance)
    ]
    if len(preferred) >= int(min_papers):
        pool, pool_tier = preferred, 1
    elif len(relevant) >= int(min_papers):
        # Cross-check APIs are best effort: when they are all rate-limited,
        # fall back to relevance instead of failing the run.
        pool, pool_tier = relevant, 2
    else:
        pool, pool_tier = available, 3
    selected = list(pool[:int(top_k)])
    fill_floor = max(0.05, float(min_relevance) * 0.5)
    filled = 0
    if len(selected) < int(top_k):
        # Fill to top-k, but never with clearly off-topic papers.
        chosen = {_local_paper_id(candidate.get("record") or {})
                  for candidate in selected}
        for candidate in available:
            pid = _local_paper_id(candidate.get("record") or {})
            if pid in chosen:
                continue
            if _float(candidate.get("relevance")) < fill_floor:
                continue
            selected.append(candidate)
            chosen.add(pid)
            filled += 1
            if len(selected) >= int(top_k):
                break
    audit["selected"] = [
        _local_paper_id(candidate.get("record") or {}) for candidate in selected
    ]
    audit["available"] = len(available)
    audit["preferred"] = len(preferred)
    audit["relevant"] = len(relevant)
    audit["pool_tier"] = pool_tier
    audit["corroboration_floor"] = corroboration_floor
    audit["external_candidates"] = external_candidates
    audit["min_relevance"] = float(min_relevance)
    audit["fill_floor"] = fill_floor
    audit["filled"] = filled
    audit["extract_attempts"] = extraction_attempts
    audit["extracted"] = extracted
    audit["extraction_workers"] = EXTRACTION_WORKERS
    selection_path = run_dir / "01_retrieval" / "selection.json"
    write_json(selection_path, audit)
    if len(selected) < int(min_papers):
        raise AriadneError(
            f"Only {len(selected)} usable graph-backed paper(s) found; "
            f"need at least {min_papers}. Selection audit: {selection_path}"
        )
    return selected, audit


# --------------------------------------------------------------------------- #
# materialisation
# --------------------------------------------------------------------------- #

def _fallback_text(graph: Mapping[str, Any], paper: Mapping[str, Any]) -> str:
    items = (graph.get("data") or {}).get("papers") or []
    item = items[0] if items else {}
    meta = item.get("paper") or {}
    title = (
        meta.get("en_title") or meta.get("zh_title")
        or paper.get("title") or paper.get("lkm_id", "paper")
    )
    abstract = meta.get("en_abstract") or paper.get("abstract") or ""
    nodes = ((item.get("graph") or {}).get("nodes") or [])
    conclusions = [
        str(node.get("content") or "").strip()
        for node in nodes
        if str(node.get("kind") or "").lower() == "conclusion" and node.get("content")
    ]
    parts = [f"# {title}", "", "## Abstract", "", abstract, ""]
    if conclusions:
        parts += ["## Conclusions", ""]
        for text in conclusions:
            parts += [text, ""]
    return "\n".join(parts).strip() + "\n"


def _fetch_text(paper: Mapping[str, Any], graph: Mapping[str, Any], dest: Path,
                run_dir: Path, runtime: Runtime, fulltext: bool) -> Path:
    local = paper.get("ocr_path")
    if local:
        source = _resolve_local(local)
        if not source.is_file():
            raise AriadneError(f"ocr_path not found: {source}")
        shutil.copyfile(source, dest)
        return dest
    if not fulltext:
        dest.write_text(_fallback_text(graph, paper), encoding="utf-8")
        return dest
    arxiv = paper.get("arxiv")
    if arxiv and runtime.bohr_bin:
        try:
            pdf = dest.parent / "source.pdf"
            url = f"https://arxiv.org/pdf/{arxiv}"
            request = urllib.request.Request(url, headers={"User-Agent": "ariadne/0.3"})
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                payload = response.read()
            if not payload.startswith(b"%PDF"):
                raise ValueError("not a PDF")
            pdf.write_bytes(payload)
            parsed = run(
                bohr_command(runtime, "pdf", "parse", "--file", str(pdf), "--sync"),
                log=stage_log(run_dir, f"01_pdf_parse_{paper['lkm_id']}"),
                timeout=PDF_TIMEOUT, check=False,
            )
            if parsed.returncode != 0:
                raise ValueError((parsed.stderr or parsed.stdout or "").strip()[-300:])
            token = (json.loads(parsed.stdout).get("data") or {}).get("token")
            if not token:
                raise ValueError("no parse token")
            result = run(
                bohr_command(runtime, "pdf", "result", token),
                log=stage_log(run_dir, f"01_pdf_result_{paper['lkm_id']}"),
                timeout=BOHR_TIMEOUT, check=False,
            )
            if result.returncode != 0:
                raise ValueError((result.stderr or result.stdout or "").strip()[-300:])
            content = (json.loads(result.stdout).get("data") or {}).get("content") or ""
            if content.strip():
                dest.write_text(content, encoding="utf-8")
                return dest
            raise ValueError("empty PDF text")
        except Exception as exc:  # noqa: BLE001 - graph-derived text is a valid fallback
            print(
                f"[retrieve] {paper.get('lkm_id')}: full text unavailable "
                f"({str(exc)[:120]}); using graph-derived text",
                flush=True,
            )
    dest.write_text(_fallback_text(graph, paper), encoding="utf-8")
    return dest


def materialise(records: Iterable[Mapping[str, Any]], run_dir: Path,
                runtime: Runtime, *, fulltext: bool = True) -> list[dict[str, Any]]:
    """Materialise an explicit ``--papers`` list (no selection)."""
    papers_dir = run_dir / "01_retrieval" / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    materialised: list[dict[str, Any]] = []
    for record in records:
        paper = dict(record)
        pid = _local_paper_id(paper)
        if not pid:
            continue
        paper["lkm_id"] = pid
        out = papers_dir / pid
        out.mkdir(parents=True, exist_ok=True)
        graph_path, graph = _fetch_graph(paper, out / "graph.json", run_dir, runtime)
        text_path = _fetch_text(
            paper, graph, out / "ocr.md", run_dir, runtime, fulltext
        )
        paper.update({
            "lkm_id": pid,
            "graph_path": str(graph_path.relative_to(run_dir)),
            "ocr_path": str(text_path.relative_to(run_dir)),
        })
        # A graph title is more reliable than an empty search title.
        items = (graph.get("data") or {}).get("papers") or []
        meta = (items[0].get("paper") or {}) if items else {}
        if meta.get("en_title"):
            paper["title"] = meta["en_title"]
        materialised.append(paper)
    if not materialised:
        raise AriadneError("no papers were materialised")
    write_json(
        run_dir / "01_retrieval" / "papers.json",
        {"papers": materialised, "fulltext": fulltext, "mode": "spec"},
    )
    return materialised


def materialise_selected(selected: list[dict[str, Any]], run_dir: Path,
                         runtime: Runtime, *, fulltext: bool = True
                         ) -> list[dict[str, Any]]:
    """Copy selected probe graphs into the per-paper layout paper2ir expects."""
    papers_dir = run_dir / "01_retrieval" / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    materialised: list[dict[str, Any]] = []
    for candidate in selected:
        record = dict(candidate.get("record") or {})
        pid = record.get("lkm_id") or _local_paper_id(record)
        if not pid:
            continue
        record["lkm_id"] = pid
        out = papers_dir / pid
        out.mkdir(parents=True, exist_ok=True)
        probe_path = run_dir / str(candidate.get("probe_path") or "")
        if not probe_path.is_file():
            raise AriadneError(f"selected graph for {pid} is missing: {probe_path}")
        shutil.copyfile(probe_path, out / "graph.json")
        graph = read_json(out / "graph.json")
        text_path = _fetch_text(
            record, graph, out / "ocr.md", run_dir, runtime, fulltext
        )
        record.update({
            "graph_path": str((out / "graph.json").relative_to(run_dir)),
            "ocr_path": str(text_path.relative_to(run_dir)),
            "graph_source": candidate.get("graph_source") or "native",
            "selection": {
                "rank_score": candidate.get("rank_score"),
                "query_positions": list(candidate.get("query_positions") or []),
                "node_count": candidate.get("node_count"),
                "conclusion_count": candidate.get("conclusion_count"),
                "graph_source": candidate.get("graph_source") or "native",
                "extract_task_id": candidate.get("extract_task_id") or "",
                "extract_status": candidate.get("extract_status") or "",
                "cross_check": dict(candidate.get("cross_check") or {}),
                "discovered_by": list(candidate.get("discovered_by") or []),
            },
        })
        items = (graph.get("data") or {}).get("papers") or []
        meta = (items[0].get("paper") or {}) if items else {}
        if meta.get("en_title"):
            record["title"] = meta["en_title"]
        materialised.append(record)
    if not materialised:
        raise AriadneError("no selected papers were materialised")
    # Keep the probe directory: selection.json points into it, and rejected
    # graphs are useful when auditing why Ariadne skipped a paper.
    return materialised


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #

def validate_documents(paths: Iterable[Path]) -> list[Path]:
    """Validate every selected file before uploading any of them."""
    selected = []
    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in {".pdf", ".md", ".markdown"}:
            raise AriadneError(f"document must be a readable PDF or Markdown file: {path}")
        if not path.stat().st_size or path.stat().st_size > 64 * 1024 * 1024:
            raise AriadneError(f"document must be nonempty and at most 64 MiB: {path}")
        try:
            if path.suffix.lower() == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(path)
                if reader.is_encrypted or not 0 < len(reader.pages) <= 50:
                    raise AriadneError(f"LKM needs an unencrypted PDF of at most 50 pages: {path}")
            else:
                path.read_text(encoding="utf-8")
                import reportlab  # noqa: F401 - fail before upload if converter is absent
        except ImportError as exc:
            raise AriadneError("Document import needs `pip install -r ariadne/requirements.txt`.") from exc
        except AriadneError:
            raise
        except Exception as exc:
            raise AriadneError(f"cannot read document {path}: {exc}") from exc
        if path not in selected:
            selected.append(path)
    return selected


def markdown_pdf(source: Path, destination: Path) -> Path:
    """A text-preserving PDF transport for LKM, never a rendered HTML fetch.

    Keep Markdown/math source verbatim and use a CJK font for Chinese text.
    The original Markdown, rather than this transport, feeds paper2ir.
    """
    from itertools import groupby
    from xml.sax.saxutils import escape
    import reportlab
    from pypdf import PdfReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.pagesizes import A4
    font = "STSong-Light"
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    latin = "AriadneVera"
    if latin not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(latin, str(Path(reportlab.__file__).parent / "fonts" / "Vera.ttf")))
    latin_glyphs = pdfmetrics.getFont(latin).face.charToGlyph

    def font_for(char: str) -> str:
        if char.isspace() or ord(char) in latin_glyphs:
            return latin
        try:
            char.encode("gb2312")
        except UnicodeEncodeError as exc:
            raise AriadneError(
                f"Markdown contains an unsupported PDF character U+{ord(char):04X}: {source}"
            ) from exc
        return font

    style = ParagraphStyle("source", fontName=latin, fontSize=10, leading=15,
                           wordWrap="CJK", splitLongWords=True)
    text = source.read_text(encoding="utf-8")
    for char in set(text):
        font_for(char)  # Reject unsupported glyphs before creating a transport.
    story = []
    for line in text.splitlines():
        if line.strip():
            # Chunk very long lines to keep ReportLab paragraphs splittable.
            for start in range(0, len(line), 2000):
                # Embed Latin/scientific glyphs; the CID font alone silently
                # changes characters such as Å, ² and ³ in PDF text extraction.
                markup = "".join(
                    f'<font name="{name}">{escape("".join(chars))}</font>'
                    for name, chars in groupby(line[start:start + 2000], key=font_for)
                )
                story.append(Paragraph(markup, style))
        else:
            story.append(Spacer(1, 7))
    if not any(line.strip() for line in text.splitlines()):
        raise AriadneError(f"Markdown document has no text: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(destination), pagesize=A4, leftMargin=40, rightMargin=40,
                      topMargin=40, bottomMargin=40).build(story)
    validate_documents([destination])
    extracted = "".join(page.extract_text() or "" for page in PdfReader(destination).pages)
    if re.sub(r"\s+", "", extracted) != re.sub(r"\s+", "", text):
        destination.unlink(missing_ok=True)
        raise AriadneError(f"Markdown PDF conversion changed text; document was not uploaded: {source}")
    return destination


def import_documents(paths: Iterable[Path], run_dir: Path, runtime: Runtime,
                     *, cached: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """Upload only explicitly selected files using the existing LKM extractor."""
    selected = validate_documents(paths)
    result = []
    by_id = {str(item["lkm_id"]): item for item in cached}
    # Convert/check all transports first so a bad later document cannot cause
    # earlier documents to be uploaded in a partially invalid batch.
    prepared = []
    for source in selected:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        pid = "local_" + digest[:24]
        out = run_dir / "01_retrieval" / "papers" / pid
        previous = by_id.get(pid)
        if previous and Path(str(previous.get("graph_path", ""))).is_file():
            result.append(dict(previous))
            continue
        out.mkdir(parents=True, exist_ok=True)
        transport = out / "source.pdf"
        snapshot = transport if source.suffix.lower() == ".pdf" else out / "source.md"
        shutil.copyfile(source, snapshot)
        if hashlib.sha256(snapshot.read_bytes()).hexdigest() != digest:
            raise AriadneError(f"selected document changed while being prepared: {source}; retry")
        if source.suffix.lower() != ".pdf":
            markdown_pdf(snapshot, transport)
        prepared.append((source, snapshot, transport, out, pid, digest))
    for source, snapshot, transport, out, pid, source_digest in prepared:
        record = {"lkm_id": pid, "title": source.stem, "pdf_path": str(transport),
                  "source": "document", "source_path": str(source),
                  "document_sha256": source_digest}
        path, graph, metadata = _extract_graph(record, out / "graph.json", run_dir,
                                               runtime, pdf=transport)
        if source.suffix.lower() != ".pdf":
            (out / "ocr.md").write_text(snapshot.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            from pypdf import PdfReader
            text = "\n\n".join(page.extract_text() or "" for page in PdfReader(transport).pages)
            if text.strip():
                (out / "ocr.md").write_text(text, encoding="utf-8")
            else:
                _fetch_text(record, graph, out / "ocr.md", run_dir, runtime, fulltext=False)
        record.update(metadata)
        record.update(graph_path=str(path), ocr_path=str(out / "ocr.md"))
        result.append(record)
    return result


def retrieve(
    question: str,
    run_dir: Path,
    *,
    top_k: int = 8,
    paper_spec: Path | None = None,
    runtime: Runtime | None = None,
    fulltext: bool = True,
    question_en: str = "",
    domain: str = "",
    max_queries: int = DEFAULT_MAX_QUERIES,
    per_query: int = DEFAULT_PER_QUERY,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    min_papers: int = DEFAULT_MIN_PAPERS,
    min_graph_nodes: int = DEFAULT_MIN_GRAPH_NODES,
    min_relevance: float = DEFAULT_MIN_RELEVANCE,
    use_query_planner: bool = True,
    use_lkm_extract: bool = True,
    max_extractions: int = DEFAULT_MAX_EXTRACTIONS,
    cross_check: bool = DEFAULT_CROSS_CHECK,
    cross_check_sources: str = DEFAULT_CROSS_SOURCES,
    min_corroboration: int = DEFAULT_MIN_CORROBORATION,
    external_discovery: bool = False,
    external_candidates: int = 4,
) -> list[dict[str, Any]]:
    runtime = _runtime_or_resolve(runtime)
    if paper_spec is not None:
        records = read_spec(paper_spec)
        return materialise(records, run_dir, runtime, fulltext=fulltext)

    queries, planner = plan_queries(
        question,
        question_en=question_en,
        domain=domain,
        max_queries=max_queries,
        use_query_planner=use_query_planner,
    )
    write_json(
        run_dir / "01_retrieval" / "queries.json",
        {"question": question, "question_en": question_en, "domain": domain,
         "planner": planner, "queries": queries},
    )
    print(
        f"[retrieve] {len(queries)} queries ({planner}): "
        + " | ".join(item["query"] for item in queries),
        flush=True,
    )

    cross_audit: dict[str, Any] = {"enabled": False}
    enrich = None
    if cross_check:
        def enrich(candidates: dict[str, dict[str, Any]]) -> None:
            started = time.monotonic()
            print(
                "[retrieve] cross-checking against independent sources: "
                f"{cross_check_sources}",
                flush=True,
            )
            try:
                cross_audit.update(cross_check_candidates(
                    candidates, queries, run_dir,
                    source_names=cross_check_sources,
                    per_query=per_query,
                    min_corroboration=min_corroboration,
                    discover=external_discovery,
                    discovery_limit=external_candidates,
                ))
            except Exception as exc:  # noqa: BLE001 - never block retrieval
                cross_audit.update({
                    "enabled": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
                print(
                    "[retrieve] independent-source cross-check unavailable "
                    f"({str(exc)[:160]}); continuing with LKM only",
                    flush=True,
                )
                return
            cross_audit["seconds"] = round(time.monotonic() - started, 1)
            if cross_audit.get("enabled"):
                entries = cross_audit.get("candidates") or []
                confirmed = sum(
                    1 for entry in entries if entry.get("corroboration")
                )
                print(
                    f"[retrieve] cross-check: {confirmed}/{len(entries)} "
                    "candidates confirmed by independent sources"
                    + (
                        f", +{cross_audit['discovery']['added']} discovered"
                        if (cross_audit.get("discovery") or {}).get("added")
                        else ""
                    ),
                    flush=True,
                )
                failures = [
                    source for source in cross_audit.get("sources") or []
                    if source.get("error")
                ]
                if failures:
                    print(
                        "[retrieve] cross-check source(s) unavailable: "
                        + "; ".join(
                            f"{item['name']} ({str(item['error'])[:70]})"
                            for item in failures
                        ),
                        flush=True,
                    )

    candidates, search_audit = search_many(
        queries, per_query, run_dir, runtime, enrich=enrich
    )
    selected, selection_audit = probe_and_select(
        candidates, run_dir, runtime,
        top_k=top_k,
        candidate_limit=candidate_limit,
        min_papers=min_papers,
        min_graph_nodes=min_graph_nodes,
        min_relevance=min_relevance,
        min_corroboration=(
            int(min_corroboration) if cross_audit.get("enabled") else 0
        ),
        use_lkm_extract=use_lkm_extract,
        max_extractions=max_extractions,
    )
    selection = {
        "question": question,
        "question_en": question_en,
        "domain": domain,
        "planner": planner,
        "queries": queries,
        "search": search_audit,
        "cross_check": cross_audit,
        "cross_check_sources": cross_check_sources,
        "min_corroboration": int(min_corroboration),
        "external_discovery": bool(external_discovery),
        "external_candidates": int(external_candidates),
        "candidates": selection_audit.get("candidates") or [],
        "selected": selection_audit.get("selected") or [],
        "available": selection_audit.get("available", 0),
        "preferred": selection_audit.get("preferred", 0),
        "relevant": selection_audit.get("relevant", 0),
        "pool_tier": selection_audit.get("pool_tier", 0),
        "corroboration_floor": selection_audit.get("corroboration_floor", 0),
        "external_candidate_count": selection_audit.get("external_candidates", 0),
        "filled": selection_audit.get("filled", 0),
        "fill_floor": selection_audit.get("fill_floor", 0.0),
        "min_relevance": selection_audit.get("min_relevance", min_relevance),
        "probed": selection_audit.get("probed", 0),
        "first_pass": selection_audit.get("first_pass"),
        "max_probe": selection_audit.get("max_probe"),
        "extract_attempts": selection_audit.get("extract_attempts", 0),
        "extracted": selection_audit.get("extracted", 0),
        "extraction_workers": selection_audit.get("extraction_workers", 0),
        "use_lkm_extract": use_lkm_extract,
        "max_extractions": max_extractions,
        "top_k": int(top_k),
        "min_papers": int(min_papers),
    }
    write_json(run_dir / "01_retrieval" / "selection.json", selection)
    papers = materialise_selected(selected, run_dir, runtime, fulltext=fulltext)
    write_json(
        run_dir / "01_retrieval" / "papers.json",
        {
            "papers": papers,
            "fulltext": fulltext,
            "mode": "auto",
            "selection": str(run_dir / "01_retrieval" / "selection.json"),
            "top_k": int(top_k),
            "queries": queries,
            "planner": planner,
            "cross_check": str(run_dir / "01_retrieval" / "sources.json")
            if cross_audit.get("enabled") else "",
        },
    )
    return papers
