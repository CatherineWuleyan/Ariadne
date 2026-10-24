"""Independent bibliographic sources used to cross-check LKM retrieval.

LKM stays the primary index: it is the only source that returns the paper
graph every downstream stage needs.  OpenAlex, Crossref, arXiv and Semantic
Scholar are queried in parallel so that a candidate can be confirmed by an
index that did not produce it:

* **verification** - a paper is "corroborated" when at least one independent
  source resolves the same DOI, arXiv id or near-identical title;
* **recall** (opt-in) - a paper missed by LKM but returned by several
  independent sources can enter the candidate pool.

Everything here is best effort.  Rate limits, timeouts and malformed payloads
degrade to "no evidence from this source" and never fail a run.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from html import unescape
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import __version__
from .util import write_json

HTTP_TIMEOUT = 10
MAX_RETRIES = 1
RETRY_DELAY = 1.5
MAX_BYTES = 4_000_000
MAILTO = "ariadne@example.org"
USER_AGENT = (
    f"ariadne/{__version__} (literature cross-check; mailto:{MAILTO})"
)
DEFAULT_SOURCE_NAMES = (
    "openalex", "crossref", "arxiv", "semantic_scholar",
)
DEFAULT_SOURCE_LIST = ",".join(DEFAULT_SOURCE_NAMES)
DEFAULT_CROSS_QUERIES = 2

_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_ARXIV_PREFIX = re.compile(r"^(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/)", re.I)
_ARXIV_URL = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", re.I
)
_ARXIV_DOI = re.compile(r"10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5})", re.I)
_MATCH_RANK = {"doi": 3, "arxiv": 2, "title": 1}
_TITLE_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "and", "or", "in", "on", "to", "with",
    "from", "by", "at", "as", "is", "are", "via", "using", "towards", "toward",
})


# --------------------------------------------------------------------------- #
# normalisation and matching
# --------------------------------------------------------------------------- #

def normalise_doi(value: Any) -> str:
    """Return a bare lowercase DOI (``10.1234/abc``), or ``""``."""
    text = str(value or "").strip()
    text = _DOI_PREFIX.sub("", text)
    return text.strip().rstrip(".,;").lower()


def normalise_arxiv(value: Any) -> str:
    """Return a bare arXiv id without the ``arXiv:`` prefix or version."""
    text = str(value or "").strip()
    text = _ARXIV_PREFIX.sub("", text)
    text = re.sub(r"\.pdf$", "", text, flags=re.I)
    text = re.sub(r"v\d+$", "", text, flags=re.I)
    match = _ARXIV_URL.search(text)
    return (match.group(1) if match else text).strip()


def arxiv_from_urls(*values: Any) -> str:
    for value in values:
        match = _ARXIV_URL.search(str(value or ""))
        if match:
            return match.group(1)
    return ""


def title_tokens(value: Any) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", _clean_title(value).lower())
        if token not in _TITLE_STOPWORDS
    }


def _clean_title(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return unescape(text)


def _compact_title(value: Any) -> str:
    text = _clean_title(value).lower()
    return " ".join(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text))


def same_title(left: Any, right: Any) -> bool:
    """Tolerant title equality: punctuation, case and markup do not matter.

    Token Jaccard >= 0.9 catches "Aircraft Design: A Survey" vs "Aircraft
    Design - A Survey"; very short titles need an exact compact match so
    generic words cannot create false corroboration.
    """
    first, second = title_tokens(left), title_tokens(right)
    if not first or not second:
        compact = _compact_title(left)
        return bool(compact) and compact == _compact_title(right)
    if first == second:
        return True
    if min(len(first), len(second)) < 3:
        return False
    return len(first & second) / len(first | second) >= 0.9


def _first(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value or "")


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _identity(record: "SourceRecord") -> str:
    if record.doi:
        return "doi:" + normalise_doi(record.doi)
    if record.arxiv:
        return "arxiv:" + normalise_arxiv(record.arxiv)
    return "title:" + _compact_title(record.title)


# --------------------------------------------------------------------------- #
# records and index
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SourceRecord:
    """One paper as described by one independent index."""

    source: str
    title: str
    doi: str = ""
    arxiv: str = ""
    year: int | None = None
    venue: str = ""
    url: str = ""
    authors: str = ""
    abstract: str = ""
    pdf_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceIndex:
    """All records returned by the independent sources for one question."""

    records: list[SourceRecord] = field(default_factory=list)

    def extend(self, records: Sequence[SourceRecord]) -> None:
        self.records.extend(records)

    def match(self, *, doi: Any = "", title: Any = "", arxiv: Any = ""
              ) -> dict[str, str]:
        """Map independent source name -> strongest matching reason."""
        wanted_doi = normalise_doi(doi)
        wanted_arxiv = normalise_arxiv(arxiv)
        found: dict[str, tuple[int, str]] = {}
        for record in self.records:
            record_doi = normalise_doi(record.doi)
            if wanted_doi and record_doi and record_doi == wanted_doi:
                reason = "doi"
            elif (wanted_arxiv
                  and normalise_arxiv(record.arxiv) == wanted_arxiv):
                reason = "arxiv"
            elif title and record.title and same_title(title, record.title):
                reason = "title"
            else:
                continue
            rank = _MATCH_RANK[reason]
            current = found.get(record.source)
            if current is None or rank > current[0]:
                found[record.source] = (rank, reason)
        return {name: reason for name, (_, reason) in found.items()}

    def sources_for(self, *, doi: Any = "", title: Any = "",
                    arxiv: Any = "") -> list[str]:
        return sorted(self.match(doi=doi, title=title, arxiv=arxiv))

    def discover(self, *, exclude_dois: Sequence[str] = (),
                 exclude_arxiv: Sequence[str] = (),
                 exclude_titles: Sequence[str] = (),
                 min_sources: int = 2, limit: int = 4
                 ) -> list[dict[str, Any]]:
        """Papers several independent sources agree on, minus known ones."""
        excluded_dois = {normalise_doi(value) for value in exclude_dois if value}
        excluded_arxiv = {
            normalise_arxiv(value) for value in exclude_arxiv if value
        }
        clusters: list[dict[str, SourceRecord]] = []
        for record in self.records:
            if not record.title:
                continue
            if normalise_doi(record.doi) in excluded_dois:
                continue
            if normalise_arxiv(record.arxiv) in excluded_arxiv:
                continue
            for cluster in clusters:
                if any(_same_paper(existing, record)
                       for existing in cluster.values()):
                    cluster.setdefault(record.source, record)
                    break
            else:
                clusters.append({record.source: record})

        minimum = max(2, int(min_sources))
        discovered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for by_source in clusters:
            if len(by_source) < minimum:
                continue
            records = list(by_source.values())
            if any(
                normalise_doi(record.doi) in excluded_dois
                or normalise_arxiv(record.arxiv) in excluded_arxiv
                for record in records
            ):
                continue
            if any(
                same_title(record.title, known)
                for record in records for known in exclude_titles if known
            ):
                continue
            merged = _merge_records(records)
            key = _identity(merged)
            if key in seen:
                continue
            seen.add(key)
            discovered.append({
                "record": merged,
                "sources": sorted(by_source),
            })
        discovered.sort(
            key=lambda item: (-len(item["sources"]),
                              bool(item["record"].doi),
                              bool(item["record"].pdf_url)),
        )
        return discovered[:max(0, int(limit))]


def _same_paper(left: SourceRecord, right: SourceRecord) -> bool:
    left_doi = normalise_doi(left.doi)
    right_doi = normalise_doi(right.doi)
    if left_doi and right_doi:
        return left_doi == right_doi
    left_arxiv = normalise_arxiv(left.arxiv)
    right_arxiv = normalise_arxiv(right.arxiv)
    if left_arxiv and right_arxiv:
        return left_arxiv == right_arxiv
    return same_title(left.title, right.title)


def _merge_records(records: Sequence[SourceRecord]) -> SourceRecord:
    """Collapse the same paper from several sources into the richest record."""
    base = max(
        records,
        key=lambda item: (bool(item.doi), bool(item.pdf_url),
                          bool(item.abstract), len(item.title)),
    )

    def pick(attribute: str) -> Any:
        for item in (base, *records):
            value = getattr(item, attribute)
            if value:
                return value
        return getattr(base, attribute)

    return SourceRecord(
        source="external",
        title=pick("title"),
        doi=normalise_doi(pick("doi")),
        arxiv=normalise_arxiv(pick("arxiv")),
        year=pick("year"),
        venue=pick("venue"),
        url=pick("url"),
        authors=pick("authors"),
        abstract=str(pick("abstract") or "")[:4000],
        pdf_url=pick("pdf_url"),
    )


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class SourceUnavailable(RuntimeError):
    """A source could not be reached or returned an unusable payload."""


def _get(url: str, *, timeout: int = HTTP_TIMEOUT,
         headers: Mapping[str, str] | None = None) -> bytes:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/atom+xml, */*",
    }
    if headers:
        request_headers.update(headers)
    last_error = "request failed"
    for attempt in range(MAX_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.read(MAX_BYTES)
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code in {429, 500, 502, 503, 504} and attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            break
    raise SourceUnavailable(f"{url.split('?', 1)[0]}: {last_error}")


def _json(url: str, *, headers: Mapping[str, str] | None = None) -> Any:
    body = _get(url, headers=headers)
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceUnavailable(f"invalid JSON from {url.split('?', 1)[0]}") from exc


# --------------------------------------------------------------------------- #
# OpenAlex
# --------------------------------------------------------------------------- #

def _openalex_abstract(inverted: Any) -> str:
    if not isinstance(inverted, Mapping):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        for index in indexes or []:
            try:
                positions.append((int(index), str(word)))
            except (TypeError, ValueError):
                continue
    positions.sort()
    return " ".join(word for _, word in positions)[:4000]


def _openalex_record(work: Any) -> SourceRecord | None:
    if not isinstance(work, Mapping):
        return None
    title = _text(work.get("title") or work.get("display_name"))
    if not title:
        return None
    locations = [work.get("best_oa_location"), work.get("primary_location")]
    locations.extend(work.get("locations") or [])
    urls = [
        str(location.get("landing_page_url") or "") + " " +
        str(location.get("pdf_url") or "")
        for location in locations if isinstance(location, Mapping)
    ]
    primary = work.get("primary_location")
    primary = primary if isinstance(primary, Mapping) else {}
    source_meta = primary.get("source")
    source_meta = source_meta if isinstance(source_meta, Mapping) else {}
    best = work.get("best_oa_location")
    best = best if isinstance(best, Mapping) else {}
    authors: list[str] = []
    for authorship in work.get("authorships") or []:
        if not isinstance(authorship, Mapping):
            continue
        person = authorship.get("author") or {}
        if isinstance(person, Mapping):
            authors.append(_text(person.get("display_name")))
    return SourceRecord(
        source="openalex",
        title=title,
        doi=normalise_doi(work.get("doi")),
        arxiv=arxiv_from_urls(*urls),
        year=work.get("publication_year"),
        venue=_text(source_meta.get("display_name")),
        url=_text(work.get("id")),
        authors=", ".join(name for name in authors if name)[:400],
        abstract=_openalex_abstract(work.get("abstract_inverted_index")),
        pdf_url=_text(best.get("pdf_url")),
    )


def search_openalex(query: str, limit: int) -> list[SourceRecord]:
    params = urllib.parse.urlencode({
        "search": query,
        "per-page": max(1, min(int(limit), 50)),
        "mailto": MAILTO,
    })
    payload = _json("https://api.openalex.org/works?" + params)
    results = payload.get("results") if isinstance(payload, Mapping) else None
    return [
        record for record in (_openalex_record(item) for item in results or [])
        if record is not None
    ]


def lookup_openalex(doi: str) -> SourceRecord | None:
    doi = normalise_doi(doi)
    if not doi:
        return None
    payload = _json(
        "https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="/")
        + "?mailto=" + urllib.parse.quote(MAILTO)
    )
    return _openalex_record(payload) if isinstance(payload, Mapping) else None


# --------------------------------------------------------------------------- #
# Crossref
# --------------------------------------------------------------------------- #

def _crossref_record(item: Any) -> SourceRecord | None:
    if not isinstance(item, Mapping):
        return None
    title = _text(_first(item.get("title")))
    if not title:
        return None
    issued = item.get("issued")
    parts = (
        issued.get("date-parts") if isinstance(issued, Mapping) else None
    ) or [[None]]
    year = None
    try:
        year = int(parts[0][0]) if parts and parts[0] else None
    except (TypeError, ValueError, IndexError):
        year = None
    authors = [
        _text(f"{author.get('given') or ''} {author.get('family') or ''}")
        for author in item.get("author") or []
        if isinstance(author, Mapping)
    ]
    links = [
        link for link in item.get("link") or [] if isinstance(link, Mapping)
    ]
    pdf_url = ""
    for link in links:
        url = str(link.get("URL") or "")
        content_type = str(link.get("content-type") or "").lower()
        if url and ("pdf" in content_type or url.lower().endswith(".pdf")):
            pdf_url = url
            break
    return SourceRecord(
        source="crossref",
        title=title,
        doi=normalise_doi(item.get("DOI")),
        arxiv=arxiv_from_urls(
            *(str(link.get("URL") or "") for link in links),
            item.get("URL"),
        ),
        year=year,
        venue=_text(_first(item.get("container-title"))),
        url=_text(item.get("URL")),
        authors=", ".join(name for name in authors if name)[:400],
        abstract=re.sub(r"<[^>]+>", " ", _text(item.get("abstract")))[:4000],
        pdf_url=pdf_url,
    )


def search_crossref(query: str, limit: int) -> list[SourceRecord]:
    params = urllib.parse.urlencode({
        "query.bibliographic": query,
        "rows": max(1, min(int(limit), 50)),
        "mailto": MAILTO,
    })
    payload = _json("https://api.crossref.org/works?" + params)
    message = payload.get("message") if isinstance(payload, Mapping) else None
    items = message.get("items") if isinstance(message, Mapping) else None
    return [
        record for record in (_crossref_record(item) for item in items or [])
        if record is not None
    ]


def lookup_crossref(doi: str) -> SourceRecord | None:
    doi = normalise_doi(doi)
    if not doi:
        return None
    payload = _json(
        "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/")
    )
    message = payload.get("message") if isinstance(payload, Mapping) else None
    return _crossref_record(message)


# --------------------------------------------------------------------------- #
# arXiv
# --------------------------------------------------------------------------- #

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"


def _arxiv_request_query(text: str) -> str:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+.-]{1,}", str(text or ""))[:8]
    if tokens:
        return " AND ".join(f"all:{token}" for token in tokens)
    phrase = str(text or "").strip().replace('"', " ")[:120]
    return f'all:"{phrase}"' if phrase else ""


def _arxiv_record(entry: Any) -> SourceRecord | None:
    title = _text(entry.findtext(f"{_ATOM}title", default=""))
    if not title:
        return None
    arxiv = normalise_arxiv(entry.findtext(f"{_ATOM}id", default=""))
    published = entry.findtext(f"{_ATOM}published", default="") or ""
    authors = [
        _text(author.findtext(f"{_ATOM}name", default=""))
        for author in entry.findall(f"{_ATOM}author")
    ]
    pdf_url = ""
    url = ""
    for link in entry.findall(f"{_ATOM}link"):
        if link.get("title") == "pdf":
            pdf_url = link.get("href") or ""
        elif link.get("rel") == "alternate":
            url = link.get("href") or ""
    return SourceRecord(
        source="arxiv",
        title=title,
        doi=normalise_doi(entry.findtext(f"{_ARXIV}doi", default="")),
        arxiv=arxiv,
        year=int(published[:4]) if published[:4].isdigit() else None,
        venue=_text(entry.findtext(f"{_ARXIV}journal_ref", default="")),
        url=url or (f"https://arxiv.org/abs/{arxiv}" if arxiv else ""),
        authors=", ".join(name for name in authors if name)[:400],
        abstract=_text(entry.findtext(f"{_ATOM}summary", default=""))[:4000],
        pdf_url=pdf_url or (
            f"https://arxiv.org/pdf/{arxiv}" if arxiv else ""
        ),
    )


def _arxiv_feed(url: str) -> list[SourceRecord]:
    body = _get(url)
    try:
        root = ET.fromstring(body)  # noqa: S314 - trusted publisher endpoint
    except ET.ParseError as exc:
        raise SourceUnavailable(
            f"invalid Atom from {url.split('?', 1)[0]}"
        ) from exc
    if root.tag == f"{_ATOM}entry":
        entries = [root]
    else:
        entries = root.findall(f"{_ATOM}entry")
    return [
        record for record in (_arxiv_record(entry) for entry in entries)
        if record is not None
    ]


def search_arxiv(query: str, limit: int) -> list[SourceRecord]:
    request = _arxiv_request_query(query)
    if not request:
        return []
    params = urllib.parse.urlencode({
        "search_query": request,
        "start": 0,
        "max_results": max(1, min(int(limit), 50)),
    })
    return _arxiv_feed("https://export.arxiv.org/api/query?" + params)


def lookup_arxiv(doi: str) -> SourceRecord | None:
    arxiv = arxiv_from_urls(doi)
    if not arxiv:
        match = _ARXIV_DOI.search(str(doi or ""))
        arxiv = match.group(1) if match else normalise_arxiv(doi)
    if not arxiv or not re.fullmatch(r"\d{4}\.\d{4,5}", arxiv):
        return None
    records = _arxiv_feed(
        "https://export.arxiv.org/api/query?"
        + urllib.parse.urlencode({"id_list": arxiv, "max_results": 1})
    )
    return records[0] if records else None


# --------------------------------------------------------------------------- #
# Semantic Scholar
# --------------------------------------------------------------------------- #

_S2_FIELDS = (
    "title,abstract,externalIds,year,venue,authors,openAccessPdf,url"
)


def _s2_headers() -> dict[str, str]:
    key = (
        os.environ.get("S2_API_KEY")
        or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        or ""
    )
    return {"x-api-key": key} if key else {}


def _s2_record(item: Any) -> SourceRecord | None:
    if not isinstance(item, Mapping):
        return None
    title = _text(item.get("title"))
    if not title:
        return None
    external = item.get("externalIds")
    external = external if isinstance(external, Mapping) else {}
    authors = [
        _text(author.get("name"))
        for author in item.get("authors") or []
        if isinstance(author, Mapping)
    ]
    open_access = item.get("openAccessPdf")
    open_access = open_access if isinstance(open_access, Mapping) else {}
    return SourceRecord(
        source="semantic_scholar",
        title=title,
        doi=normalise_doi(external.get("DOI")),
        arxiv=normalise_arxiv(external.get("ArXiv")),
        year=item.get("year"),
        venue=_text(item.get("venue")),
        url=_text(item.get("url")),
        authors=", ".join(name for name in authors if name)[:400],
        abstract=_text(item.get("abstract"))[:4000],
        pdf_url=_text(open_access.get("url")),
    )


def search_semantic_scholar(query: str, limit: int) -> list[SourceRecord]:
    params = urllib.parse.urlencode({
        "query": query,
        "limit": max(1, min(int(limit), 50)),
        "fields": _S2_FIELDS,
    })
    payload = _json(
        "https://api.semanticscholar.org/graph/v1/paper/search?" + params,
        headers=_s2_headers(),
    )
    items = payload.get("data") if isinstance(payload, Mapping) else None
    return [
        record for record in (_s2_record(item) for item in items or [])
        if record is not None
    ]


def lookup_semantic_scholar(doi: str) -> SourceRecord | None:
    doi = normalise_doi(doi)
    if not doi:
        return None
    payload = _json(
        "https://api.semanticscholar.org/graph/v1/paper/DOI:"
        + urllib.parse.quote(doi, safe="/")
        + "?fields=" + urllib.parse.quote(_S2_FIELDS, safe=","),
        headers=_s2_headers(),
    )
    return _s2_record(payload)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Source:
    name: str
    search: Callable[[str, int], list[SourceRecord]]
    lookup: Callable[[str], SourceRecord | None] | None = None


SOURCES: dict[str, Source] = {
    source.name: source for source in (
        Source("openalex", search_openalex, lookup_openalex),
        Source("crossref", search_crossref, lookup_crossref),
        Source("arxiv", search_arxiv, lookup_arxiv),
        Source("semantic_scholar", search_semantic_scholar,
               lookup_semantic_scholar),
    )
}
SOURCE_NAMES = tuple(SOURCES)


def parse_source_names(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        items = [part.strip() for part in str(value or "").split(",")]
    return [item for item in dict.fromkeys(items) if item]


def resolve_source_names(value: Any = None) -> list[str]:
    """Keep only registered names, preserving the caller's order."""
    if value is None:
        return list(SOURCE_NAMES)
    requested = parse_source_names(value)
    return [name for name in requested if name in SOURCES]


def _clamp_queries(queries: Sequence[Any]) -> list[str]:
    values: list[str] = []
    for item in queries:
        text = item.get("query") if isinstance(item, Mapping) else item
        value = " ".join(str(text or "").split())
        if value and value.lower() not in {seen.lower() for seen in values}:
            values.append(value)
    return values


def collect(queries: Sequence[Any], *, per_query: int = 8,
            source_names: Any = None, run_dir: Path | None = None,
            max_queries: int = DEFAULT_CROSS_QUERIES,
            workers: int = 6) -> tuple[SourceIndex, dict[str, Any]]:
    """Query every independent source and build one matching index.

    Failures are recorded per source and never raised: cross-checking must not
    be able to break retrieval when a free API is rate-limited.
    """
    names = resolve_source_names(source_names)
    selected = _clamp_queries(queries)[:max(1, int(max_queries))]
    index = SourceIndex()
    audit: dict[str, Any] = {
        "enabled": bool(names and selected),
        "queries": selected,
        "sources": [],
        "records": 0,
    }
    if not names or not selected:
        return index, audit

    by_source: dict[str, list[SourceRecord]] = {name: [] for name in names}
    errors: dict[str, list[str]] = {name: [] for name in names}
    tasks = [(name, query) for name in names for query in selected]
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(tasks)))) as pool:
        futures = {
            pool.submit(SOURCES[name].search, query, int(per_query)): name
            for name, query in tasks
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                by_source[name].extend(future.result() or [])
            except Exception as exc:  # noqa: BLE001 - one API must not stop others
                errors[name].append(f"{type(exc).__name__}: {str(exc)[:160]}")

    for name in names:
        deduped: dict[str, SourceRecord] = {}
        for record in by_source[name]:
            deduped.setdefault(_identity(record), record)
        records = list(deduped.values())
        index.extend(records)
        audit["sources"].append({
            "name": name,
            "queries": len(selected),
            "records": len(records),
            "error": "; ".join(errors[name]),
        })
        if run_dir is not None:
            write_json(
                run_dir / "01_retrieval" / "sources" / f"{name}.json",
                {
                    "source": name,
                    "queries": selected,
                    "records": [record.as_dict() for record in records],
                    "error": "; ".join(errors[name]),
                },
            )
    audit["records"] = len(index.records)
    return index, audit


def lookup_many(dois: Sequence[str], *, source_names: Any = None,
                workers: int = 4, limit: int = 8
                ) -> tuple[list[SourceRecord], list[dict[str, Any]]]:
    """Resolve bare DOIs against independent sources (bounded, best effort)."""
    names = [name for name in resolve_source_names(source_names)
             if SOURCES[name].lookup is not None]
    wanted = [normalise_doi(doi) for doi in dois if normalise_doi(doi)]
    wanted = list(dict.fromkeys(wanted))[:max(0, int(limit))]
    if not names or not wanted:
        return [], []
    records: list[SourceRecord] = []
    audit: list[dict[str, Any]] = []
    tasks = [(doi, name) for doi in wanted for name in names]
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(tasks)))) as pool:
        futures = {}
        for doi, name in tasks:
            lookup = SOURCES[name].lookup
            if lookup is not None:
                futures[pool.submit(lookup, doi)] = (doi, name)
        for future in as_completed(futures):
            doi, name = futures[future]
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001
                audit.append({"doi": doi, "source": name, "found": False,
                              "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
                continue
            audit.append({"doi": doi, "source": name,
                          "found": record is not None, "error": ""})
            if record is not None:
                records.append(record)
    return records, audit
