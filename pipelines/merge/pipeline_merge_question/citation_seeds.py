"""Citation-linked candidate seeds for the cross-paper relation judge.

Two papers in the same pool that cite each other already stand in a relation
that no retrieval model has to discover: the citing paper builds on, corrects,
extends or bounds the cited one.  A purely semantic candidate search can still
miss such a pair (the two claims may share little vocabulary), so the reference
list is used as a high-precision *seed*: the pairs it nominates are always handed
to the judge, whether or not retrieval proposed them.

The seed only selects which claim pairs get judged.  It never asserts an edge:
the judge still decides the relation, and a seeded pair the judge rejects
produces nothing.

Scope is deliberately bounded.  A citation is a paper-level fact, and the
cross-product of two papers' claims is far more pairs than the judge should see,
so each citation contributes at most ``per_citation`` claim pairs, chosen by
lexical overlap and then by id, which keeps the selection deterministic.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from pipeline_harness.models import JSONDict

CITATION_KIND = "cross_paper.citations"
_WORD = re.compile(r"[a-z][a-z\-]{3,}")
_STOP = frozenset("""that this with from they their which when where have been being
than then them these those such only also more most less least other others into
over under between among across rather study studies show shows shown using used
based results result however moreover further""".split())


def tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(str(text).lower()) if w not in _STOP)


def citation_pairs(payloads: Iterable[Any]) -> list[tuple[str, str]]:
    """Package-level ``(citing, cited)`` pairs from the citation artifacts."""
    pairs: list[tuple[str, str]] = []
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        for item in payload.get("pairs") or []:
            if not isinstance(item, Mapping):
                continue
            citing = str(item.get("citing_package") or "")
            cited = str(item.get("cited_package") or "")
            if citing and cited and citing != cited:
                pairs.append((citing, cited))
    return sorted(set(pairs))


def seed_candidates(
    records: Mapping[str, JSONDict],
    pairs: Sequence[tuple[str, str]],
    *,
    per_citation: int = 8,
) -> list[JSONDict]:
    """Deterministic claim-pair seeds for the citation-linked packages."""
    by_package: dict[str, list[str]] = defaultdict(list)
    for qid, record in records.items():
        by_package[str(record["package"])].append(qid)
    cache: dict[str, frozenset[str]] = {}

    def toks(qid: str) -> frozenset[str]:
        if qid not in cache:
            cache[qid] = tokens(records[qid]["content"])
        return cache[qid]

    seeds: list[JSONDict] = []
    seen: set[tuple[str, str]] = set()
    for citing, cited in pairs:
        left, right = sorted(by_package.get(citing, ())), sorted(by_package.get(cited, ()))
        if not left or not right:
            continue
        scored: list[tuple[int, str, str]] = []
        for a in left:
            ta = toks(a)
            for b in right:
                scored.append((len(ta & toks(b)), a, b))
        # Highest lexical overlap first, id order second, so the same corpus
        # always yields the same seeds.
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        for _overlap, a, b in scored[:per_citation]:
            key = (a, b) if a < b else (b, a)
            if key in seen:
                continue
            seen.add(key)
            seeds.append({
                "source_qids": [a, b],
                "propositions": [records[a], records[b]],
                "hint": {"direction": "citation",
                         "reason": f"{citing} cites {cited} (reference-list match)"},
            })
    return seeds


__all__ = ["CITATION_KIND", "citation_pairs", "seed_candidates", "tokens"]
