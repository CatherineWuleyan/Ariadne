"""Attach one new candidate answer to an already-built graph, on demand.

`ask` builds a graph and answers a question from it. This module does the
opposite direction: given a graph that already exists (a frozen view), decide
whether the claims already in it bear on a candidate the graph was not built
for -- for example an experiment you are considering running.

It never mutates the input view and it never calls LKM. That is deliberate:

* the graph is evidence, and attaching a candidate must not silently rewrite it;
* a "no support" answer from a frozen graph means "no support among these
  claims", which is not the same as "no support in the literature". The report
  says ``searched: false`` and counts how many claims were actually offered to
  the judge, so the two readings cannot be confused.

Escalation is a separate, explicit step: run ``ariadne ask`` for the missing
material, then run this again against the enlarged graph.
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import Config, Runtime, resolve_runtime
from .util import AriadneError, REPO, read_json, write_json

SCHEMA = "ariadne.support"
SCHEMA_VERSION = "0.1.0"
STATUS_SUPPORTED = "supported"
STATUS_CONTRADICTED = "contradicted"
STATUS_UNRESOLVED = "unresolved_in_graph"


def native_imports() -> tuple[Any, Any]:
    """Import the shared question layer from the maintained pipeline."""
    for extra in (REPO, REPO / "agent", REPO / "pipelines/single_paper", REPO / "pipelines/merge"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    from pipeline_merge_question import question_layer, step3

    return question_layer, step3


def build_candidate(
    statement: str,
    *,
    code: str = "C1",
    title_zh: str = "",
    title_en: str = "",
    scope: str = "",
    criteria: str = "",
    answer_type: str = "method_route",
    seed_claim_ids: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """The root shape ``question_layer`` expects, built from a plain statement."""
    statement = statement.strip()
    if not statement:
        raise AriadneError("candidate statement is empty")
    return {
        "id": "candidate:" + code.lower(),
        "code": code,
        "title_zh": title_zh or statement[:40],
        "title_en": title_en or statement[:80],
        "statement": statement,
        "scope": scope,
        "criteria": criteria,
        "answer_type": answer_type,
        "seed_claim_ids": [str(item) for item in seed_claim_ids],
    }


def _status(links: Mapping[str, list[dict[str, str]]]) -> str:
    if links.get("support"):
        return STATUS_SUPPORTED
    if links.get("oppose"):
        return STATUS_CONTRADICTED
    return STATUS_UNRESOLVED


MAX_EXPAND_PAPERS = 6
MAX_EXPAND_NODES_PER_PAPER = 24
SPEND_PER_CALL_CNY = 0.05


def _require_spend(runtime: Runtime, yes: bool, calls: int, detail: str) -> None:
    if yes or getattr(runtime, "auto_approve_spend", False):
        return
    raise AriadneError(
        f"expansion makes up to {calls} billable LKM calls (~{calls * SPEND_PER_CALL_CNY:.2f} CNY): "
        f"{detail}. Re-run with --yes, or drop --expand to judge the frozen graph only."
    )


def expand(view: Mapping[str, Any], candidate: Mapping[str, Any], *,
           runtime: Runtime, run_dir: Path, max_papers: int,
           query: str | None = None, yes: bool = False
           ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retrieve papers about the candidate and turn their graph nodes into evidence.

    Only graphs are fetched -- paper2ir, single and merge are not run. That keeps
    expansion cheap enough to be a "look before you pay" step: the extra nodes are
    raw LKM graph nodes, marked ``origin: lkm_expand``, not processed claims.
    """
    from . import retrieval as retrieval_module

    max_papers = max(1, min(int(max_papers), MAX_EXPAND_PAPERS))
    query = (query or str(candidate.get("statement") or "")).strip()
    if not query:
        raise AriadneError("expansion needs a query or a candidate statement")
    # One search plus at most one graph call per paper.
    _require_spend(runtime, yes, 1 + max_papers,
                   f"1 LKM search + up to {max_papers} graph fetch(es)")

    run_dir.mkdir(parents=True, exist_ok=True)
    records = retrieval_module.search(query, max_papers, run_dir, runtime)

    overlay: list[dict[str, Any]] = []
    papers: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for record in records[:max_papers]:
        pid = str(record.get("lkm_id") or record.get("id") or "")
        if not pid:
            continue
        dest = run_dir / "probe" / f"{pid}.json"
        try:
            _, graph = retrieval_module._fetch_graph(record, dest, run_dir, runtime)
        except Exception as exc:  # noqa: BLE001 - one paper must not stop expansion
            skipped.append({"lkm_id": pid, "reason": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        nodes = _graph_nodes(graph)
        if not nodes:
            skipped.append({"lkm_id": pid, "reason": "graph has no nodes"})
            continue
        # Conclusions first: they are the statements that can bear on a candidate.
        nodes.sort(key=lambda node: (
            0 if str(node.get("kind") or "").lower() == "conclusion" else 1,
            -len(str(node.get("content") or "")),
        ))
        added = 0
        for node in nodes[:MAX_EXPAND_NODES_PER_PAPER]:
            text = str(node.get("content") or "").strip()
            if len(text) < 40:
                continue
            overlay.append(_overlay_node(pid, record, node, text))
            added += 1
        papers.append({
            "lkm_id": pid,
            "title": str(record.get("title") or "")[:160],
            "nodes_added": added,
            "graph": str(dest),
        })

    merged = copy.deepcopy(dict(view))
    merged["nodes"] = list(merged.get("nodes") or []) + overlay
    merged.setdefault("edges", [])
    audit = {
        "query": query,
        "papers_requested": max_papers,
        "papers_used": len(papers),
        "nodes_added": len(overlay),
        "papers": papers,
        "skipped": skipped,
        "lkm_calls": 1 + len([p for p in papers]) + len(skipped),
        "estimated_cost_cny": round(SPEND_PER_CALL_CNY * (1 + len(papers) + len(skipped)), 2),
        "note": "raw LKM graph nodes, not processed claims: no paper2ir, single or merge ran",
    }
    return merged, audit


def _graph_nodes(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    items = (graph.get("data") or {}).get("papers") or []
    for item in items:
        if isinstance(item, Mapping):
            nodes.extend(item.get("graph", {}).get("nodes") or [])
    return [node for node in nodes if isinstance(node, Mapping)]


def _overlay_node(pid: str, record: Mapping[str, Any], node: Mapping[str, Any],
                  text: str) -> dict[str, Any]:
    node_id = str(node.get("id") or node.get("global_id") or "")
    return {
        "id": f"knowledge:papers:expand_{pid}::{node_id or 'node'}",
        "kind": "claim",
        "layer": "claims",
        "summary": text[:400],
        "entity_id": node_id,
        "details": {
            "content": text,
            "origin": "lkm_expand",
            "lkm_node_id": node_id,
            "lkm_kind": str(node.get("kind") or ""),
            "paper_title": str(record.get("title") or "")[:200],
        },
    }


def run_support(
    view_path: Path,
    candidate: Mapping[str, Any],
    *,
    question: Mapping[str, str] | None = None,
    out: Path | None = None,
    runtime: Runtime | None = None,
    call_json: Callable[[str], Any] | None = None,
    broaden_retry: bool = True,
    expand_with: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Link one candidate against a frozen view and write a report.

    ``call_json`` is injectable so the judging path can be tested without a
    model call.
    """
    view_path = Path(view_path)
    if not view_path.is_file():
        raise AriadneError(f"view not found: {view_path}")
    view = read_json(view_path)
    if not isinstance(view, Mapping) or not view.get("nodes"):
        raise AriadneError(f"not a view model: {view_path}")

    question_layer, _ = native_imports()
    if call_json is None:
        runtime = runtime or resolve_runtime(Config.load())
        runtime.apply_env()
        call_json = question_layer._post_json

    asked = {
        "zh": str((question or {}).get("zh") or candidate["statement"]),
        "en": str((question or {}).get("en") or candidate["statement"]),
    }
    working_view, expansion = view, None
    if expand_with is not None:
        runtime = runtime or resolve_runtime(Config.load())
        runtime.apply_env()
        working_view, expansion = expand(
            view, candidate, runtime=runtime,
            run_dir=Path(expand_with["run_dir"]),
            max_papers=int(expand_with.get("max_papers") or MAX_EXPAND_PAPERS),
            query=expand_with.get("query"), yes=bool(expand_with.get("yes")),
        )
    roots = [dict(candidate)]
    links_by_root, raw_calls = question_layer.link_roots(
        working_view, asked, roots, call_json=call_json,
        broaden_retry=bool(broaden_retry),
    )
    links = links_by_root.get(str(candidate["code"]), [])
    expanded_ids = {str(node.get("id")) for node in (working_view.get("nodes") or [])
                    if (node.get("details") or {}).get("origin") == "lkm_expand"}
    for link in links:
        link["origin"] = "retrieval" if link["claim_id"] in expanded_ids else "graph"

    # Build the answer layer on a copy: the frozen view on disk is evidence and
    # is never rewritten by attaching a candidate to it.
    working = copy.deepcopy(dict(working_view))
    try:
        layer = question_layer.build_answer_layer(
            working, asked, roots, {str(candidate["code"]): links}
        )
        root = (layer.get("roots") or [{}])[0]
        verdict = {
            "class": root.get("verdict_class"),
            "label": root.get("verdict"),
            "stats": root.get("stats"),
        }
    except ValueError:
        # The merge pipeline refuses to emit a root with no support link: an
        # isolated root would be a floating assertion. For this audit that
        # refusal *is* the answer, so report the links and say why there is no
        # verdict instead of turning "no support" into a crash.
        verdict = {
            "class": None,
            "label": (
                "no root emitted: the pipeline does not publish an answer without a "
                "support link"
            ),
            "stats": {
                "support_claim_count": 0,
                "support_paper_count": 0,
                "support_papers": [],
                "support_paper_counts": {},
                "second_paper_support": 0,
                "multi_paper_corroborated": False,
                "chain_count": 0,
                "chain_status": "no_typed_chain",
            },
            "note": "absence of support links in this view, not a failed judge call",
        }
    offered = raw_calls[0].get("candidates") if raw_calls else []
    status = _status({rel: [l for l in links if l["relation"] == rel]
                      for rel in ("support", "oppose", "bounds")})

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "candidate": dict(candidate),
        "question": asked,
        "scope": {
            "view": str(view_path.resolve()),
            "node_count": len(view.get("nodes") or []),
            "edge_count": len(view.get("edges") or []),
            "searched": bool(expansion),
            "retrieval": (
                expansion
                if expansion else
                "not attempted: only claims already in this view were judged"
            ),
            "nodes_judged": len(working_view.get("nodes") or []),
            "claims_offered_to_judge": len(offered or []),
            "broaden_retry": bool(broaden_retry),
        },
        "status": status,
        "links": {
            "support": [link for link in links if link["relation"] == "support"],
            "oppose": [link for link in links if link["relation"] == "oppose"],
            "bounds": [link for link in links if link["relation"] == "bounds"],
        },
        "verdict": verdict,
    }
    report["next"] = _next_step(report)

    if out is not None:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / "support.json", report)
        (out / "support.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def _next_step(report: Mapping[str, Any]) -> dict[str, Any]:
    status = report.get("status")
    if status == STATUS_SUPPORTED:
        return {
            "retrieval_recommended": False,
            "reason": "the existing graph already carries support for this candidate",
        }
    scope = report.get("scope") or {}
    retrieval = scope.get("retrieval")
    if scope.get("searched"):
        assert isinstance(retrieval, dict)
        return {
            "retrieval_recommended": False,
            "reason": (
                "searched LKM and judged "
                f"{scope.get('nodes_judged')} nodes "
                f"({retrieval.get('papers_used')} of {retrieval.get('papers_requested')} "
                "papers usable); nothing was judged to support the candidate."
            ),
            "how": (
                "either the candidate is not supported, or the search did not reach "
                "the right papers: raise --max-papers or run `ariadne ask` for the "
                "material it needs, then re-run `ariadne support`"
            ),
        }
    return {
        "retrieval_recommended": True,
        "reason": (
            "no claim in this view was judged to support the candidate. That is a "
            "statement about these "
            f"{scope.get('node_count')} nodes, not about the literature: no LKM "
            "search was performed."
        ),
        "how": (
            "re-run with `--expand N` to search LKM for related papers, or run "
            "`ariadne ask` for the material this candidate needs, then run "
            "`ariadne support` against the enlarged graph"
        ),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    candidate = report.get("candidate") or {}
    scope = report.get("scope") or {}
    links = report.get("links") or {}
    verdict = report.get("verdict") or {}
    stats = verdict.get("stats") or {}
    next_step = report.get("next") or {}
    lines = [
        f"# Candidate {candidate.get('code')} · support audit",
        "",
        f"> {candidate.get('statement')}",
        "",
        "## Result",
        "",
        f"- **status:** `{report.get('status')}`",
        f"- **verdict:** `{verdict.get('class') or 'none'}` — {verdict.get('label')}",
        f"- **support / oppose / bounds:** {len(links.get('support') or [])} / "
        f"{len(links.get('oppose') or [])} / {len(links.get('bounds') or [])}",
    ]
    if stats:
        counts = stats.get("support_paper_counts") or {}
        if counts:
            lines.append(
                "- **support by paper:** "
                + ", ".join(f"{paper}: {count}" for paper, count in counts.items())
            )
        lines.append(
            f"- **typed chains:** {stats.get('chain_count', 0)} "
            f"({stats.get('chain_status', 'no_typed_chain')})"
        )
    lines += [
        "",
        "## Scope",
        "",
        f"- view: `{scope.get('view')}`",
        f"- nodes / edges: {scope.get('node_count')} / {scope.get('edge_count')}",
        f"- **searched LKM: no** — {scope.get('retrieval')}",
        "- absence here is not absence in the literature; only the claims in this "
        "view were judged",
        f"- claims offered to the judge: {scope.get('claims_offered_to_judge')}",
        "",
    ]
    for relation, label in (("support", "Support"), ("oppose", "Oppose"), ("bounds", "Bounds")):
        items = links.get(relation) or []
        lines.append(f"## {label} ({len(items)})")
        lines.append("")
        if not items:
            lines.append("_none_")
            lines.append("")
            continue
        for item in items:
            origin = item.get("origin") or "graph"
            lines.append(f"- `{item['claim_id']}` ({origin}) — {item['reason']}")
        lines.append("")
    if verdict.get("note"):
        lines.append(f"- note: {verdict['note']}")
    retrieval = scope.get("retrieval")
    if isinstance(retrieval, dict):
        lines += [
            "## Expansion",
            "",
            f"- query: `{retrieval.get('query')}`",
            f"- papers used / requested: {retrieval.get('papers_used')} / "
            f"{retrieval.get('papers_requested')}",
            f"- nodes added: {retrieval.get('nodes_added')} "
            f"(cap {MAX_EXPAND_NODES_PER_PAPER} per paper)",
            f"- estimated LKM spend: {retrieval.get('estimated_cost_cny')} CNY",
            f"- {retrieval.get('note')}",
            "",
        ]
        for paper in retrieval.get("papers") or []:
            lines.append(f"  - `{paper['lkm_id']}` {paper['title']} "
                         f"(+{paper['nodes_added']} nodes)")
        for skipped in retrieval.get("skipped") or []:
            lines.append(f"  - skipped `{skipped['lkm_id']}`: {skipped['reason']}")
        lines.append("")
    lines += [
        "## Next",
        "",
        f"- retrieval recommended: **{next_step.get('retrieval_recommended')}**",
        f"- {next_step.get('reason')}",
    ]
    if next_step.get("how"):
        lines.append(f"- {next_step['how']}")
    lines.append("")
    return "\n".join(lines)
