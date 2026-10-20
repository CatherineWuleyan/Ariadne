"""Step 6: materialize the question-led answer layer.

This stage is deliberately separate from Step 3's domain-conclusion synthesis.
Step 3 creates K conclusions from cross-paper relations; Step 6 takes the
compiled claim graph *plus a question* and adds:

* candidate answer root nodes (never claimed as proven conclusions),
* answer links from existing claims/K conclusions to each root,
* precomputed premise-to-answer chains for the viewer.

The stage emits one immutable artifact; the view adapter only reads it, so the
same run can be re-rendered without another LLM call.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_harness.models import Finding, JSONDict
from pipeline_harness.plugins import ArtifactDraft, StageContext, StageResult
from pipeline_harness.store import RunStore, atomic_write_json

from .question_layer import (
    build_answer_layer,
    evaluate,
    evaluation_markdown,
    generate_roots,
    inject_answer_layer,
    link_roots,
)
from .step3 import _post_json
from .view import MergeViewAdapter


DEFAULT_QUESTION_ZH = "怎么完成飞机的仿真设计？"
DEFAULT_QUESTION_EN = "How can aircraft simulation design be carried out?"
DEFAULT_DOMAIN = (
    "aircraft simulation design (conceptual design, flight dynamics, "
    "aerodynamic modeling, MDO, HIL validation)"
)


def _read_fixture(path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    roots = payload.get("roots")
    links = payload.get("links")
    if not isinstance(roots, list) or not isinstance(links, dict):
        raise ValueError("answers_fixture must contain roots (list) and links (object)")
    return roots, links


def _normalise_fixture_roots(roots: list[dict], view: JSONDict) -> list[dict]:
    known = {str(node.get("id")) for node in view.get("nodes") or []}
    normalised = []
    for index, raw in enumerate(roots):
        root = dict(raw)
        root.setdefault("code", f"R{index + 1}")
        root.setdefault("id", f"answer:{str(root['code']).lower()}")
        root.setdefault("title_zh", root.get("title_en", root["code"]))
        root.setdefault("title_en", root.get("title_zh", root["code"]))
        root.setdefault("statement", "")
        root.setdefault("answer_type", "method_route")
        root.setdefault("scope", "")
        root.setdefault("criteria", "")
        root["seed_claim_ids"] = [nid for nid in root.get("seed_claim_ids") or [] if nid in known]
        normalised.append(root)
    return normalised


class Step6QuestionLayerPlugin:
    """Generate candidate answers and link the frozen claim graph to them."""

    def run(self, context: StageContext) -> StageResult:
        options = context.options or {}
        question = {
            "zh": str(options.get("question_zh") or DEFAULT_QUESTION_ZH),
            "en": str(options.get("question_en") or DEFAULT_QUESTION_EN),
            "domain": str(options.get("domain") or DEFAULT_DOMAIN),
        }
        max_roots = int(options.get("max_roots") or 5)
        fixture = options.get("answers_fixture")
        edge_roles = None
        edge_roles_path = options.get("edge_roles")
        if edge_roles_path:
            payload = json.loads(Path(str(edge_roles_path)).read_text(encoding="utf-8"))
            edge_roles = payload.get("edges") if isinstance(payload, dict) else payload
        try:
            store = RunStore(context.run_dir)
            base_view = MergeViewAdapter().project(store, context.inputs)
            view = base_view.to_dict()

            if fixture:
                roots, links_by_root = _read_fixture(Path(str(fixture)))
                roots = _normalise_fixture_roots(roots, view)
                root_audit = {"fixture": str(fixture)}
                link_audit = []
            else:
                roots, root_audit = generate_roots(
                    view, question, call_json=_post_json, max_roots=max_roots,
                )
                links_by_root, link_audit = link_roots(
                    view, question, roots, call_json=_post_json,
                )

            answer_layer = build_answer_layer(
                view, question, roots, links_by_root, edge_roles=edge_roles,
            )
            evaluation = evaluate(view, answer_layer)
            augmented = inject_answer_layer(view, answer_layer)
            # Validate the injected view before publishing it; the view adapter
            # will rebuild it from the immutable artifact.
            from pipeline_harness.view.model import ViewDocument
            augmented["metadata"]["answer_layer"] = answer_layer
            ViewDocument.from_dict(augmented)

            layer_path = context.work_dir / "question_answer_layer.json"
            atomic_write_json(layer_path, answer_layer)
            evaluation_path = context.work_dir / "question_evaluation.json"
            atomic_write_json(evaluation_path, evaluation)
            markdown_path = context.work_dir / "question_evaluation.md"
            markdown_path.write_text(evaluation_markdown(evaluation, answer_layer), encoding="utf-8")
            audit_path = context.work_dir / "question_llm_raw.json"
            atomic_write_json(audit_path, {
                "question": question,
                "root_generation": root_audit,
                "link_calls": link_audit,
            })

            stats = evaluation["answer_layer"]
            findings = []
            if len(answer_layer["roots"]) < 3:
                findings.append(Finding(
                    "QUESTION_FEW_ANSWERS", "warning",
                    f"only {len(answer_layer['roots'])} candidate answers received a defensible "
                    "support link; a question-led graph needs enough routes to be a real choice",
                ))
            if not any(root.get("oppose_ids") for root in answer_layer["roots"]):
                findings.append(Finding(
                    "QUESTION_NO_OPPOSITION", "info",
                    "no root has an opposition link; this is expected when the merge graph has "
                    "no contradiction operator, not evidence that every answer is uncontested",
                ))
            return StageResult(
                "succeeded",
                artifacts=[
                    ArtifactDraft(layer_path, "question.answer_layer", "application/json", {
                        "schema_name": answer_layer.get("schema_name"),
                        "schema_version": answer_layer.get("schema_version"),
                        "root_count": len(answer_layer["roots"]),
                        "answer_edge_count": len(answer_layer["answer_edges"]),
                    }),
                    ArtifactDraft(evaluation_path, "question.evaluation", "application/json", {
                        "coverage_of_connected_claims": stats.get("coverage_of_connected_claims"),
                    }),
                    ArtifactDraft(markdown_path, "question.evaluation_markdown", "text/markdown", {}),
                    ArtifactDraft(audit_path, "question.llm_audit", "application/json", {
                        "prompt_version": answer_layer.get("generation", {}).get("prompt_version"),
                    }),
                ],
                findings=findings,
                metadata={
                    "step": 6,
                    "question": question,
                    "root_count": len(answer_layer["roots"]),
                    "answer_edge_count": len(answer_layer["answer_edges"]),
                    "linked_claim_count": stats.get("linked_claim_count"),
                    "chain_node_count": stats.get("chain_node_count"),
                    "coverage_of_connected_claims": stats.get("coverage_of_connected_claims"),
                    "chainable_edge_count": stats.get("chainable_edge_count", 0),
                    "edge_role_counts": stats.get("edge_role_counts", {}),
                    "roots": [
                        {
                            "code": root["code"],
                            "title_zh": root["title_zh"],
                            "verdict_class": root["verdict_class"],
                            "support_claim_count": root["stats"]["support_claim_count"],
                            "support_paper_count": root["stats"]["support_paper_count"],
                            "chain_count": root["stats"]["chain_count"],
                        }
                        for root in answer_layer["roots"]
                    ],
                },
            )
        except (ValueError, KeyError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            return StageResult("failed", findings=[Finding("STEP6_FAILED", "error", str(exc))])


__all__ = ["Step6QuestionLayerPlugin"]
