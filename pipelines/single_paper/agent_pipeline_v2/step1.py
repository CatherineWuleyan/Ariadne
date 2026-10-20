"""Pipeline 7.0 Step 1: freeze claims_final and construct the initial graph."""
from __future__ import annotations

import json
import re
from typing import Any

from pipeline_harness.domain.stages import (
    _ast_claim_ids,
    _expression_tokens,
    _paper_paragraphs,
    _parse_expression,
)
from pipeline_harness.models import Finding
from pipeline_harness.plugins import ArtifactDraft, StageContext, StageResult
from pipeline_harness.store import atomic_write_json

from .authoring import emit_step1


INPUT_BUNDLE_KIND = "input.bundle"
CLAIMS_FINAL_KIND = "source.claims_final"
PAPER_TEXT_KIND = "source.paper_text"
INPUT_BUNDLE_VERSION = "1.0.0"
CLAIM_REFERENCE_PATTERN = re.compile(r"\bclaim\s+(\d+)\b", re.IGNORECASE)
EVIDENCE_EXPRESSION_PATTERN = re.compile(r"^\s*(.+?)\s*是\s*(.+?)\s*的例子或证据\s*$")
POSTFIX_SYMMETRIC_PATTERN = re.compile(
    r"^\s*(\[[^\]]+\])\s*(?:与|和)\s*(\[[^\]]+\])\s*(?:矛盾|不可同时成立|等价)\s*$"
)


def _default_package_name(run_id: str) -> str:
    """Keep the generated local package name inside Gaia's QID length contract."""

    suffix = run_id.rsplit("_", 1)[-1]
    return f"paper_{suffix}"


def _read_json(context: StageContext, ref) -> dict[str, Any]:
    try:
        value = json.loads(context.artifact_path(ref).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{ref.kind} must contain valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{ref.kind} must contain a JSON object")
    return value


def _require_one(context: StageContext, kind: str):
    try:
        return context.require_one(kind)
    except ValueError as exc:
        raise ValueError(f"Step 1 requires exactly one {kind} input") from exc


def _validate_claims_final(value: dict[str, Any]) -> None:
    for field in ("claim", "relation", "note"):
        if not isinstance(value.get(field), list):
            raise ValueError(f"claims_final.json field {field!r} must be an array")
    for role in ("claim", "note"):
        seen: set[int] = set()
        for index, item in enumerate(value[role]):
            if not isinstance(item, dict):
                raise ValueError(f"claims_final.json {role}[{index}] must be an object")
            number, text = item.get("number"), item.get("text")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise ValueError(f"claims_final.json {role}[{index}].number must be a positive integer")
            if number in seen:
                raise ValueError(f"claims_final.json {role} numbers must be unique: {number}")
            seen.add(number)
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"claims_final.json {role}[{index}].text must be non-empty")
            if not isinstance(item.get("conclusion"), str) or not item["conclusion"].strip():
                raise ValueError(f"claims_final.json {role}[{index}].conclusion must be non-empty")
    for index, item in enumerate(value["relation"]):
        if not isinstance(item, dict):
            raise ValueError(f"claims_final.json relation[{index}] must be an object")
        connects = item.get("connects")
        if not isinstance(connects, list) or len(connects) < 2 or any(
            not isinstance(number, int) or isinstance(number, bool) or number < 1
            for number in connects
        ):
            raise ValueError(f"claims_final.json relation[{index}].connects must contain at least two positive integers")
        if len(set(connects)) != len(connects):
            raise ValueError(f"claims_final.json relation[{index}].connects must not repeat a claim number")

    claim_numbers = {item["number"] for item in value["claim"]}
    for role in ("claim", "note"):
        for index, item in enumerate(value[role]):
            references = _claim_reference_numbers(item["text"])
            unresolved = [number for number in references if number not in claim_numbers]
            if unresolved:
                raise ValueError(
                    f"claims_final.json {role}[{index}].text references unknown claim numbers: {unresolved}"
                )


def _claim_reference_numbers(text: str) -> list[int]:
    """Return internal claim references once, preserving their textual order."""
    return list(dict.fromkeys(int(match.group(1)) for match in CLAIM_REFERENCE_PATTERN.finditer(text)))


def _claim_reference_closure(text: str, claim_texts: dict[int, str]) -> list[int]:
    """Resolve the finite transitive context needed to understand a proposition."""
    ordered: list[int] = []
    visited: set[int] = set()
    active: set[int] = set()

    def visit(number: int) -> None:
        if number in active:
            raise ValueError(f"claims_final.json contains a cyclic claim reference involving claim {number}")
        if number in visited:
            return
        active.add(number)
        visited.add(number)
        ordered.append(number)
        for nested in _claim_reference_numbers(claim_texts[number]):
            visit(nested)
        active.remove(number)

    for reference in _claim_reference_numbers(text):
        visit(reference)
    return ordered


def _self_contained_text(text: str, references: list[int], claim_texts: dict[int, str]) -> str:
    """Preserve the source proposition and append deterministic referenced-claim context."""
    canonical = text.strip()
    if not references:
        return canonical
    context = " ".join(f"[Referenced claim {number}: {claim_texts[number].strip()}]" for number in references)
    return f"{canonical} {context}"


def _conjunction_claim_numbers(expression: str, references: dict[str, str]) -> list[int]:
    """Parse one claim or a conjunction of claims in the fixed relation language."""
    ast = _parse_expression(_expression_tokens(expression, references))

    def visit(node: dict[str, Any]) -> list[int]:
        if node["kind"] == "claim":
            return [int(node["id"])]
        if node["kind"] != "conjunction":
            raise ValueError("evidence relation sides may contain only claims joined by 且/和/与")
        return [*visit(node["left"]), *visit(node["right"])]

    return visit(ast)


def _relation_endpoints(relation: dict[str, Any], known_numbers: set[int]) -> list[tuple[list[int], int]]:
    """Derive directed link endpoints from the fixed expression, never connects order."""
    expression = relation.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("relation expression must be a non-empty fixed-language string")
    references = {str(number): str(number) for number in known_numbers}
    evidence_match = EVIDENCE_EXPRESSION_PATTERN.fullmatch(expression)
    if evidence_match is not None:
        sources = _conjunction_claim_numbers(evidence_match.group(1), references)
        targets = _conjunction_claim_numbers(evidence_match.group(2), references)
    elif (symmetric_match := POSTFIX_SYMMETRIC_PATTERN.fullmatch(expression)) is not None:
        operands = sorted(
            _conjunction_claim_numbers(symmetric_match.group(index), references)[0]
            for index in (1, 2)
        )
        sources, targets = operands[:-1], operands[-1:]
    else:
        ast = _parse_expression(_expression_tokens(expression, references))
        if ast["kind"] == "implies":
            sources = [int(item) for item in _ast_claim_ids(ast["left"])]
            targets = [int(item) for item in _ast_claim_ids(ast["right"])]
        elif ast["kind"] in {"equivalence", "contradiction", "conjunction", "disjunction", "negation"}:
            operands = sorted(int(item) for item in _ast_claim_ids(ast))
            if len(operands) < 2:
                raise ValueError("a non-directional relation must reference at least two claims")
            sources, targets = operands[:-1], operands[-1:]
        else:
            raise ValueError("relation expression must be evidence, implication, or a fixed logical relation")
    mentioned = [*sources, *targets]
    if len(mentioned) != len(set(mentioned)):
        raise ValueError("relation expression must reference each claim exactly once")
    connects = relation["connects"]
    if set(mentioned) != set(connects) or len(mentioned) != len(connects):
        raise ValueError("relation expression claim references do not match connects")
    if not sources or not targets:
        raise ValueError("relation expression must determine at least one source and target")
    return [(list(sources), target) for target in targets]


class ClaimsFinalInputImporter:
    """Freeze the two Step 1 inputs into the one Pipeline 7.0 input bundle."""

    def run(self, context: StageContext) -> StageResult:
        try:
            paper_ref = _require_one(context, PAPER_TEXT_KIND)
            claims_ref = _require_one(context, CLAIMS_FINAL_KIND)
            if paper_ref.media_type != "text/markdown":
                raise ValueError("source.paper_text must have media type text/markdown")
            if claims_ref.media_type != "application/json":
                raise ValueError("source.claims_final must have media type application/json")
            paper_text = context.artifact_path(paper_ref).read_text(encoding="utf-8-sig")
            if not paper_text.strip():
                raise ValueError("paper_text.md must be non-empty")
            claims_final = _read_json(context, claims_ref)
            _validate_claims_final(claims_final)
        except ValueError as exc:
            return StageResult("failed", findings=[Finding("STEP1_INPUT_INVALID", "error", str(exc))])

        # Reuse the existing optional source.original_figure input contract when
        # callers provide the paper's extracted figures in the manifest.
        figures = []
        for figure_ref in context.find_all("source.original_figure"):
            if figure_ref.media_type not in {"image/jpeg", "image/png"}:
                continue
            figures.append({
                "artifact_id": figure_ref.artifact_id,
                "sha256": figure_ref.sha256,
                "media_type": figure_ref.media_type,
                "figure": figure_ref.metadata.get("figure", figure_ref.metadata.get("source_filename")),
                "sequence": figure_ref.metadata.get("sequence"),
            })
        bundle = {
            "schema_version": INPUT_BUNDLE_VERSION,
            "sources": {
                PAPER_TEXT_KIND: [{
                    "artifact_id": paper_ref.artifact_id, "sha256": paper_ref.sha256,
                    "media_type": paper_ref.media_type, "value": paper_text,
                }],
                CLAIMS_FINAL_KIND: [{
                    "artifact_id": claims_ref.artifact_id, "sha256": claims_ref.sha256,
                    "media_type": claims_ref.media_type, "value": claims_final,
                }],
            },
            "figures": figures,
        }
        output = context.work_dir / "input.bundle.json"
        atomic_write_json(output, bundle)
        return StageResult("succeeded", [ArtifactDraft(output, INPUT_BUNDLE_KIND, "application/json", {
            "schema_version": INPUT_BUNDLE_VERSION,
            "source_kinds": [PAPER_TEXT_KIND, CLAIMS_FINAL_KIND],
            "figure_count": len(figures),
        })])


def _bundle_source(context: StageContext, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle_ref = _require_one(context, INPUT_BUNDLE_KIND)
    bundle = _read_json(context, bundle_ref)
    sources = bundle.get("sources")
    entries = sources.get(kind) if isinstance(sources, dict) else None
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise ValueError(f"input.bundle.sources must contain exactly one {kind}")
    entry = entries[0]
    if not all(isinstance(entry.get(field), str) and entry[field] for field in ("artifact_id", "sha256", "media_type")):
        raise ValueError(f"input.bundle source {kind} is missing provenance")
    return entry, bundle_ref.metadata


class Step1ImportClaimsFinalPlugin:
    """Import all claims, notes, and declared non-reasoning relations."""

    def run(self, context: StageContext) -> StageResult:
        try:
            paper_entry, _ = _bundle_source(context, PAPER_TEXT_KIND)
            claims_entry, _ = _bundle_source(context, CLAIMS_FINAL_KIND)
            claims_final = claims_entry.get("value")
            if not isinstance(claims_final, dict):
                raise ValueError("input.bundle source.claims_final value must be an object")
            _validate_claims_final(claims_final)
        except ValueError as exc:
            return StageResult("failed", findings=[Finding("STEP1_BUNDLE_INVALID", "error", str(exc))])

        document: dict[str, Any] = {
            "schema_version": "1.1.0",
            "revision": {"revision_id": f"revision_{context.run_id}_step_1", "supersedes": None, "parent_hash": None, "content_hash": ""},
            "package": {"paper_id": str(context.options.get("paper_id", context.run_id)), "namespace": str(context.options.get("namespace", "papers")), "name": str(context.options.get("package_name", _default_package_name(context.run_id))), "version": str(context.options.get("package_version", "1"))},
            "graph": {"nodes": [], "operators": [], "strategies": [], "composes": []},
            "knowledges": {},
            "workflow": {"source_records": [], "source_anchors": [], "weakpoints": [], "gaps": [], "non_reasoning_links": [], "revisions": []},
        }
        paper_text = paper_entry["value"]
        if not isinstance(paper_text, str):
            return StageResult("failed", findings=[Finding("STEP1_BUNDLE_INVALID", "error", "input.bundle source.paper_text value must be text")])
        for paragraph in _paper_paragraphs(paper_text.splitlines()):
            document["workflow"]["source_anchors"].append({
                "anchor_id": paragraph["anchor_id"], "artifact_id": paper_entry["artifact_id"],
                "source_kind": PAPER_TEXT_KIND,
                "locator": {
                    "type": "markdown_span", "start_line": paragraph["start_line"],
                    "end_line": paragraph["end_line"],
                },
                "relevance": "paragraph",
            })

        claim_ids: dict[int, str] = {}
        all_claim_ids: dict[int, str] = {}
        claim_texts = {item["number"]: item["text"] for item in claims_final["claim"]}
        for role, knowledge_type, prefix in (("claim", "claim", "claim"), ("note", "note", "note")):
            for index, item in enumerate(claims_final[role]):
                number = item["number"]
                knowledge_id = f"{prefix}_{number}"
                anchor_id = f"anchor_{prefix}_{number}"
                document["workflow"]["source_anchors"].append({
                    "anchor_id": anchor_id, "artifact_id": claims_entry["artifact_id"], "source_kind": CLAIMS_FINAL_KIND,
                    "locator": {"type": "json_pointer", "pointer": f"/{role}/{index}"}, "relevance": "source_record",
                })
                # Experimental claims are first-class observations from the
                # moment they enter the pipeline.  Step 2 refines this same
                # ID in place; it must not create a parallel candidate node.
                node_type = "observation_claim" if item.get("is_pure_data") is True else knowledge_type
                try:
                    references = _claim_reference_closure(item["text"], claim_texts)
                except ValueError as exc:
                    return StageResult("failed", findings=[Finding("STEP1_CLAIM_REFERENCE_INVALID", "error", str(exc))])
                document["knowledges"][knowledge_id] = {
                    "type": node_type,
                    "content": {"canonical": _self_contained_text(item["text"], references, claim_texts)},
                    "source_anchor_ids": [anchor_id, *(f"anchor_claim_{reference}" for reference in references)],
                }
                if node_type in {"claim", "observation_claim"}:
                    document["graph"]["nodes"].append(knowledge_id)
                if role == "claim":
                    all_claim_ids[number] = knowledge_id
                    if node_type in {"claim", "observation_claim"}:
                        claim_ids[number] = knowledge_id

        for relation_index, relation in enumerate(claims_final["relation"]):
            connects = relation["connects"]
            unresolved = [number for number in connects if number not in all_claim_ids]
            if unresolved:
                return StageResult("failed", findings=[Finding(
                    "STEP1_RELATION_UNRESOLVED", "error",
                    f"relation[{relation_index}] references unknown claim numbers: {unresolved}",
                )])
            try:
                endpoints = _relation_endpoints(relation, set(all_claim_ids))
            except ValueError as exc:
                return StageResult("failed", findings=[Finding(
                    "STEP1_RELATION_DIRECTION_INVALID", "error",
                    f"relation[{relation_index}] has an invalid fixed expression: {exc}",
                )])
            # Pure-data records are deliberately retained as non-graph
            # observation candidates.  Their expressions are still gated, but
            # the relation cannot become a formal graph link.
            if any(number not in claim_ids for number in connects):
                continue
            for sources, target_number in endpoints:
                link_id = f"relation_{relation_index + 1}"
                if len(endpoints) > 1:
                    link_id = f"{link_id}_target_{target_number}"
                document["workflow"]["non_reasoning_links"].append({
                    "id": link_id,
                    "link_type": "imported_relation",
                    "sources": [claim_ids[number] for number in sources],
                    "target": claim_ids[target_number],
                    "reasoning": False,
                    # High-confidence relations declared in claims_final must be
                    # preserved verbatim and in order by every later stage.
                    "metadata": {"source": "claims_final", "relation_index": relation_index, "relation": relation},
                })

        return emit_step1(context, document)
