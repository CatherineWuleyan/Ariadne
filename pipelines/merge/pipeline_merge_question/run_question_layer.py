#!/usr/bin/env python3
"""Run the question-led answer layer on an already-merged domain graph.

This is the standalone entry point used while Step 0-5 outputs are frozen.  It
takes the current ``domain_view_model.json`` (311 claims for the aircraft run),
generates candidate answers, links existing claims to them, and writes:

    answer_layer.json          roots + answer edges + chains + generation audit
    domain_view_question.json  the original view plus roots / answer edges
    domain_viewer_question.html  self-contained viewer (when the template exists)
    evaluation.json / .md      quantitative + qualitative evaluation
    llm_raw.json               root/link prompts and responses for audit

The same functions are called by Step 6 in a full pipeline run; only the input
(view model vs. Step 4/5 artifacts) differs.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for extra in (REPO, REPO / "agent", REPO / "pipelines" / "single_paper", REPO / "pipelines" / "merge"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from pipeline_merge_question import question_layer as ql  # noqa: E402
from pipeline_merge_question.step3 import _post_json  # noqa: E402

DEFAULT_VIEW = REPO / "outputs/aircraft/versions/03-reviewed-handcrafted-2026-10-14/source_snapshot/domain_view_model.json"
DEFAULT_OUT = REPO / "outputs/aircraft/final"
DEFAULT_EDGE_ROLES = REPO / "outputs/aircraft/final/edge_roles.json"
DEFAULT_QUESTION_ZH = "怎么完成飞机的仿真设计？"
DEFAULT_QUESTION_EN = "How can aircraft simulation design be carried out?"
DEFAULT_DOMAIN = "aircraft simulation design (conceptual design, flight dynamics, aerodynamic modeling, MDO, HIL validation)"


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load_fixture(path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    """Load a frozen root/link fixture for offline tests.

    Shape: {"roots": [...], "links": {"R1": [{"claim_id":..., "relation":..., "reason":...}]}}
    """
    payload = read_json(path)
    roots = payload.get("roots")
    links = payload.get("links")
    if not isinstance(roots, list) or not isinstance(links, dict):
        raise ValueError("fixture must contain roots (list) and links (object)")
    return roots, links


def render_viewer(template: Path, view: dict) -> str:
    text = template.read_text(encoding="utf-8")
    if text.count("__VIEW_DATA__") != 1:
        raise ValueError("viewer template must contain exactly one __VIEW_DATA__ placeholder")
    payload = json.dumps(view, ensure_ascii=False).replace("</", "<\\/")
    return text.replace("__VIEW_DATA__", payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--view", type=Path, default=DEFAULT_VIEW)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--question-zh", default=DEFAULT_QUESTION_ZH)
    parser.add_argument("--question-en", default=DEFAULT_QUESTION_EN)
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--max-roots", type=int, default=5)
    parser.add_argument("--fixture", type=Path, help="offline roots/links fixture; skips LLM calls")
    parser.add_argument("--edge-roles", type=Path, default=DEFAULT_EDGE_ROLES,
                        help="edge_role_audit output; only chainable/deduction roles form support chains")
    parser.add_argument("--template", type=Path, default=HERE / "viewer_question.html")
    parser.add_argument("--no-viewer", action="store_true")
    args = parser.parse_args()

    view = read_json(args.view)
    edge_roles = None
    if args.edge_roles.is_file():
        payload = read_json(args.edge_roles)
        edge_roles = payload.get("edges") if isinstance(payload.get("edges"), list) else payload
        print(f"edge roles: {len(edge_roles)} from {args.edge_roles}")
    else:
        print(f"warning: edge roles not found at {args.edge_roles}; no typed deduction chains", file=sys.stderr)
    question = {
        "zh": args.question_zh,
        "en": args.question_en,
        "domain": args.domain,
    }
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    if args.fixture:
        roots, links_by_root = load_fixture(args.fixture)
        root_audit = {"fixture": str(args.fixture)}
        link_audit = []
        normalized = []
        known = {str(node.get("id")) for node in view.get("nodes") or []}
        for index, raw in enumerate(roots):
            root = dict(raw)
            root.setdefault("code", f"R{index + 1}")
            root.setdefault("id", f"answer:{root['code'].lower()}")
            root.setdefault("title_zh", root.get("title_en", root["code"]))
            root.setdefault("title_en", root.get("title_zh", root["code"]))
            root.setdefault("statement", "")
            root.setdefault("answer_type", "method_route")
            root.setdefault("scope", "")
            root.setdefault("criteria", "")
            root.setdefault("seed_claim_ids", [])
            root["seed_claim_ids"] = [nid for nid in root["seed_claim_ids"] if nid in known]
            normalized.append(root)
        roots = normalized
    else:
        roots, root_audit = ql.generate_roots(
            view, question, call_json=_post_json, max_roots=args.max_roots,
        )
        links_by_root, link_audit = ql.link_roots(
            view, question, roots, call_json=_post_json,
        )

    answer_layer = ql.build_answer_layer(view, question, roots, links_by_root, edge_roles=edge_roles)
    augmented = ql.inject_answer_layer(view, answer_layer)
    evaluation = ql.evaluate(view, answer_layer)

    write_json(out / "answer_layer.json", answer_layer)
    write_json(out / "domain_view_question.json", augmented)
    write_json(out / "evaluation.json", evaluation)
    (out / "evaluation.md").write_text(ql.evaluation_markdown(evaluation, answer_layer), encoding="utf-8")
    write_json(out / "llm_raw.json", {
        "question": question,
        "root_generation": root_audit,
        "link_calls": link_audit,
        "run": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "view": str(args.view),
            "edge_roles": str(args.edge_roles) if edge_roles is not None else None,
            "max_roots": args.max_roots,
        },
    })

    viewer_path = None
    if not args.no_viewer and args.template.is_file():
        html = render_viewer(args.template, augmented)
        viewer_path = out / "domain_viewer_question.html"
        viewer_path.write_text(html, encoding="utf-8")
    elif not args.no_viewer:
        print(f"warning: viewer template not found at {args.template}; skipping HTML", file=sys.stderr)

    stats = evaluation["answer_layer"]
    print(json.dumps({
        "out": str(out),
        "question": question,
        "roots": [
            {
                "code": root["code"],
                "title": root["title_zh"],
                "verdict": root["verdict_class"],
                "support": root["stats"]["support_claim_count"],
                "papers": root["stats"]["support_paper_count"],
                "chains": root["stats"]["chain_count"],
            }
            for root in answer_layer["roots"]
        ],
        "linked_claims": stats["linked_claim_count"],
        "chain_nodes": stats["chain_node_count"],
        "coverage_of_connected": stats["coverage_of_connected_claims"],
        "answer_edges": stats["answer_edge_count"],
        "viewer": str(viewer_path) if viewer_path else None,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
