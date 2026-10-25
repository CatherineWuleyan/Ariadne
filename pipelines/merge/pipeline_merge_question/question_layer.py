"""Question-led answer layer for the merge domain graph.

The merge pipeline (Step 0-5) builds a claim-level graph.  A *question-led*
domain graph needs one more product: a small set of candidate answers (roots)
and a defensible set of links from existing claims to those answers, so a
reader can enter the graph from the question instead of scanning 300 claims.

This module is deliberately separate from Step 3's domain-conclusion tree.  It
does not synthesize new domain facts and it never rewrites a claim.  It only:

1. proposes 3-6 candidate *answers* to a supplied question, each grounded in a
   digest of the existing graph;
2. asks a second, per-root judge to link existing claims as ``support``,
   ``oppose`` or ``bounds`` (everything else is ``none``);
3. materializes the answer layer as root nodes + answer edges, and enumerates
   the premise-to-answer chains for the viewer.

The generated roots are candidate answers, not proven conclusions.  Their
verdict is a description of graph support (how many claims/papers/chains), not
a truth value.  This is why the emitted nodes carry ``verdict_class`` and a
caveat instead of pretending to be a domain conclusion.

The module is importable from the copied pipeline package and from the
standalone runner.  All LLM calls go through ``step3._post_json`` so the same
DEEPSEEK_* environment and retry policy are reused.
"""
from __future__ import annotations

import copy
import json
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from .step3 import _post_json

SCHEMA_NAME = "gaia.integration.question_answer_layer"
SCHEMA_VERSION = "0.1.0"
PROMPT_VERSION = "question-answer-v1"
ANSWER_TYPES = {"method_route", "modeling_choice", "design_decision", "validation_route"}
LINK_RELATIONS = {"support", "oppose", "bounds"}
# How the claims are put to the judge.
#   routed: one call per claim, every candidate in context. The claim is asked
#           to pick the route it bears on.
#           Measured with a route and its negation in the same candidate set:
#           the model linked the same claim to both, but as support for one and
#           OPPOSE for the other, and never supported two candidates at once.
#           Seeing the opposite is what lets it fix the polarity -- judged alone,
#           the negated route collected 7 of 9 supports instead. The audit keeps
#           both counts apart: same claim on several routes, and same claim
#           *supporting* several.
#   budget: legacy -- one call per candidate, bounded pool, at most ten links.
#           Kept so frozen runs and old configurations stay reproducible.
LINK_PROTOCOL = "routed"
LINK_PROTOCOL_CHOICES = ("routed", "budget")
LINK_WORKERS_DEFAULT = 8
LINK_POOL_LIMIT = 44
SECOND_OPINION_LIMIT = 2
# A second paper must carry at least this share of the dominant paper's support
# before a root counts as corroborated. Routing hands every route a usable set
# of claims, so an absolute count no longer separates a route one paper carries
# from a route several papers carry.
VERDICT_DOMINANCE_FLOOR = 0.2
LINK_LABELS = {"support": "supports", "oppose": "opposes", "bounds": "bounds"}
VERDICT_CLASSES = {"primary", "partial", "contested", "exploratory"}
VERDICT_COLORS = {
    "primary": "#b42318",
    "partial": "#b54708",
    "contested": "#6941c6",
    "exploratory": "#475467",
}
VERDICT_LABELS = {
    "primary": "multi-paper direct support",
    "partial": "single-paper core",
    "contested": "opposed in the graph",
    "exploratory": "exploratory",
}
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "their", "there",
    "these", "those", "which", "were", "was", "are", "is", "be", "been", "being",
    "can", "could", "may", "might", "will", "would", "should", "must", "has", "have",
    "had", "not", "but", "also", "such", "than", "then", "when", "where", "while",
    "using", "used", "use", "based", "under", "over", "between", "within", "across",
    "each", "other", "more", "most", "some", "any", "all", "both", "only", "its",
    "it", "they", "them", "we", "our", "their", "a", "an", "of", "to", "in", "on",
    "by", "as", "at", "or", "if", "so", "do", "does", "did", "how", "what", "why",
}
_MAX_CHAINS_PER_ROOT = 90
_MAX_CHAIN_DEPTH = 8
_MAX_TOTAL_CHAINS = 320


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------
def paper_of(node_id: str) -> str:
    match = re.match(r"^(?:knowledge:)?papers:(paper_[^:]+)::", str(node_id))
    if match:
        return match.group(1)
    value = str(node_id)
    cut = value.find("::")
    return value[:cut].replace("knowledge:", "").replace("papers:", "") if cut > 0 else "other"


def papers_of_node(node: Mapping[str, Any]) -> list[str]:
    """All source papers a node stands on.

    Integration-owned K conclusions carry ``package_provenance`` in their
    details; a K node has no ``papers:`` id of its own, so using the id alone
    would count every domain conclusion as one opaque "other" paper.
    """
    details = node.get("details") if isinstance(node.get("details"), Mapping) else {}
    metadata = details.get("metadata") if isinstance(details.get("metadata"), Mapping) else {}
    provenance = metadata.get("package_provenance") or []
    if not isinstance(provenance, list):
        provenance = []
    papers = sorted({
        match.group(1)
        for item in provenance
        if isinstance(item, str)
        and (match := re.fullmatch(r"papers:([^:]+)", item))
        and match.group(1) != "other"
    })
    if papers:
        return papers
    # "other" is a grouping fallback, never an independently sourced paper.
    match = re.match(r"^(?:knowledge:)?papers:([^:]+)::", str(node.get("id")))
    return [match.group(1)] if match and match.group(1) != "other" else []


def primary_paper_of_node(node: Mapping[str, Any]) -> str:
    papers = papers_of_node(node)
    return papers[0] if papers else "other"


def node_code(node: Mapping[str, Any]) -> str:
    return str(node.get("display_label") or node.get("label") or node.get("entity_id") or node.get("id"))


def node_text(node: Mapping[str, Any]) -> str:
    return str(node.get("summary") or (node.get("details") or {}).get("content") or "")


def short_text(value: Any, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def degree_map(edges: Iterable[Mapping[str, Any]]) -> Counter:
    degree: Counter = Counter()
    for edge in edges:
        degree[str(edge.get("source"))] += 1
        degree[str(edge.get("target"))] += 1
    return degree


def infer_up_adjacency(edges: Iterable[Mapping[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    """conclusion -> [(premise, relation)] for traversable reasoning edges.

    Only directed ``infer`` edges become a chain step.  ``conjunction`` is a
    symmetric operator: walking it would turn one branch into another and is
    exactly the failure mode the viewer's chain traversal avoids.
    """
    up: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for edge in edges:
        rel = str(edge.get("semantic_type") or edge.get("label") or "")
        if rel == "infer":
            up[str(edge.get("target"))].append((str(edge.get("source")), "infer"))
    return up


def normalize_edge_roles(edge_roles: Any) -> dict[str, dict[str, Any]]:
    """Accept both {edge_id: role} and [{edge_id, ...}] role collections."""
    if not edge_roles:
        return {}
    if isinstance(edge_roles, Mapping):
        result = {}
        for edge_id, role in edge_roles.items():
            if isinstance(role, Mapping):
                item = dict(role)
                item.setdefault("edge_id", str(edge_id))
                result[str(edge_id)] = item
        return result
    if isinstance(edge_roles, list):
        return {
            str(item.get("edge_id")): dict(item)
            for item in edge_roles
            if isinstance(item, Mapping) and item.get("edge_id")
        }
    return {}


def chain_up_adjacency(
    edges: Iterable[Mapping[str, Any]],
    edge_roles: Any,
) -> dict[str, list[tuple[str, str]]]:
    """conclusion -> [(premise, type)] for *chainable* edges only.

    `infer` is the residual class and is not chainable.  Edges without a role
    are treated as non-chainable: a missing audit is not evidence.
    """
    roles = normalize_edge_roles(edge_roles)
    up: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for edge in edges:
        rel = str(edge.get("semantic_type") or edge.get("label") or "")
        # Legacy projections used the display label infer for every directed
        # edge; native projections retain deduction as the label as well as
        # the audited role. Both must reach the same role gate.
        if rel not in {"infer", "deduction"}:
            continue
        role = roles.get(str(edge.get("id")))
        if role is not None:
            chainable = bool(role.get("chainable"))
            reasoning_type = str(role.get("reasoning_type") or "infer")
        else:
            # A native typed view (Step 4/5 projection) carries the role on the
            # edge itself; a missing flag is conservative, not chainable.
            chainable = bool(edge.get("chainable") or (edge.get("details") or {}).get("chainable"))
            reasoning_type = str(edge.get("reasoning_type") or (edge.get("details") or {}).get("reasoning_type") or "infer")
        if not chainable:
            continue
        up[str(edge.get("target"))].append((str(edge.get("source")), reasoning_type))
    return up


def all_adjacency(edges: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        source, target = str(edge.get("source")), str(edge.get("target"))
        adjacency[source].add(target)
        adjacency[target].add(source)
    return adjacency


def tokens(value: Any) -> set[str]:
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", str(value or "").lower())
    return {token for token in raw if token not in STOPWORDS}


def lexical_score(query_tokens: set[str], value: Any) -> float:
    if not query_tokens:
        return 0.0
    candidate = tokens(value)
    if not candidate:
        return 0.0
    overlap = len(query_tokens & candidate)
    return overlap / (len(query_tokens) ** 0.5)


# ---------------------------------------------------------------------------
# Digest and root generation
# ---------------------------------------------------------------------------
def build_digest(view: Mapping[str, Any], max_claims: int = 72) -> dict[str, Any]:
    """A deterministic, bounded view of the graph for the root-generation call.

    Selection: top claims by degree, then the two strongest claims of every
    paper (so no paper is invisible), then the strongest integration-delta
    claims.  The paper counts and cross-paper relations are included so the
    model can see the graph's actual organisation instead of a flat claim list.
    """
    nodes = list(view.get("nodes") or [])
    edges = list(view.get("edges") or [])
    by_id = {str(node.get("id")): node for node in nodes}
    degree = degree_map(edges)
    paper_counts = Counter(primary_paper_of_node(node) for node in nodes)
    ranked = sorted(nodes, key=lambda node: (-degree[str(node.get("id"))], node_code(node)))

    selected: list[Mapping[str, Any]] = []
    seen: set[str] = set()

    def add(node: Mapping[str, Any]) -> None:
        nid = str(node.get("id"))
        if nid and nid not in seen and (node.get("kind") or "claim") != "answer_root":
            seen.add(nid)
            selected.append(node)

    for node in ranked[:46]:
        add(node)
    by_paper: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for node in ranked:
        by_paper[primary_paper_of_node(node)].append(node)
    for paper in sorted(by_paper):
        for node in by_paper[paper][:1]:
            add(node)
    delta = [node for node in ranked if node.get("layer") == "weakpoints"]
    for node in delta[:12]:
        add(node)

    selected = selected[:max_claims]
    claims = [{
        "id": str(node.get("id")),
        "code": node_code(node),
        "paper": primary_paper_of_node(node),
        "layer": str(node.get("layer") or "claims"),
        "degree": degree[str(node.get("id"))],
        "content": short_text(node_text(node), 300),
    } for node in selected]

    cross_edges = []
    for edge in edges:
        if edge.get("layer") != "weakpoints":
            continue
        source, target = by_id.get(str(edge.get("source"))), by_id.get(str(edge.get("target")))
        if not source or not target:
            continue
        cross_edges.append({
            "source": node_code(source),
            "target": node_code(target),
            "relation": str(edge.get("semantic_type") or edge.get("label") or ""),
        })
    domain_conclusions = [node for node in nodes if re.match(r"^K\d", node_code(node))]
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "paper_count": len(paper_counts),
        "domain_conclusion_count": len(domain_conclusions),
        "paper_counts": dict(sorted(paper_counts.items())),
        "delta_node_count": sum(1 for node in nodes if node.get("layer") == "weakpoints"),
        "claims": claims,
        "cross_paper_relations": cross_edges[:80],
        "selected_ids": [str(node.get("id")) for node in selected],
    }


def build_root_prompt(question: Mapping[str, str], digest: Mapping[str, Any], max_roots: int = 5) -> str:
    return f"""You are building a question-led domain graph from a merged cross-paper claim graph.

QUESTION (zh): {question.get('zh', '')}
QUESTION (en): {question.get('en', '') or '(not supplied)'}
DOMAIN: {question.get('domain', '') or 'aircraft simulation design'}

The graph has {digest['node_count']} claims from {digest['paper_count']} papers; {digest['delta_node_count']} claims participate in cross-paper integration. {digest.get('domain_conclusion_count', 0) and f"The graph contains {digest['domain_conclusion_count']} domain conclusions (K nodes): prefer a K conclusion as the direct evidence for an answer when one bears on it, and use raw claims only where no K exists." or "There is no domain-conclusion layer yet, so answers must be grounded directly in claims."}

Propose {max_roots} possible high-level ANSWERS to the question (or fewer only if the graph cannot support {max_roots} genuinely different routes). Each answer is a candidate route/position that the graph could support. It must be a claim about HOW the domain work can be done, not a specific case result from one aircraft and not a paraphrase of one paper.

Rules:
- distinct, non-overlapping routes; do not propose "combine all of them".
- aim for coverage of different workflow stages where the graph supports them: overall design environment/integration, simulation-model formulation, model calibration/identification, optimization, and validation. Do not force a stage that has no claims.
- grounded only in the supplied claims. Do not add numbers, methods, tools, or conditions absent from them.
- statement: one self-contained English sentence, cautious (what this route can provide or when it applies), never a universal guarantee.
- title_zh: <=18 Chinese characters; title_en: <=12 words.
- criteria: one English sentence describing what evidence would support this answer.
- seed_claim_ids: 3-8 ids from CLAIMS that are most relevant (use ids exactly as given).
- answer_type: one of method_route, modeling_choice, design_decision, validation_route.

Return JSON only:
{{"answers":[{{"id":"R1","title_zh":"...","title_en":"...","statement":"...","answer_type":"...","scope":"...","criteria":"...","seed_claim_ids":["..."]}}]}}

PAPER_COUNTS={json.dumps(digest['paper_counts'], ensure_ascii=False, sort_keys=True)}
CLAIMS={json.dumps(digest['claims'], ensure_ascii=False, sort_keys=True)}
"""


def _seed_overlap(seeds: list[str], statement: str, view: Mapping[str, Any]) -> dict[str, Any]:
    """Lexical agreement between a root's statement and the seeds it cites."""
    by_id = {str(node.get("id")): node for node in view.get("nodes") or []}
    statement_tokens = tokens(statement)
    overlaps: list[float] = []
    for seed in seeds:
        node = by_id.get(seed)
        if node is None:
            continue
        seed_tokens = tokens(node_text(node))
        if not seed_tokens or not statement_tokens:
            overlaps.append(0.0)
            continue
        overlaps.append(len(statement_tokens & seed_tokens) / max(1, len(seed_tokens)))
    if not overlaps:
        return {"mean": 0.0, "min": 0.0, "max": 0.0, "seeds_measured": 0,
                "seeds_with_overlap": 0}
    return {
        "mean": round(sum(overlaps) / len(overlaps), 3),
        "min": round(min(overlaps), 3),
        "max": round(max(overlaps), 3),
        "seeds_measured": len(overlaps),
        "seeds_with_overlap": sum(1 for value in overlaps if value > 0),
    }


def _fallback_seeds(root: Mapping[str, Any], view: Mapping[str, Any], limit: int = 5) -> list[str]:
    query = tokens(" ".join(str(root.get(key) or "") for key in ("title_en", "title_zh", "statement", "criteria")))
    scored = []
    for node in view.get("nodes") or []:
        if node.get("kind") == "answer_root":
            continue
        score = lexical_score(query, node_text(node))
        if score > 0:
            scored.append((score, str(node.get("id"))))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [nid for _, nid in scored[:limit]]


def generate_roots(
    view: Mapping[str, Any],
    question: Mapping[str, str],
    *,
    call_json: Callable[[str], Any] = _post_json,
    max_roots: int = 5,
    digest: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    digest = digest or build_digest(view)
    prompt = build_root_prompt(question, digest, max_roots=max_roots)
    response = call_json(prompt)
    raw_answers = response.get("answers") if isinstance(response, Mapping) else None
    if not isinstance(raw_answers, list):
        raw_answers = response.get("roots") if isinstance(response, Mapping) else None
    if not isinstance(raw_answers, list):
        raise ValueError("root generation response must contain an 'answers' list")

    known = {str(node.get("id")) for node in view.get("nodes") or []}
    roots: list[dict[str, Any]] = []
    used_codes: set[str] = set()
    seen_text: list[set[str]] = []
    for index, raw in enumerate(raw_answers[:max_roots]):
        if not isinstance(raw, Mapping):
            continue
        statement = str(raw.get("statement") or raw.get("summary_en") or "").strip()
        if not statement:
            continue
        # A root that repeats another root adds no navigational choice.
        fingerprint = tokens(statement)
        if any(fingerprint and len(fingerprint & other) / max(1, len(fingerprint | other)) >= 0.72
               for other in seen_text):
            continue
        seen_text.append(fingerprint)
        code = str(raw.get("id") or f"R{index + 1}").strip().upper()
        if not re.fullmatch(r"R\d+[A-Za-z]?", code) or code in used_codes:
            number = index + 1
            while f"R{number}" in used_codes:
                number += 1
            code = f"R{number}"
        used_codes.add(code)
        seeds = [str(item) for item in raw.get("seed_claim_ids") or [] if str(item) in known]
        if not seeds:
            seeds = _fallback_seeds(raw, view)
        # Seeds are written by the same call that wrote the statement, so citing
        # them proves nothing by itself. Measure the lexical overlap and record
        # it: a root that names six seeds of which none share a term with its own
        # statement is a root whose grounding should be read, not counted.
        seed_overlap = _seed_overlap(seeds, statement, view)
        title_zh = str(raw.get("title_zh") or raw.get("title") or "").strip()
        title_en = str(raw.get("title_en") or raw.get("title") or code).strip()
        roots.append({
            "code": code,
            "id": f"answer:{code.lower()}",
            "title_zh": title_zh or title_en,
            "title_en": title_en,
            "statement": statement,
            "answer_type": str(raw.get("answer_type") or "method_route"),
            "scope": str(raw.get("scope") or "").strip(),
            "criteria": str(raw.get("criteria") or "").strip(),
            "seed_claim_ids": list(dict.fromkeys(seeds))[:8],
            "seed_overlap": seed_overlap,
        })
    if not roots:
        raise ValueError("root generation returned no usable answer")
    for root in roots:
        if root["answer_type"] not in ANSWER_TYPES:
            root["answer_type"] = "method_route"
    return roots, {"prompt": prompt, "response": response, "digest": digest}


# ---------------------------------------------------------------------------
# Linking claims to one root
# ---------------------------------------------------------------------------
def candidate_pool(
    view: Mapping[str, Any],
    root: Mapping[str, Any],
    *,
    limit: int = 44,
    broaden: bool = False,
) -> list[dict[str, Any]]:
    nodes = [node for node in view.get("nodes") or [] if node.get("kind") != "answer_root"]
    by_id = {str(node.get("id")): node for node in nodes}
    degree = degree_map(view.get("edges") or [])
    adjacency = all_adjacency(view.get("edges") or [])
    query = tokens(" ".join(str(root.get(key) or "") for key in ("title_zh", "title_en", "statement", "criteria", "scope")))
    picked: list[str] = []
    seen: set[str] = set()

    def add(node_id: str) -> None:
        if node_id in by_id and node_id not in seen:
            seen.add(node_id)
            picked.append(node_id)

    for node_id in root.get("seed_claim_ids") or []:
        add(str(node_id))
    for node_id in list(picked):
        for neighbor in sorted(adjacency.get(node_id, set())):
            add(neighbor)
    lexical = []
    for node in nodes:
        score = lexical_score(query, node_text(node))
        if score > 0:
            lexical.append((score, -degree[str(node.get("id"))], str(node.get("id"))))
    lexical.sort(key=lambda item: (-item[0], item[1], item[2]))
    for _, _, node_id in lexical[: (34 if broaden else 16)]:
        add(node_id)
    by_paper: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for node in nodes:
        by_paper[primary_paper_of_node(node)].append((degree[str(node.get("id"))], str(node.get("id"))))
    for paper in sorted(by_paper):
        for _, node_id in sorted(by_paper[paper], key=lambda item: (-item[0], item[1]))[: (3 if broaden else 2)]:
            add(node_id)
    delta = sorted(
        (node for node in nodes if node.get("layer") == "weakpoints"),
        key=lambda node: (-degree[str(node.get("id"))], node_code(node)),
    )
    for node in delta[: (8 if broaden else 5)]:
        add(str(node.get("id")))
    seeds = {str(node_id) for node_id in root.get("seed_claim_ids") or []}
    result = []
    for node_id in picked[:limit]:
        node = by_id[node_id]
        result.append({
            "id": node_id,
            "code": node_code(node),
            "paper": primary_paper_of_node(node),
            "layer": str(node.get("layer") or "claims"),
            "degree": degree[node_id],
            "seed": node_id in seeds,
            "content": short_text(node_text(node), 300),
        })
    return result


def build_link_prompt(
    question: Mapping[str, str],
    root: Mapping[str, Any],
    candidates: list[Mapping[str, Any]],
) -> str:
    answer = {
        "id": root["code"],
        "title_zh": root["title_zh"],
        "title_en": root["title_en"],
        "statement": root["statement"],
        "criteria": root["criteria"],
        "scope": root["scope"],
        "answer_type": root["answer_type"],
    }
    return f"""You are auditing which claims in a merged cross-paper graph actually bear on ONE candidate answer.

QUESTION (zh): {question.get('zh', '')}
QUESTION (en): {question.get('en', '') or '(not supplied)'}
ANSWER={json.dumps(answer, ensure_ascii=False, sort_keys=True)}

For each CLAIM decide whether it bears on the answer:
- support: the claim states a method, result, or design fact that directly supports this answer route. Same topic alone is not enough; the reason must name what the claim contributes.
- oppose: the claim reports a result that is incompatible with the answer under compatible conditions. Do not infer opposition from a different aircraft, dataset, model, or condition.
- bounds: the claim gives a condition, scope limit, or assumption that constrains when the answer holds.
- none: topical only, or not enough information.

Rules:
- use exact claim_id values from CANDIDATES; never invent an id.
- if a K domain conclusion is among the candidates, prefer it over a raw claim that merely restates the same proposition; the raw claim can still be used as a chain premise.
- return at most 10 links, and at least one support only if the graph genuinely supports the answer.
- a claim may receive at most one relation; if it is ambiguous, use none.
- reason: one short English sentence (<= 25 words) naming the contribution of the claim.
- the candidate set is deliberately bounded. A missing claim is not evidence that the answer is unsupported; use none.
- if there is no defensible support, return an empty links list; the answer will be marked exploratory.

Return JSON only:
{{"root_id":{json.dumps(root['code'])}, "links":[{{"claim_id":"...","relation":"support|oppose|bounds","reason":"..."}}]}}

CANDIDATES={json.dumps(list(candidates), ensure_ascii=False, sort_keys=True)}
"""


def _normalize_links(
    response: Any,
    root: Mapping[str, Any],
    candidates: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    allowed = {str(item["id"]) for item in candidates}
    raw_links = response.get("links") if isinstance(response, Mapping) else None
    if not isinstance(raw_links, list):
        return []
    # Collect first, decide after. Keeping only the first link per claim made the
    # outcome depend on the order the judge happened to emit, which is neither
    # meaningful nor reproducible.
    collected: list[dict[str, str]] = []
    for raw in raw_links:
        if not isinstance(raw, Mapping):
            continue
        claim_id = str(raw.get("claim_id") or raw.get("id") or "")
        relation = str(raw.get("relation") or "").strip().lower()
        if claim_id not in allowed or relation not in LINK_RELATIONS:
            continue
        reason = re.sub(r"\s+", " ", str(raw.get("reason") or "")).strip()
        if not reason:
            continue
        collected.append({
            "claim_id": claim_id,
            "relation": relation,
            "reason": reason[:300],
        })
    # A support and an oppose on the same claim means the judge did not decide;
    # keep neither rather than drawing a contradictory pair.
    by_claim: dict[str, set[str]] = defaultdict(set)
    for link in collected:
        by_claim[link["claim_id"]].add(link["relation"])
    conflicting = {claim for claim, rels in by_claim.items() if len(rels) > 1}
    kept: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for link in collected:
        key = (link["claim_id"], link["relation"])
        if link["claim_id"] in conflicting or key in seen:
            continue
        seen.add(key)
        kept.append(link)
    return kept[:12]


def link_roots(
    view: Mapping[str, Any],
    question: Mapping[str, str],
    roots: list[Mapping[str, Any]],
    *,
    call_json: Callable[[str], Any] = _post_json,
    broaden_retry: bool = True,
    protocol: str | None = None,
    multi_route: bool = True,
    workers: int | None = None,
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, Any]]]:
    """Link claims to candidates with the configured protocol.

    ``routed`` (default) asks each claim where it belongs; ``budget`` keeps the
    original per-candidate, ten-link behaviour for frozen runs.
    """
    chosen = protocol or os.environ.get("ARIADNE_LINK_PROTOCOL", LINK_PROTOCOL)
    if chosen not in LINK_PROTOCOL_CHOICES:
        raise ValueError(f"unknown link protocol: {chosen}")
    if chosen == "routed":
        return link_roots_routed(
            view, question, roots, call_json=call_json,
            multi_route=multi_route, workers=workers,
        )
    links_by_root: dict[str, list[dict[str, str]]] = {}
    raw_calls: list[dict[str, Any]] = []
    for root in roots:
        candidates = candidate_pool(view, root)
        prompt = build_link_prompt(question, root, candidates)
        response = call_json(prompt)
        links = _normalize_links(response, root, candidates)
        raw_calls.append({"root": root["code"], "prompt": prompt, "response": response, "candidates": candidates})
        if not any(link["relation"] == "support" for link in links) and broaden_retry:
            wider = candidate_pool(view, root, limit=60, broaden=True)
            retry_prompt = build_link_prompt(question, root, wider)
            retry = call_json(retry_prompt)
            retry_links = _normalize_links(retry, root, wider)
            raw_calls.append({"root": root["code"], "retry": True, "prompt": retry_prompt,
                              "response": retry, "candidates": wider})
            if any(link["relation"] == "support" for link in retry_links):
                links = retry_links
                candidates = wider
        links_by_root[root["code"]] = links
    return links_by_root, raw_calls


_LINK_RUBRIC = """A claim can bear on a candidate in exactly one way:
- support: the claim states a method, result, or design fact that directly supports this route. Same topic alone is not enough; the reason must name what the claim contributes.
- oppose: the claim reports a result that is incompatible with the route under compatible conditions. Do not infer opposition from a different aircraft, dataset, model, or condition.
- bounds: the claim gives a condition, scope limit, or assumption that constrains when the route holds.
- none: topical only, not enough information, or the claim argues for a different recommendation."""


def build_routed_prompt(question: Mapping[str, str], claim: Mapping[str, Any],
                        roots: list[Mapping[str, Any]]) -> str:
    """One claim, every candidate: the claim has to pick where it belongs."""
    routes = [
        {"code": str(root.get("code")), "statement": str(root.get("statement") or "")}
        for root in roots
    ]
    return f"""Judge ONE claim against EVERY candidate route at once.

QUESTION (zh): {question.get('zh', '')}
QUESTION (en): {question.get('en', '') or '(not supplied)'}

{_LINK_RUBRIC}

Candidates compete: if the claim argues for one route, it does not support the
route that says the opposite. A claim may bear on more than one candidate only
when it genuinely bears on each; otherwise name the one it bears on.

CLAIM {claim.get('code')}: {claim.get('content')}

CANDIDATES={json.dumps(routes, ensure_ascii=False, sort_keys=True)}

Return JSON only, with one entry per candidate you can justify (an empty list is allowed):
{{"claim_id": {json.dumps(str(claim.get('id')))}, "links": [{{"candidate": "R1", "relation": "support|oppose|bounds", "reason": "one short sentence"}}]}}"""


def build_second_opinion_prompt(question: Mapping[str, str], claim: Mapping[str, Any],
                                root: Mapping[str, Any]) -> str:
    """One claim, one neighbouring route: does it also bear on this one?"""
    answer = {"id": root.get("code"), "statement": root.get("statement") or ""}
    return f"""Judge ONE claim against ONE candidate route.

QUESTION (zh): {question.get('zh', '')}
QUESTION (en): {question.get('en', '') or '(not supplied)'}
ANSWER={json.dumps(answer, ensure_ascii=False, sort_keys=True)}

{_LINK_RUBRIC}

The claim is already evidence for a neighbouring route. Judge only whether it
ALSO bears on this one. Answering none is the normal outcome.

CLAIM {claim.get('code')}: {claim.get('content')}

Return JSON only:
{{"claim_id": {json.dumps(str(claim.get('id')))}, "relation": "support|oppose|bounds|none", "reason": "one short sentence"}}"""


def _parse_routed(response: Any, allowed: set[str]) -> dict[str, dict[str, str]]:
    raw = response.get("links") if isinstance(response, Mapping) else None
    picked: dict[str, dict[str, str]] = {}
    for item in raw or []:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("candidate") or item.get("candidate_id") or "")
        relation = str(item.get("relation") or "").strip().lower()
        reason = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()
        if code not in allowed or relation not in LINK_RELATIONS or not reason or code in picked:
            continue
        picked[code] = {"relation": relation, "reason": reason[:300]}
    return picked


def _parse_second_opinion(response: Any) -> dict[str, str] | None:
    if not isinstance(response, Mapping):
        return None
    relation = str(response.get("relation") or "").strip().lower()
    reason = re.sub(r"\s+", " ", str(response.get("reason") or "")).strip()
    if relation not in LINK_RELATIONS or not reason:
        return None
    return {"relation": relation, "reason": reason[:300]}


def link_workers(explicit: int | None = None) -> int:
    raw = explicit
    if raw is None:
        env = os.environ.get("ARIADNE_LINK_WORKERS", "").strip()
        raw = int(env) if env.isdigit() else LINK_WORKERS_DEFAULT
    return max(1, int(raw))


def _closest_route(roots: list[Mapping[str, Any]]) -> dict[str, str]:
    """For each route, the most similar other route by statement wording.

    The paper-overlap rule alone never fires for a paper whose claims all landed
    on one route, which is exactly where a shared claim is most likely to be
    missed. This adds a deterministic fallback so every routed claim gets at
    least one second look.
    """
    by_code = {str(root.get("code")): tokens(str(root.get("statement") or ""))
               for root in roots}
    closest: dict[str, str] = {}
    for code, statement in by_code.items():
        best, best_score = "", 0.0
        for other, other_statement in by_code.items():
            if other == code:
                continue
            score = len(statement & other_statement) / max(1, len(statement | other_statement))
            if score > best_score or (score == best_score and best and other < best):
                best, best_score = other, score
        if best and best_score > 0:
            closest[code] = best
    return closest


def _paper_owners(view: Mapping[str, Any],
                  decisions: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    """Which routes already own a claim from the same paper."""
    by_id = {str(node.get("id")): node for node in view.get("nodes") or []}
    owned: dict[str, set[str]] = defaultdict(set)
    for (code, claim_id) in decisions:
        node = by_id.get(str(claim_id))
        if node is None:
            continue
        for paper in papers_of_node(node):
            owned[paper].add(code)
    return owned


def link_roots_routed(
    view: Mapping[str, Any],
    question: Mapping[str, str],
    roots: list[Mapping[str, Any]],
    *,
    call_json: Callable[[str], Any] = _post_json,
    multi_route: bool = True,
    workers: int | None = None,
    pool_limit: int = LINK_POOL_LIMIT,
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, Any]]]:
    """Route every pooled claim to the candidate it bears on.

    One call per claim instead of one per candidate, and no link budget: the
    claim decides, and a claim that fits nothing answers with an empty list.
    """
    codes = [str(root.get("code")) for root in roots]
    pools: dict[str, dict[str, Mapping[str, Any]]] = {}
    for root in roots:
        items = candidate_pool(view, root, limit=pool_limit)
        pools[str(root.get("code"))] = {str(item["id"]): item for item in items}
    claim_ids = list(dict.fromkeys(
        claim_id for code in codes for claim_id in pools.get(code, {})
    ))
    if not claim_ids:
        return {code: [] for code in codes}, []

    by_id = {str(node.get("id")): node for node in view.get("nodes") or []}
    claims: dict[str, dict[str, Any]] = {}
    for claim_id in claim_ids:
        node = by_id.get(claim_id)
        if node is None:
            continue
        claims[claim_id] = {"id": claim_id, "code": node_code(node),
                            "content": short_text(node_text(node), 300)}

    allowed = set(codes)
    decisions: dict[tuple[str, str], dict[str, str]] = {}
    calls: list[dict[str, Any]] = []
    worker_count = link_workers(workers)

    def route(claim_id: str) -> tuple[str, dict[str, dict[str, str]]]:
        prompt = build_routed_prompt(question, claims[claim_id], roots)
        return claim_id, _parse_routed(call_json(prompt), allowed)

    if worker_count > 1 and len(claims) > 1:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {pool.submit(route, claim_id): claim_id for claim_id in claims}
            for future in as_completed(futures):
                routed_id, picked = future.result()
                for code, value in picked.items():
                    decisions[(code, routed_id)] = value
    else:
        for claim_id in claims:
            routed_id, picked = route(claim_id)
            for code, value in picked.items():
                decisions[(code, routed_id)] = value
    assigned: Counter[str] = Counter(claim_id for (_, claim_id) in decisions)
    supported: Counter[str] = Counter(
        claim_id for (_, claim_id), value in decisions.items()
        if value["relation"] == "support"
    )
    calls.append({"stage": "route", "claims": len(claims),
                  "workers": worker_count, "decisions": len(decisions),
                  "claims_routed_to_several": sum(1 for n in assigned.values() if n > 1),
                  "claims_supporting_several": sum(1 for n in supported.values() if n > 1),
                  "claims_routed_to_nothing": len(claims) - len(assigned)})

    if multi_route and decisions:
        # A routed claim lands on one route by construction. Ask the neighbouring
        # routes separately, or evidence two routes genuinely share is dropped.
        owned = _paper_owners(view, decisions)
        closest = _closest_route(roots)
        extra: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for (code, claim_id) in decisions:
            node = by_id.get(str(claim_id))
            if node is None:
                continue
            neighbours: set[str] = set()
            for paper in papers_of_node(node):
                neighbours |= owned.get(paper, set())
            fallback = closest.get(code)
            if fallback:
                neighbours.add(fallback)
            for candidate_code in sorted(neighbours - {code}):
                pair = (candidate_code, claim_id)
                if pair not in decisions and pair not in seen:
                    seen.add(pair)
                    extra.append(pair)
        extra = extra[: len(decisions) * SECOND_OPINION_LIMIT]
        root_by_code = {str(root.get("code")): root for root in roots}

        def second_opinion(pair: tuple[str, str]
                           ) -> tuple[tuple[str, str], dict[str, str] | None]:
            code, claim_id = pair
            prompt = build_second_opinion_prompt(question, claims[claim_id],
                                                 root_by_code[code])
            return pair, _parse_second_opinion(call_json(prompt))

        accepted = 0
        if extra:
            if worker_count > 1 and len(extra) > 1:
                with ThreadPoolExecutor(max_workers=worker_count) as pool:
                    futures = {pool.submit(second_opinion, pair): pair for pair in extra}
                    for future in as_completed(futures):
                        pair, value = future.result()
                        if value is not None:
                            decisions[pair] = value
                            accepted += 1
            else:
                for pair in extra:
                    pair, value = second_opinion(pair)
                    if value is not None:
                        decisions[pair] = value
                        accepted += 1
            calls.append({"stage": "second_opinion", "asked": len(extra),
                          "accepted": accepted})

    links_by_root: dict[str, list[dict[str, str]]] = {code: [] for code in codes}
    for (code, claim_id), value in sorted(decisions.items()):
        links_by_root.setdefault(code, []).append({
            "claim_id": claim_id,
            "relation": value["relation"],
            "reason": value["reason"],
        })
    return links_by_root, calls


# ---------------------------------------------------------------------------
# Answer layer construction
# ---------------------------------------------------------------------------
def enumerate_chains(
    up: Mapping[str, list[tuple[str, str]]],
    start: str,
    *,
    seen: set[str] | None = None,
    depth: int = 0,
) -> list[tuple[list[str], list[str]]]:
    """Return leaf-to-start paths through directed infer edges.

    The return value is a list of ``(nodes, relations)`` where ``nodes`` runs
    from the leaf premise to ``start`` and ``relations[i]`` is the relation that
    introduced ``nodes[i]`` (the first entry is empty).
    """
    seen = set(seen or set())
    if start in seen or depth > _MAX_CHAIN_DEPTH:
        return []
    parents = [(premise, rel) for premise, rel in up.get(start, []) if premise not in seen]
    if not parents:
        return [([start], [""])]
    result: list[tuple[list[str], list[str]]] = []
    next_seen = seen | {start}
    for premise, rel in parents:
        for nodes, rels in enumerate_chains(up, premise, seen=next_seen, depth=depth + 1):
            result.append((nodes + [start], rels + [rel]))
            if len(result) > _MAX_CHAINS_PER_ROOT * 4:
                return result
    return result


def _root_chains(
    root: Mapping[str, Any],
    links: list[Mapping[str, str]],
    up: Mapping[str, list[tuple[str, str]]],
    nodes_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, ...]] = set()
    for link in links:
        if link["relation"] != "support":
            continue
        claim_id = str(link["claim_id"])
        for chain_nodes, chain_rels in enumerate_chains(up, claim_id):
            path = chain_nodes + [root["id"]]
            rels = chain_rels + ["support"]
            key = tuple(path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            leaf = path[0]
            leaf_node = nodes_by_id.get(leaf) or {}
            paths.append({
                "path": path,
                "rels": rels,
                "leaf": leaf,
                "leaf_code": node_code(leaf_node) if leaf_node else leaf,
                "depth": len(path),
            })
            if len(paths) >= _MAX_CHAINS_PER_ROOT:
                break
        if len(paths) >= _MAX_CHAINS_PER_ROOT:
            break
    paths.sort(key=lambda item: (-item["depth"], item["leaf_code"]))
    return paths


def annotate_chain(
    chain: Mapping[str, Any],
    root: Mapping[str, Any],
    edge_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
    roles: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Attach per-step reasoning types to a path.

    The last step is the answer edge (claim -> root) and is labelled ``support``.
    A path is ``deduction`` only if every graph step is a chainable deduction.
    """
    path = [str(item) for item in chain.get("path") or []]
    types: list[str] = [""] * len(path)
    edge_ids: list[str | None] = []
    for index in range(len(path) - 1):
        source, target = path[index], path[index + 1]
        if index == len(path) - 2 and target == str(root.get("id")):
            types[index + 1] = "support"
            edge_ids.append(None)
            continue
        edge = edge_lookup.get((source, target))
        role = roles.get(str(edge.get("id"))) if edge else None
        types[index + 1] = str(role.get("reasoning_type") or "infer") if role else "infer"
        edge_ids.append(str(edge.get("id")) if edge else None)
    out = dict(chain)
    out["types"] = types
    out["edge_ids"] = edge_ids
    out["typed_steps"] = sum(1 for item in types[1:] if item in {"deduction", "reproduces"})
    out["chain_type"] = (
        "deduction" if all(item in {"deduction", "support"} for item in types[1:]) else "mixed"
    )
    return out


def build_answer_layer(
    view: Mapping[str, Any],
    question: Mapping[str, str],
    roots: list[Mapping[str, Any]],
    links_by_root: Mapping[str, list[Mapping[str, str]]],
    edge_roles: Any = None,
) -> dict[str, Any]:
    nodes = list(view.get("nodes") or [])
    edges = list(view.get("edges") or [])
    nodes_by_id = {str(node.get("id")): node for node in nodes}
    roles = normalize_edge_roles(edge_roles)
    if not roles:
        # A native typed view already carries the role on each edge; use it when
        # no external audit sidecar was supplied (full pipeline runs).
        for edge in edges:
            details = edge.get("details") or {}
            reasoning_type = edge.get("reasoning_type") or details.get("reasoning_type")
            if not reasoning_type:
                continue
            roles[str(edge.get("id"))] = {
                "edge_id": str(edge.get("id")),
                "reasoning_type": str(reasoning_type),
                "residual_reason": edge.get("residual_reason") or details.get("residual_reason"),
                "chainable": bool(edge.get("chainable") or details.get("chainable")),
                "reason": edge.get("role_reason") or details.get("role_reason") or "",
            }
    up_chain = chain_up_adjacency(edges, roles)
    up_all = infer_up_adjacency(edges)
    edge_lookup = {
        (str(edge.get("source")), str(edge.get("target"))): edge
        for edge in edges
    }

    built_roots: list[dict[str, Any]] = []
    answer_edges: list[dict[str, Any]] = []
    total_chains = 0
    total_exploratory = 0
    for root in roots:
        links = list(links_by_root.get(root["code"]) or [])
        supported = [link for link in links if link["relation"] == "support"]
        boundaries = [link for link in links if link["relation"] == "bounds"]
        opposes = [link for link in links if link["relation"] == "oppose"]
        if not supported:
            continue  # an isolated root would be a floating assertion, not a route

        chains = [
            annotate_chain(chain, root, edge_lookup, roles)
            for chain in _root_chains(root, links, up_chain, nodes_by_id)
        ]
        # A depth-2 path is just the direct answer link; it is already counted in
        # support_ids.  A support chain must contain at least one graph step.
        chains = [
            chain for chain in chains
            if chain.get("chain_type") == "deduction" and chain.get("depth", 0) >= 3
        ]
        valid_keys = {tuple(chain["path"]) for chain in chains}
        exploratory = [
            annotate_chain(chain, root, edge_lookup, roles)
            for chain in _root_chains(root, links, up_all, nodes_by_id)
            if chain.get("depth", 0) >= 3 and tuple(chain["path"]) not in valid_keys
        ]
        total_chains += len(chains)
        total_exploratory += len(exploratory)

        paper_support: Counter[str] = Counter()
        for link in supported:
            for paper in papers_of_node(nodes_by_id.get(str(link["claim_id"]), {})):
                paper_support[paper] += 1
        support_papers = sorted(paper_support)
        # How much the second-largest supporting paper contributes.  A second
        # paper that supplies one claim out of ten does not corroborate the
        # root; labelling that "multi-paper direct support" overstates it.
        ranked_support = sorted(paper_support.values(), reverse=True)
        second_paper_support = ranked_support[1] if len(ranked_support) > 1 else 0
        support_claims = [str(link["claim_id"]) for link in supported]
        leaves = {chain["leaf"] for chain in chains}
        depth = max((chain["depth"] for chain in chains), default=0)
        exploratory_leaves = {chain["leaf"] for chain in exploratory}
        exploratory_depth = max((chain["depth"] for chain in exploratory), default=0)
        dominant_paper_support = ranked_support[0] if ranked_support else 0
        second_paper_share = (
            round(second_paper_support / dominant_paper_support, 3)
            if dominant_paper_support else 0.0
        )
        if opposes:
            verdict_class = "contested"
        elif (len(support_papers) >= 2 and second_paper_support >= 2
              and second_paper_share >= VERDICT_DOMINANCE_FLOOR):
            verdict_class = "primary"
        elif supported and support_papers:
            verdict_class = "partial"
        else:
            verdict_class = "exploratory"
        built = dict(root)
        built.update({
            "id": root["id"],
            "verdict_class": verdict_class,
            "verdict": VERDICT_LABELS[verdict_class],
            "color": VERDICT_COLORS[verdict_class],
            "caveat": ("Candidate answer generated from the merged claim graph; support counts are "
                       "structural links, not a truth vote. Read the linked claims before relying on it."),
            "stats": {
                "support_claim_count": len(supported),
                "support_paper_count": len(support_papers),
                "support_papers": support_papers,
                "support_paper_counts": dict(sorted(
                    paper_support.items(), key=lambda item: item[1], reverse=True
                )),
                "second_paper_support": second_paper_support,
                "dominant_paper_support": dominant_paper_support,
                "second_paper_share": second_paper_share,
                "multi_paper_corroborated": (
                    len(support_papers) >= 2 and second_paper_support >= 2
                ),
                "chain_count": len(chains),
                "leaf_count": len(leaves),
                "max_depth": depth,
                "chain_status": "typed" if chains else "no_typed_chain",
                "exploratory_path_count": len(exploratory),
                "exploratory_leaf_count": len(exploratory_leaves),
                "exploratory_max_depth": exploratory_depth,
                "deduction_step_count": sum(1 for chain in chains for item in chain["types"] if item == "deduction"),
                "boundary_count": len(boundaries),
                "oppose_count": len(opposes),
            },
            "support_ids": support_claims,
            "oppose_ids": [str(link["claim_id"]) for link in opposes],
            "boundary_ids": [str(link["claim_id"]) for link in boundaries],
            "links": links,
            "chains": chains,
            "exploratory_paths": exploratory,
        })
        built_roots.append(built)
        for link in links:
            answer_edges.append({
                "id": f"answer:{root['code'].lower()}:{link['claim_id']}",
                "source": str(link["claim_id"]),
                "target": root["id"],
                "label": LINK_LABELS[link["relation"]],
                "semantic_type": link["relation"],
                "edge_class": "answer",
                "layer": "answer",
                "step": 6,
                "visible_at": ["overview", "standard"],
                "min_granularity": "overview",
                "details": {
                    "relation": link["relation"],
                    "reason": link["reason"],
                    "root_id": root["id"],
                    "source": str(link["claim_id"]),
                    "target": root["id"],
                },
            })
    if not built_roots:
        raise ValueError("no candidate answer received a defensible support link")

    role_list = sorted(roles.values(), key=lambda item: str(item.get("edge_id")))
    role_counts = Counter(str(item.get("reasoning_type") or "unknown") for item in role_list)
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "question": dict(question),
        "root_ids": [root["id"] for root in built_roots],
        "bridge_ids": [],
        "axiom_ids": [],
        "roots": built_roots,
        "answer_edges": answer_edges,
        "edge_roles": role_list,
        "edge_role_counts": dict(sorted(role_counts.items())),
        "chainable_edge_count": sum(1 for item in role_list if item.get("chainable")),
        "generation": {
            "prompt_version": PROMPT_VERSION,
            "model": os.environ.get("DEEPSEEK_MERGE_MODEL", "deepseek-v4-flash"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "root_count": len(built_roots),
            "answer_edge_count": len(answer_edges),
            "chain_count": total_chains,
            "exploratory_path_count": total_exploratory,
            "edge_role_count": len(role_list),
            "base_node_count": len(nodes),
            "base_edge_count": len(edges),
        },
    }


def inject_answer_layer(view: Mapping[str, Any], answer_layer: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(view))
    nodes = result.setdefault("nodes", [])
    edges = result.setdefault("edges", [])
    search_documents = result.setdefault("search_documents", [])
    # Attach the audit's reasoning type to the *base* edges so the viewer can
    # distinguish deduction / special_case / analogy / residual infer without
    # another lookup table.
    roles = normalize_edge_roles(answer_layer.get("edge_roles"))
    for edge in edges:
        role = roles.get(str(edge.get("id")))
        if not role:
            continue
        edge["reasoning_type"] = role.get("reasoning_type")
        edge["residual_reason"] = role.get("residual_reason")
        edge["chainable"] = bool(role.get("chainable"))
        edge["role_reason"] = role.get("reason")
        details = dict(edge.get("details") or {})
        details.update({
            "reasoning_type": role.get("reasoning_type"),
            "residual_reason": role.get("residual_reason"),
            "chainable": bool(role.get("chainable")),
            "role_reason": role.get("reason"),
        })
        edge["details"] = details
    existing = {str(node.get("id")) for node in nodes}
    for root in answer_layer.get("roots") or []:
        root_id = str(root["id"])
        if root_id in existing:
            continue
        existing.add(root_id)
        nodes.append({
            "id": root_id,
            "entity_id": root["code"],
            "label": root["code"],
            "display_label": root["code"],
            "display_meta": "answer_root",
            "kind": "answer_root",
            "layer": "answer",
            "step": 6,
            "visible_at": ["overview", "standard"],
            "min_granularity": "overview",
            "summary": root["statement"],
            "source_anchor_ids": [],
            "details": {
                "id": root_id,
                "qid": root_id,
                "type": "answer_root",
                "content": root["statement"],
                "title_zh": root.get("title_zh", ""),
                "title_en": root.get("title_en", ""),
                "answer_type": root.get("answer_type", ""),
                "scope": root.get("scope", ""),
                "criteria": root.get("criteria", ""),
                "verdict": root.get("verdict", ""),
                "verdict_class": root.get("verdict_class", ""),
                "caveat": root.get("caveat", ""),
                "stats": root.get("stats", {}),
                "integration_owned": True,
                "metadata": {"source": "question_answer_layer"},
            },
        })
        search_documents.append({
            "id": f"search:{root_id}",
            "title": root["code"],
            "text": " ".join(str(root.get(key) or "") for key in ("title_zh", "title_en", "statement", "criteria")),
            "tags": ["answer_root", "answer"],
            "refs": [root_id],
            "metadata": {"step": 6},
        })
    edge_ids = {str(edge.get("id")) for edge in edges}
    for edge in answer_layer.get("answer_edges") or []:
        if str(edge.get("id")) in edge_ids:
            continue
        edges.append(dict(edge))
        edge_ids.add(str(edge.get("id")))
    result["answer_layer"] = dict(answer_layer)
    metadata = dict(result.get("metadata") or {})
    linked = {str(edge.get("source")) for edge in answer_layer.get("answer_edges") or []}
    root_ids = {str(root.get("id")) for root in answer_layer.get("roots") or []}
    chain_nodes = {
        str(node_id)
        for root in answer_layer.get("roots") or []
        for chain in root.get("chains") or []
        for node_id in chain.get("path") or []
        if str(node_id) not in root_ids
    }
    exploratory_nodes = {
        str(node_id)
        for root in answer_layer.get("roots") or []
        for chain in root.get("exploratory_paths") or []
        for node_id in chain.get("path") or []
        if str(node_id) not in root_ids
    }
    metadata.update({
        "answer_question": answer_layer.get("question", {}),
        "answer_root_count": len(answer_layer.get("roots") or []),
        "answer_edge_count": len(answer_layer.get("answer_edges") or []),
        "answer_linked_claim_count": len(linked),
        "answer_chain_node_count": len(chain_nodes),
        "answer_chain_count": sum(len(root.get("chains") or []) for root in answer_layer.get("roots") or []),
        "answer_exploratory_path_count": sum(
            len(root.get("exploratory_paths") or []) for root in answer_layer.get("roots") or []
        ),
        "answer_exploratory_node_count": len(exploratory_nodes),
        "answer_edge_role_counts": answer_layer.get("edge_role_counts", {}),
        "answer_chainable_edge_count": answer_layer.get("chainable_edge_count", 0),
    })
    result["metadata"] = metadata
    layers = list(result.get("layers") or [])
    if not any(layer.get("id") == "answer" for layer in layers):
        layers.insert(0, {"id": "answer", "label": "Candidate answers (roots)", "default_visible": True})
    result["layers"] = layers
    return result


def evaluate(view: Mapping[str, Any], answer_layer: Mapping[str, Any]) -> dict[str, Any]:
    nodes = list(view.get("nodes") or [])
    edges = list(view.get("edges") or [])
    by_id = {str(node.get("id")): node for node in nodes}
    degree = degree_map(edges)
    connected = {str(node.get("id")) for node in nodes if degree[str(node.get("id"))] > 0}
    chain_nodes: set[str] = set()
    exploratory_nodes: set[str] = set()
    linked: set[str] = set()
    per_root = []
    for root in answer_layer.get("roots") or []:
        support = set(str(item) for item in root.get("support_ids") or [])
        boundaries = set(str(item) for item in root.get("boundary_ids") or [])
        opposes = set(str(item) for item in root.get("oppose_ids") or [])
        chains = root.get("chains") or []
        exploratory = root.get("exploratory_paths") or []
        root_chain_nodes = {
            str(nid) for chain in chains for nid in chain.get("path") or []
            if str(nid) != str(root.get("id"))
        }
        root_exploratory_nodes = {
            str(nid) for chain in exploratory for nid in chain.get("path") or []
            if str(nid) != str(root.get("id"))
        }
        chain_nodes |= root_chain_nodes
        exploratory_nodes |= root_exploratory_nodes
        linked |= support | boundaries | opposes
        per_root.append({
            "code": root.get("code"),
            "title_zh": root.get("title_zh"),
            "title_en": root.get("title_en"),
            "verdict_class": root.get("verdict_class"),
            "verdict": root.get("verdict"),
            "support_claim_count": len(support),
            "support_paper_count": len(root.get("stats", {}).get("support_papers") or []),
            "support_papers": root.get("stats", {}).get("support_papers") or [],
            "support_paper_counts": root.get("stats", {}).get("support_paper_counts") or {},
            "second_paper_support": root.get("stats", {}).get("second_paper_support", 0),
            "boundary_count": len(boundaries),
            "oppose_count": len(opposes),
            "chain_count": len(chains),
            "chain_status": (root.get("stats") or {}).get("chain_status", "no_typed_chain"),
            "typed_step_count": sum(
                1 for chain in chains for item in chain.get("types") or [] if item == "deduction"
            ),
            "exploratory_path_count": len(exploratory),
            "exploratory_node_count": len(root_exploratory_nodes),
            "leaf_count": len({chain.get("leaf") for chain in chains}),
            "max_depth": max((int(chain.get("depth") or 0) for chain in chains), default=0),
            "chain_node_count": len(root_chain_nodes),
            "direct_ids": sorted(support | boundaries | opposes),
            "statement": root.get("statement"),
            "seed_overlap": root.get("seed_overlap"),
        })
    linked_or_chained = linked | chain_nodes
    linked_or_chained_with_exploratory = linked_or_chained | exploratory_nodes
    count_by_paper: dict[str, int] = Counter()
    touched_papers: set[str] = set()
    for node_id in linked_or_chained:
        if node_id in by_id:
            papers = papers_of_node(by_id[node_id])
            touched_papers.update(papers)
            count_by_paper.update(papers)
    multi_root = Counter()
    for root in answer_layer.get("roots") or []:
        for node_id in set(root.get("support_ids") or []):
            multi_root[node_id] += 1
    return {
        "question": answer_layer.get("question"),
        "base": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "connected_claim_count": len(connected),
            "paper_count": len({paper for node in nodes for paper in papers_of_node(node)}),
        },
        "answer_layer": {
            "root_count": len(answer_layer.get("roots") or []),
            "answer_edge_count": len(answer_layer.get("answer_edges") or []),
            "linked_claim_count": len(linked),
            "chain_node_count": len(chain_nodes),
            "exploratory_node_count": len(exploratory_nodes),
            "exploratory_path_count": sum(
                len(root.get("exploratory_paths") or []) for root in answer_layer.get("roots") or []
            ),
            "linked_or_chained_claim_count": len(linked_or_chained),
            "linked_or_chained_with_exploratory_count": len(linked_or_chained_with_exploratory),
            "coverage_of_connected_claims": round(len(linked_or_chained & connected) / max(1, len(connected)), 4),
            "coverage_with_exploratory": round(
                len(linked_or_chained_with_exploratory & connected) / max(1, len(connected)), 4
            ),
            "coverage_of_all_claims": round(len(linked_or_chained) / max(1, len(nodes)), 4),
            "papers_touched": len(touched_papers),
            "claims_on_multiple_roots": sum(1 for value in multi_root.values() if value > 1),
            "claims_per_paper": dict(sorted(count_by_paper.items())),
            "edge_role_counts": answer_layer.get("edge_role_counts", {}),
            "chainable_edge_count": answer_layer.get("chainable_edge_count", 0),
        },
        "roots": per_root,
    }


def evaluation_markdown(result: Mapping[str, Any], answer_layer: Mapping[str, Any]) -> str:
    question = result.get("question") or {}
    base = result.get("base") or {}
    layer = result.get("answer_layer") or {}
    lines = [
        "# Question-led answer layer: evaluation",
        "",
        f"- Question (zh): {question.get('zh', '')}",
        f"- Question (en): {question.get('en', '')}",
        f"- Base graph: {base.get('node_count')} claims, {base.get('edge_count')} edges, "
        f"{base.get('paper_count')} papers, {base.get('connected_claim_count')} connected claims",
        f"- Candidate answers: {layer.get('root_count')} roots, {layer.get('answer_edge_count')} answer edges",
        f"- Linked / chained claims: {layer.get('linked_or_chained_claim_count')} "
        f"({layer.get('coverage_of_connected_claims', 0) * 100:.1f}% of connected claims, "
        f"{layer.get('coverage_of_all_claims', 0) * 100:.1f}% of all claims)",
        f"- Papers touched: {layer.get('papers_touched')}",
        f"- Claims supporting more than one answer: {layer.get('claims_on_multiple_roots')}",
        f"- Chainable edges: {layer.get('chainable_edge_count', 0)}; "
        f"edge role counts: {layer.get('edge_role_counts', {})}",
        f"- Typed support-chain coverage: {layer.get('linked_or_chained_claim_count')} claims; "
        f"with exploratory paths: {layer.get('linked_or_chained_with_exploratory_count')} "
        f"({layer.get('coverage_with_exploratory', 0) * 100:.1f}% of connected)",
        "",
        "## Candidate answers",
        "",
    ]
    for root in result.get("roots") or []:
        counts = root.get("support_paper_counts") or {}
        distribution = " / ".join(str(value) for value in counts.values())
        lines.extend([
            f"### {root.get('code')} · {root.get('title_zh')} / {root.get('title_en')}",
            "",
            f"- Verdict: {root.get('verdict_class')} — {root.get('verdict')}",
            f"- Support: {root.get('support_claim_count')} claims from "
            f"{root.get('support_paper_count')} papers"
            + (f" ({distribution}, so the second paper contributes "
               f"{root.get('second_paper_support', 0)}); " if distribution else "; ")
            + f"typed chains: {root.get('chain_count')} "
            f"({root.get('chain_status')}; leaves {root.get('leaf_count')}, max depth {root.get('max_depth')})",
            f"- Exploratory (non-evidential) paths: {root.get('exploratory_path_count')} "
            f"({root.get('exploratory_node_count')} nodes)",
            f"- Boundary: {root.get('boundary_count')} · Oppose: {root.get('oppose_count')}",
            f"- Chain nodes: {root.get('chain_node_count')}",
        ])
        seed_overlap = root.get("seed_overlap") or {}
        if seed_overlap.get("seeds_measured"):
            lines.append(
                f"- Seed agreement: {seed_overlap.get('seeds_with_overlap')}/"
                f"{seed_overlap.get('seeds_measured')} seeds share terms with the "
                f"statement (mean {seed_overlap.get('mean')})"
            )
        lines.extend([f"- Statement: {root.get('statement')}", ""])
    lines.extend([
        "## Reading the numbers",
        "",
        "- Coverage counts claims that are direct answer links *or* nodes on a **typed** support chain. "
        "A claim can appear in several roots when the routes overlap; overlap is reported, not hidden.",
        "- Only `deduction` edges are chainable. `special_case`, `reproduces`, `bounds`, `abduction`, "
        "`analogy` and residual `infer` are shown but do not form support chains; `infer` with "
        "`residual_reason` is explicitly non-chainable.",
        "- `exploratory_paths` are the old infer-based graph paths. They are useful for exploration but "
        "are **not counted** in direct support, typed chains, or coverage.",
        "- `support` / `oppose` / `bounds` are LLM-judged links that were mechanically checked against "
        "the frozen candidate set. They are not a proof and not a vote count.",
        "- This run starts from a claim-level graph without domain conclusions. In a full pipeline run "
        "with `summarize_conclusions: true`, the same layer can link answers to K conclusions first, "
        "then chains descend from K to claims.",
        "- Root verdicts are structural: multi-paper support, single-paper support, opposed, or exploratory.",
        "",
    ])
    return "\n".join(lines)


__all__ = [
    "SCHEMA_NAME", "SCHEMA_VERSION", "PROMPT_VERSION", "VERDICT_COLORS",
    "build_digest", "generate_roots", "candidate_pool", "link_roots",
    "normalize_edge_roles", "chain_up_adjacency", "annotate_chain",
    "build_answer_layer", "inject_answer_layer", "evaluate", "evaluation_markdown",
]
