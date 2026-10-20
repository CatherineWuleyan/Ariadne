from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from typing import Any, Mapping

from ..models import ArtifactRef, Finding, JSONDict
from ..plugins import ArtifactDraft, StageContext, StageResult, instantiate
from ..store import atomic_write_json
from .contracts import (
    FORMALIZATION_SNAPSHOT_KIND,
    FORMALIZATION_SNAPSHOT_SCHEMA,
    INVALID_FORMALIZATION_SNAPSHOT_KIND,
    SCHEMA_VERSION,
    STEP_NAMES,
    VALIDATION_KIND,
    VALIDATION_SCHEMA,
    canonical_hash,
)
from .indexing import build_knowledge_index, validate_knowledge_index
from .provenance import collect_runtime_anchor_findings
from .tools import ArtifactAwareDomainTool, DomainTool, ToolCallRequest, ToolCallResponse, validate_tool_response
from .validation import build_validation_report


def read_json_ref(context: StageContext, ref: ArtifactRef) -> JSONDict:
    with context.artifact_path(ref).open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"artifact {ref.artifact_id} must contain a JSON object")
    return value


def imported_value(context: StageContext, kind: str) -> tuple[ArtifactRef, Any]:
    bundles = context.find_all("input.bundle")
    if bundles:
        if len(bundles) != 1:
            raise ValueError(f"expected exactly one input.bundle, found {len(bundles)}")
        bundle_ref = bundles[0]
        payload = read_json_ref(context, bundle_ref)
        sources = payload.get("sources")
        entries = sources.get({
            "paper.text": "source.paper_text",
            "claims.cleaned": "source.clean_claims",
            "reasoning_graph.lkm_coarse": "source.lkm_coarse_graph",
        }.get(kind, kind), []) if isinstance(sources, dict) else []
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise ValueError(f"input.bundle must contain exactly one {kind} source")
        entry = entries[0]
        artifact_id = entry.get("artifact_id")
        sha256 = entry.get("sha256")
        media_type = entry.get("media_type")
        if not all(isinstance(value, str) and value for value in (artifact_id, sha256, media_type)) or "value" not in entry:
            raise ValueError(f"input.bundle {kind} source is incomplete")
        source_kind = {
            "paper.text": "source.paper_text",
            "claims.cleaned": "source.clean_claims",
            "reasoning_graph.lkm_coarse": "source.lkm_coarse_graph",
        }.get(kind, kind)
        return replace(
            bundle_ref,
            kind=kind,
            media_type=media_type,
            metadata={**bundle_ref.metadata, "source_artifact_id": artifact_id, "source_sha256": sha256, "source_kind": source_kind},
        ), entry["value"]
    source_kind = {
        "paper.text": "source.paper_text",
        "claims.cleaned": "source.clean_claims",
        "reasoning_graph.lkm_coarse": "source.lkm_coarse_graph",
    }.get(kind, kind)
    ref = context.require_one(source_kind)
    if source_kind == "source.paper_text":
        return ref, context.artifact_path(ref).read_text(encoding="utf-8")
    payload = read_json_ref(context, ref)
    return ref, payload.get("value", payload)


def slug(value: Any, prefix: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_")
    return f"{prefix}_{normalized or canonical_hash(str(value))[:12]}"


def new_snapshot(context: StageContext, step: int = 1) -> JSONDict:
    return {
        "schema_name": FORMALIZATION_SNAPSHOT_SCHEMA, "schema_version": SCHEMA_VERSION,
        "step": {"number": step, "name": STEP_NAMES[step]},
        "snapshot_id": f"snapshot_{context.run_id}_step_{step}", "parent_artifact_id": None,
        "inputs": [{"artifact_id": ref.artifact_id, "kind": ref.kind, "sha256": ref.sha256} for ref in context.inputs],
        "knowledge": [], "reasoning_units": [], "operators": [], "non_reasoning_links": [], "source_anchors": [],
        "changes": {"added": [], "modified": [], "removed": [], "replaced": []},
        "review": {"status": "needs_review", "issues": []},
    }


def inherit_snapshot(context: StageContext, step: int) -> tuple[ArtifactRef, JSONDict]:
    matches = [ref for ref in context.find_all("formalization") if ref.metadata.get("step") == step - 1]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one Step {step - 1} formalization revision input, found {len(matches)}")
    parent = matches[0]
    document = read_json_ref(context, parent)
    graph = document.get("graph", {})
    workflow = document.get("workflow", {})
    revisions = workflow.get("revisions", [])
    previous = revisions[-1] if isinstance(revisions, list) and revisions else {}
    payload: JSONDict = {
        "schema_name": FORMALIZATION_SNAPSHOT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "knowledge": copy.deepcopy(graph.get("knowledges", [])),
        "reasoning_units": copy.deepcopy(graph.get("strategies", [])),
        "operators": copy.deepcopy(graph.get("operators", [])),
        "non_reasoning_links": copy.deepcopy(workflow.get("non_reasoning_links", [])),
        "source_anchors": copy.deepcopy(workflow.get("source_anchors", [])),
        "workflow_proposals": copy.deepcopy(workflow.get("proposals", [])),
        "changes": {"added": [], "modified": [], "removed": [], "replaced": []},
        "review": copy.deepcopy(previous.get("review", {"status": "needs_review", "issues": []})),
    }
    payload["step"] = {"number": step, "name": STEP_NAMES[step]}
    payload["snapshot_id"] = f"snapshot_{context.run_id}_step_{step}"
    payload["parent_artifact_id"] = parent.artifact_id
    payload["inputs"] = [{"artifact_id": ref.artifact_id, "kind": ref.kind, "sha256": ref.sha256} for ref in context.inputs]
    return parent, payload


def knowledge(
    knowledge_id: str,
    canonical: str,
    *,
    knowledge_type: str = "claim",
    external_ids: list[JSONDict] | None = None,
    anchor_ids: list[str] | None = None,
    first_seen_step: int = 1,
    metadata: Mapping[str, Any] | None = None,
    visibility: str = "public",
) -> JSONDict:
    return {
        "id": knowledge_id, "type": knowledge_type,
        "content": {"canonical": canonical},
        "self_contained": knowledge_type != "claim" or bool(canonical.strip()), "origin": "extracted",
        "visibility": visibility, "source_anchor_ids": list(anchor_ids or []),
        "external_ids": list(external_ids or []),
        "epistemic": {"prior_status": "unset" if knowledge_type == "claim" and visibility == "public" else "not_applicable", "prior_ref": None},
        "first_seen_step": first_seen_step, "metadata": dict(metadata or {}),
    }


def source_anchor(anchor_id: str, artifact_id: str, source_kind: str, pointer: str) -> JSONDict:
    return {
        "anchor_id": anchor_id, "artifact_id": artifact_id, "source_kind": source_kind,
        "locator": {"type": "json_pointer", "pointer": pointer}, "relevance": "source_record",
    }


def _merge_by_id(existing: list[JSONDict], incoming: Any, id_field: str) -> list[JSONDict]:
    if not isinstance(incoming, list) or not all(isinstance(item, dict) and isinstance(item.get(id_field), str) for item in incoming):
        raise ValueError(f"snapshot patch {id_field} collection must be identified objects")
    result = copy.deepcopy(existing)
    positions = {item[id_field]: index for index, item in enumerate(result)}
    for item in incoming:
        item_id = item[id_field]
        if item_id in positions:
            result[positions[item_id]] = copy.deepcopy(item)
        else:
            positions[item_id] = len(result)
            result.append(copy.deepcopy(item))
    return result


def apply_snapshot_patch(payload: JSONDict, normalized: Mapping[str, Any]) -> None:
    patch = normalized.get("snapshot_patch")
    if not isinstance(patch, dict):
        raise ValueError("semantic tool normalized output must contain snapshot_patch")
    collections = (
        ("knowledge", "id"), ("reasoning_units", "id"), ("operators", "id"),
        ("non_reasoning_links", "id"), ("source_anchors", "anchor_id"),
    )
    for field_name, id_field in collections:
        if field_name in patch:
            payload[field_name] = _merge_by_id(payload.get(field_name, []), patch[field_name], id_field)
    for field_name in ("review", "formalization_level", "methodology"):
        if field_name in patch:
            payload[field_name] = copy.deepcopy(patch[field_name])
    payload.setdefault("changes", {}).setdefault("modified", []).extend(
        str(item.get("id") or item.get("anchor_id"))
        for field_name, _ in collections for item in patch.get(field_name, []) if isinstance(item, dict)
    )


def record_revision_history(payload: JSONDict, step: int) -> None:
    """Keep content history on domain objects while retaining one logical graph."""
    collections = (
        ("knowledge", "id"),
        ("reasoning_units", "id"),
        ("operators", "id"),
        ("non_reasoning_links", "id"),
        ("source_anchors", "anchor_id"),
    )
    for field_name, identifier in collections:
        for item in payload.get(field_name, []):
            if not isinstance(item, dict) or not isinstance(item.get(identifier), str):
                continue
            current = copy.deepcopy(item)
            current.pop("revision", None)
            revision = item.get("revision")
            if not isinstance(revision, dict):
                revision = {"introduced_at": step, "updated_at": step, "history": []}
            history = revision.get("history")
            if not isinstance(history, list):
                history = []
            current_hash = canonical_hash(current)
            previous_hash = None
            if history and isinstance(history[-1], dict):
                previous_hash = history[-1].get("content_hash")
            if previous_hash != current_hash:
                history.append({"revision": step, "content_hash": current_hash, "content": current})
            item["revision"] = {
                "introduced_at": int(revision.get("introduced_at", step)),
                "updated_at": step,
                "history": history,
            }


def invoke_optional_tool(
    context: StageContext,
    step: int,
    payload: JSONDict,
    *,
    extra_parameters: Mapping[str, Any] | None = None,
) -> tuple[StageResult | None, list[ArtifactDraft]]:
    spec = context.options.get("tool_plugin")
    if spec is None:
        return None, []
    if not isinstance(spec, str):
        raise ValueError("tool_plugin must use module:object syntax")
    tool = instantiate(spec)
    if not isinstance(tool, DomainTool):
        raise TypeError(f"{spec} does not implement DomainTool")
    if isinstance(tool, ArtifactAwareDomainTool):
        tool.bind_artifacts({
            ref.artifact_id: (context.artifact_path(ref), ref.media_type)
            for ref in context.inputs
        })
    request = ToolCallRequest(
        call_id=f"semantic_step_{step}_{context.run_id}_{context.attempt}",
        tool_name=tool.name, tool_version=tool.version, operation=f"review_step_{step}",
        inputs=[{"artifact_id": ref.artifact_id, "kind": ref.kind, "sha256": ref.sha256} for ref in context.inputs],
        parameters={
            "step": step,
            "snapshot": payload,
            **dict(context.options.get("tool_parameters", {})),
            **dict(extra_parameters or {}),
        },
    )
    try:
        response = tool.invoke(request)
        if not isinstance(response, ToolCallResponse):
            raise TypeError(f"{spec} returned {type(response).__name__}, not ToolCallResponse")
        validate_tool_response(request, response)
    except Exception as exc:
        response = ToolCallResponse(request.call_id, "failed", None, error={"type": type(exc).__name__, "message": str(exc)})
    response_path = context.work_dir / f"semantic_step_{step}_tool_response.json"
    atomic_write_json(response_path, {"request": request.to_dict(), "response": response.to_dict()})
    draft = ArtifactDraft(response_path, "tool.semantic_review.response", "application/json", {
        "schema_version": SCHEMA_VERSION, "step": step, "tool_call_id": request.call_id,
        "tool_name": tool.name, "tool_version": tool.version, "status": response.status,
    })
    if response.status == "failed":
        return StageResult(
            status="failed", artifacts=[draft],
            findings=[Finding(code="SEMANTIC_TOOL_FAILED", severity="error", message=str(response.error.get("message", "semantic tool failed")), details={"tool": tool.name, "call_id": request.call_id, "error": response.error})],
            metadata={"step": step, "tool_call_id": request.call_id},
        ), []
    try:
        apply_snapshot_patch(payload, response.normalized or {})
    except ValueError as exc:
        return StageResult(
            status="failed", artifacts=[draft],
            findings=[Finding(code="SEMANTIC_TOOL_OUTPUT_INVALID", severity="error", message=str(exc), details={"tool": tool.name, "call_id": request.call_id})],
            metadata={"step": step, "tool_call_id": request.call_id},
        ), []
    return None, [draft]


def emit_snapshot(
    context: StageContext,
    step: int,
    payload: JSONDict,
    *,
    extra_artifacts: list[ArtifactDraft] | None = None,
    emit_final_index: bool = False,
) -> StageResult:
    formalization_path = context.work_dir / "formalization.json"
    validation_path = context.work_dir / "formalization_validation.json"
    record_revision_history(payload, step)
    runtime_findings = collect_runtime_anchor_findings(context, payload)
    report = build_validation_report(payload, expected_step=step, additional_findings=runtime_findings)
    payload["validation"] = {
        "report_id": report["report_id"],
        "status": report["summary"]["status"],
        "errors": report["summary"]["errors"],
        "warnings": report["summary"]["warnings"],
    }
    report = build_validation_report(payload, expected_step=step, additional_findings=runtime_findings)
    previous_documents = [ref for ref in context.find_all("formalization") if ref.metadata.get("step") == step - 1]
    previous_document = read_json_ref(context, previous_documents[0]) if len(previous_documents) == 1 else None
    previous_revision = previous_document.get("revision", {}) if isinstance(previous_document, dict) else {}
    previous_workflow = previous_document.get("workflow", {}) if isinstance(previous_document, dict) else {}
    prior_revisions = copy.deepcopy(previous_workflow.get("revisions", [])) if isinstance(previous_workflow, dict) else []
    revision_id = f"revision_{context.run_id}_step_{step}"
    revision_entry = {
        "revision": step,
        "revision_id": revision_id,
        "step": {"number": step, "name": STEP_NAMES[step]},
        "review": copy.deepcopy(payload.get("review", {})),
        "changes": copy.deepcopy(payload.get("changes", {})),
        "active": {
            "knowledge_ids": [item["id"] for item in payload.get("knowledge", [])],
            "strategy_ids": [item["id"] for item in payload.get("reasoning_units", [])],
            "operator_ids": [item["id"] for item in payload.get("operators", [])],
            "non_reasoning_link_ids": [item["id"] for item in payload.get("non_reasoning_links", [])],
            "source_anchor_ids": [item["anchor_id"] for item in payload.get("source_anchors", [])],
        },
    }
    document: JSONDict = {
        "schema_version": SCHEMA_VERSION,
        "revision": {
            "revision_id": revision_id,
            "supersedes": previous_revision.get("revision_id") if isinstance(previous_revision, dict) else None,
            "parent_hash": previous_revision.get("content_hash") if isinstance(previous_revision, dict) else None,
        },
        "package": (copy.deepcopy(previous_document.get("package")) if isinstance(previous_document, dict) else {
            "paper_id": str(context.options.get("paper_id", context.run_id)),
            "namespace": str(context.options.get("namespace", "papers")),
            "name": str(context.options.get("package_name", context.run_id)),
            "version": str(context.options.get("package_version", "1")),
        }),
        "graph": {
            "knowledges": copy.deepcopy(payload.get("knowledge", [])),
            "operators": copy.deepcopy(payload.get("operators", [])),
            "strategies": copy.deepcopy(payload.get("reasoning_units", [])),
            "composes": copy.deepcopy(previous_document.get("graph", {}).get("composes", []) if isinstance(previous_document, dict) else []),
        },
        "workflow": {
            "source_records": copy.deepcopy(previous_workflow.get("source_records", []) if isinstance(previous_workflow, dict) else []),
            "source_anchors": copy.deepcopy(payload.get("source_anchors", [])),
            "proposals": copy.deepcopy(payload.get("workflow_proposals", previous_workflow.get("proposals", []) if isinstance(previous_workflow, dict) else [])),
            "gaps": copy.deepcopy(previous_workflow.get("gaps", []) if isinstance(previous_workflow, dict) else []),
            "non_reasoning_links": copy.deepcopy(payload.get("non_reasoning_links", [])),
            "revisions": [*prior_revisions, revision_entry],
        },
    }
    from .authoring import formalization_content_hash
    document["revision"]["content_hash"] = formalization_content_hash(document)
    atomic_write_json(formalization_path, document)
    atomic_write_json(validation_path, report)
    validation_draft = ArtifactDraft(validation_path, VALIDATION_KIND, "application/json", {
        "schema_name": VALIDATION_SCHEMA, "schema_version": SCHEMA_VERSION,
        "logical_name": validation_path.name, "step": step, "revision_id": revision_id,
        "validation_status": report["summary"]["status"], "error_count": report["summary"]["errors"],
        "warning_count": report["summary"]["warnings"],
    })
    formalization_metadata = {
        "schema_name": "gaia.formalization", "schema_version": SCHEMA_VERSION,
        "logical_name": formalization_path.name, "step": step, "step_name": STEP_NAMES[step],
        "revision_id": revision_id, "parent_artifact_id": payload.get("parent_artifact_id"),
        "validation_status": report["summary"]["status"],
    }
    if report["summary"]["status"] == "failed":
        snapshot_draft = ArtifactDraft(formalization_path, "formalization.invalid", "application/json", formalization_metadata)
        findings = [Finding(
            code="DOMAIN_CONTRACT_INVALID", severity="error", message=item["message"],
            details={"step": step, "validation_finding": item},
        ) for item in report["findings"] if item["severity"] == "error"]
        return StageResult(
            status="failed", artifacts=[snapshot_draft, validation_draft, *(extra_artifacts or [])],
            findings=findings, metadata={"step": step, "validation_report_id": report["report_id"]},
        )

    index_drafts: list[ArtifactDraft] = []
    if emit_final_index:
        try:
            index = build_knowledge_index(payload)
            validate_knowledge_index(index)
        except ValueError as exc:
            return StageResult(
                status="failed", artifacts=[validation_draft, *(extra_artifacts or [])],
                findings=[Finding(code="KNOWLEDGE_INDEX_INVALID", severity="error", message=str(exc), details={"step": step})],
                metadata={"step": step, "validation_report_id": report["report_id"]},
            )
        index_path = context.work_dir / "knowledge_index.json"
        atomic_write_json(index_path, index)
        index_drafts.append(ArtifactDraft(index_path, "knowledge.index", "application/json", {
            "schema_name": "gaia.knowledge.index", "schema_version": SCHEMA_VERSION,
            "logical_name": index_path.name, "step": step, "revision_id": revision_id,
            "final": True,
        }))
    return StageResult(
        status="succeeded",
        artifacts=[
            ArtifactDraft(formalization_path, "formalization", "application/json", formalization_metadata),
            *index_drafts,
            validation_draft,
            *(extra_artifacts or []),
        ],
        metadata={
            "step": step, "knowledge_count": len(payload.get("knowledge", [])),
            "review_status": payload.get("review", {}).get("status"),
            "validation_status": report["summary"]["status"],
        },
    )
