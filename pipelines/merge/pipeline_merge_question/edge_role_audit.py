#!/usr/bin/env python3
"""Audit existing graph edges into the pipeline's *existing* reasoning taxonomy.

Policy (agreed with the user):
    deduction > special_case > reproduces > bounds > abduction > analogy > infer
``infer`` is the residual class: it means none of the specific types fit, and it
is **not chainable by default**.

This script is the retrofit path for a graph that was produced before Step 3
recorded ``reasoning_type`` on the final view.  It reads:
  * the current view model (edges + claim texts),
  * the Step 3 proposals (for cross-paper ``expression`` justifications),
and writes ``edge_roles.json``.  No Step 2/3 re-run is needed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
for _extra in (_REPO, _REPO / "agent", _REPO / "pipelines" / "single_paper", _REPO / "pipelines" / "merge"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from pipeline_merge_question.step3 import _post_json

TYPES = ["deduction", "special_case", "reproduces", "bounds", "abduction", "analogy", "infer"]
RESIDUAL_REASONS = {"precondition", "enabling", "instance", "unclear"}
CHAINABLE_TYPES = {"deduction"}
DEFAULT_VIEW = Path("outputs/aircraft/final/domain_view_question.json")
DEFAULT_STEP3 = Path(
    "outputs/aircraft/versions/02-native-311-2026-10-13/"
    "artifacts/blobs/artifact_42958528016f4cbe972f8675b327ee44.json"
)
DEFAULT_OUT = Path("outputs/aircraft/final/edge_roles.json")


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def short(value: Any, limit: int = 520) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def paper_of(node_id: str) -> str:
    match = re.match(r"^knowledge:papers:(paper_[^:]+)::", str(node_id))
    return match.group(1) if match else "?"


def expression_map(step3: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """(source, target) -> the Step 3 expression that introduced the edge."""
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for weakpoint in step3.get("weakpoints") or []:
        payload = weakpoint.get("payload") or {}
        expression = str(payload.get("expression") or "")
        reasoning_type = str(payload.get("reasoning_type") or "")
        for evidence in payload.get("evidence_claim_ids") or []:
            for target in payload.get("target_claim_id") or []:
                result[(str(evidence), str(target))] = {
                    "expression": expression,
                    "step3_type": reasoning_type,
                }
    return result


def build_edges(view: Mapping[str, Any], expressions: Mapping[tuple[str, str], Mapping[str, Any]]) -> list[dict[str, Any]]:
    nodes = {str(n.get("id")): n for n in view.get("nodes") or []}
    records = []
    for edge in view.get("edges") or []:
        if edge.get("layer") == "answer":
            continue
        source, target = str(edge.get("source")), str(edge.get("target"))
        src, dst = nodes.get(source, {}), nodes.get(target, {})
        key = (source.replace("knowledge:", ""), target.replace("knowledge:", ""))
        meta = expressions.get(key, {})
        records.append({
            "edge_id": str(edge.get("id")),
            "source": source,
            "target": target,
            "source_code": src.get("display_label", source),
            "target_code": dst.get("display_label", target),
            "source_paper": paper_of(source),
            "target_paper": paper_of(target),
            "layer": str(edge.get("layer") or ""),
            "semantic_type": str(edge.get("semantic_type") or edge.get("label") or ""),
            "source_content": short(src.get("summary"), 520),
            "target_content": short(dst.get("summary"), 520),
            "step3_expression": short(meta.get("expression"), 900),
            "step3_type": str(meta.get("step3_type") or ""),
        })
    return records


PROMPT_HEADER = """You are auditing reasoning edges in an aircraft-simulation domain graph.

For each EDGE, choose the MOST SPECIFIC type that applies, in this exact order:

1. deduction — the premise and conclusion are about the SAME object/quantity/conditions,
   and the conclusion follows logically from the premise with no missing rule.
   Do NOT use deduction for "B needs A", "A is an input for B", "A can represent B",
   or cross-aircraft/cross-dataset extrapolation.
2. special_case — one proposition is an explicit instance/subtype/special case of the other.
3. reproduces — an independent measurement or independent method reproduces the same quantity.
4. bounds — the premise limits the range, scope, or applicable conditions of the conclusion.
5. abduction — a hypothesis is proposed to explain an observation (alternative explanations possible).
6. analogy — a cross-object/cross-domain mapping, including different aircraft, datasets, or models.
7. infer — NONE of the above applies. This is the residual class. It is NOT a synonym for
   "supports" and it is NOT chainable. For infer you MUST provide residual_reason:
   precondition | enabling | instance | unclear.

Rules:
- Prefer a specific type over infer whenever it genuinely fits.
- Do not promote a dependency ("needs", "requires") to deduction.
- Do not promote a cross-aircraft similarity to deduction; use analogy.
- `same_object` must be true for deduction; if conditions differ, it cannot be deduction.
- reason: one English sentence explaining the choice.
- Use the exact edge_id from the supplied batch.

Return JSON only:
{"edges":[{"edge_id":"...","reasoning_type":"...","same_object":true,"residual_reason":"precondition|enabling|instance|unclear|null","reason":"..."}]}
"""


def prompt_for(batch: list[Mapping[str, Any]]) -> str:
    payload = []
    for edge in batch:
        payload.append({
            "edge_id": edge["edge_id"],
            "layer": edge["layer"],
            "relation": edge["semantic_type"],
            "source": {
                "code": edge["source_code"],
                "paper": edge["source_paper"],
                "content": edge["source_content"],
            },
            "target": {
                "code": edge["target_code"],
                "paper": edge["target_paper"],
                "content": edge["target_content"],
            },
            "step3_expression": edge["step3_expression"] or None,
            "step3_type": edge["step3_type"] or None,
        })
    return PROMPT_HEADER + "\nEDGES=" + json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"


def classify_rule(edge: Mapping[str, Any]) -> tuple[str, str | None, bool, str]:
    """Deterministic priority classifier based on Step 3's own wording.

    Paper-internal edges have no Step 3 expression.  They stay residual ``infer``
    unless the claim text states a specific relation; a missing justification is
    not evidence, so they are non-chainable.
    """
    if str(edge.get("semantic_type") or "") == "conjunction":
        return "conjunction", None, False, "symmetric conjunction operator; not a reasoning type"
    step3_type = str(edge.get("step3_type") or "")
    if step3_type in {"special_case", "reproduces", "bounds", "abduction", "analogy", "deduction"}:
        return step3_type, None, step3_type == "deduction", f"Step 3 already typed this as {step3_type}"
    expression = str(edge.get("step3_expression") or "")
    text = f"{expression} {edge.get('source_content') or ''} {edge.get('target_content') or ''}".lower()
    # Only cross-paper edges carry an expression; paper-internal edges remain
    # unclassified instead of being guessed from generic words in claim text.
    if not expression:
        return "infer", "unclear", False, "paper-internal edge; no Step 3 justification in the final view"
    # Dependency / enabling wording is evaluated before "what follows" because
    # Step 3 often writes "what follows" and then says the target *needs* it.
    if re.search(r"\b(needs?|requires?|required|needed for|input for|must be supplied)\b", text):
        return "infer", "precondition", False, "Step 3 expression states a dependency/precondition"
    if re.search(r"\b(can represent|can be represented|provides?|enables?|allows?|can be used)\b", text):
        return "infer", "enabling", False, "Step 3 expression states a representation/capability relation"
    if re.search(r"\b(one specific|one example|for example|for instance|an instance of)\b", text):
        return "infer", "instance", False, "Step 3 expression uses the claim as an example/instance"
    if re.search(r"\bspecial case\b|\bsubtype\b|\bis a case of\b", text):
        return "special_case", None, False, "explicit special-case/subtype wording"
    if re.search(r"\bindependent(ly)?\b.{0,40}\breproduc|\breproduc(?:e|es|ed|ing)\b", text):
        return "reproduces", None, False, "independent reproduction wording"
    if re.search(r"\b(upper|lower) bound\b|\brange\b|\bconstrain(?:s|ed)? the\b", text):
        return "bounds", None, False, "scope/range wording"
    if re.search(r"\banalog(y|ous|ical)\b|\bcross[- ]domain\b|\bdifferent aircraft\b", text):
        return "analogy", None, False, "cross-object analogy wording"
    if re.search(r"\bhypothes(is|es)\b|\bexplain(?:s|ed|ing)?\b", text):
        return "abduction", None, False, "hypothesis/explanation wording"
    # "What follows" alone is not an entailment: the Step 3 expressions routinely
    # use it for a dependency/analogy ("... which the target needs").  Without a
    # real semantic check, keep it residual rather than inventing a deduction.
    return "infer", "unclear", False, "no specific entailment established by the Step 3 expression"


def rule_fallback(edge: Mapping[str, Any]) -> tuple[str, str | None, bool, str]:
    text = f"{edge.get('step3_expression') or ''} {edge.get('source_content') or ''} {edge.get('target_content') or ''}".lower()
    if re.search(r"\b(needs?|requires?|required|input for|needed for)\b", text):
        return "infer", "precondition", False, "dependency / precondition wording; not an entailment"
    if re.search(r"\b(can represent|can be represented|provides?|enables?|allows?)\b", text):
        return "infer", "enabling", False, "capability / enabling relation"
    if re.search(r"\b(one specific|for example|for instance|an example|instance of)\b", text):
        return "special_case", None, False, "explicit instance/example"
    if re.search(r"\banalog(y|ous|ical)\b|cross[- ]domain|different aircraft", text):
        return "analogy", None, False, "cross-object analogy"
    if re.search(r"\breproduc(e|es|ed|ing)\b", text):
        return "reproduces", None, False, "independent reproduction"
    return "infer", "unclear", False, "no specific type established by the audit"


def normalize(response: Any, batch: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    raw = response.get("edges") if isinstance(response, Mapping) else None
    if not isinstance(raw, list):
        raw = []
    by_id = {str(item["edge_id"]): item for item in batch}
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        edge_id = str(item.get("edge_id") or "")
        if edge_id not in by_id:
            continue
        reasoning_type = str(item.get("reasoning_type") or item.get("type") or "infer").lower()
        if reasoning_type not in TYPES:
            reasoning_type = "infer"
        same_object = bool(item.get("same_object"))
        residual = item.get("residual_reason")
        residual = str(residual) if residual in RESIDUAL_REASONS else None
        reason = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()
        # Mechanical guard: conditions must match for deduction, and a dependency
        # wording cannot be a deduction even if the model says so.
        if reasoning_type == "deduction" and not same_object:
            reasoning_type = "analogy" if edge_cross_paper(by_id[edge_id]) else "infer"
            residual = None if reasoning_type == "analogy" else "unclear"
        if reasoning_type == "deduction" and re.search(
            r"\b(needs?|requires?|needed for|input for)\b",
            (by_id[edge_id].get("step3_expression") or "") + " " + reason,
            re.IGNORECASE,
        ):
            reasoning_type = "infer"
            residual = "precondition"
        if reasoning_type == "infer" and not residual:
            residual = "unclear"
        result[edge_id] = {
            "reasoning_type": reasoning_type,
            "residual_reason": residual if reasoning_type == "infer" else None,
            "same_object": same_object,
            "chainable": reasoning_type in CHAINABLE_TYPES,
            "reason": reason or "audited",
        }
    return result


def edge_cross_paper(edge: Mapping[str, Any]) -> bool:
    return edge.get("source_paper") != edge.get("target_paper")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--view", type=Path, default=DEFAULT_VIEW)
    parser.add_argument("--step3", type=Path, default=DEFAULT_STEP3)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--raw-out", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--checkpoint", type=Path, default=None,
                        help="per-batch checkpoint; defaults to <out>.partial.json")
    parser.add_argument("--limit", type=int, default=0, help="audit only the first N edges (debug)")
    parser.add_argument("--rules-only", action="store_true",
                        help="deterministic Step 3 wording classifier; no LLM calls")
    args = parser.parse_args()

    view = read_json(args.view)
    step3 = read_json(args.step3) if args.step3.is_file() else {"weakpoints": []}
    edges = build_edges(view, expression_map(step3))
    if args.limit:
        edges = edges[: args.limit]
    print(f"auditing {len(edges)} edges in batches of {args.batch_size}")

    checkpoint_path = args.checkpoint or args.out.with_suffix(".partial.json")
    roles: dict[str, dict[str, Any]] = {}
    if args.rules_only:
        for edge in edges:
            reasoning_type, residual, chainable, reason = classify_rule(edge)
            roles[edge["edge_id"]] = {
                "reasoning_type": reasoning_type,
                "residual_reason": residual,
                "same_object": reasoning_type == "deduction",
                "chainable": chainable,
                "reason": reason,
            }
        print(f"rules-only: classified {len(roles)} edges", flush=True)
    if checkpoint_path.is_file() and not args.rules_only:
        try:
            prior = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            for item in prior.get("roles") or []:
                if isinstance(item, dict) and item.get("edge_id"):
                    roles[str(item["edge_id"])] = item
            print(f"resumed {len(roles)} roles from {checkpoint_path}", flush=True)
        except (OSError, json.JSONDecodeError):
            roles = {}
    pending = [] if args.rules_only else [edge for edge in edges if edge["edge_id"] not in roles]
    raw_batches = []
    for start in range(0, len(pending), args.batch_size):
        batch = pending[start:start + args.batch_size]
        prompt = prompt_for(batch)
        response = _post_json(prompt)
        parsed = normalize(response, batch)
        for edge in batch:
            if edge["edge_id"] not in parsed:
                rtype, residual, chainable, reason = rule_fallback(edge)
                parsed[edge["edge_id"]] = {
                    "reasoning_type": rtype,
                    "residual_reason": residual,
                    "same_object": rtype == "deduction",
                    "chainable": chainable,
                    "reason": reason,
                }
        roles.update(parsed)
        raw_batches.append({"start": start, "prompt": prompt, "response": response})
        checkpoint_path.write_text(json.dumps({
            "schema": "edge_role_audit_checkpoint",
            "done": len(roles),
            "total": len(edges),
            "roles": [roles[edge["edge_id"]] for edge in edges if edge["edge_id"] in roles],
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        counts = Counter(item["reasoning_type"] for item in parsed.values())
        print(f"  batch {start // args.batch_size + 1}/{(len(pending) + args.batch_size - 1) // args.batch_size} "
              f"({len(roles)}/{len(edges)} done): " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
              flush=True)

    by_id = {edge["edge_id"]: edge for edge in edges}
    output_edges = []
    for edge_id, edge in by_id.items():
        role = roles[edge_id]
        output_edges.append({**edge, **role})
    counts = Counter(item["reasoning_type"] for item in output_edges)
    output = {
        "schema": "edge_role_audit",
        "version": "1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": os.environ.get("DEEPSEEK_MERGE_MODEL", "deepseek-v4-flash"),
        "policy": "deduction > special_case > reproduces > bounds > abduction > analogy > infer (residual, non-chainable)",
        "chainable_types": sorted(CHAINABLE_TYPES),
        "edge_count": len(output_edges),
        "type_counts": dict(sorted(counts.items())),
        "edges": output_edges,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.raw_out:
        args.raw_out.write_text(json.dumps(raw_batches, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if checkpoint_path.is_file():
        checkpoint_path.unlink()
    print(json.dumps({
        "out": str(args.out),
        "edges": len(output_edges),
        "types": dict(sorted(counts.items())),
        "chainable": sum(1 for edge in output_edges if edge["chainable"]),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
