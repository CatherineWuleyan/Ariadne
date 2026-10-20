from __future__ import annotations

import json
from typing import Any, Mapping

from ..models import ArtifactRef, JSONDict
from ..store import RunStore
from ..view.model import ViewDocument
from .contracts import (
    FORMALIZATION_SNAPSHOT_KIND,
    INVALID_FORMALIZATION_SNAPSHOT_KIND,
    VALIDATION_KIND,
)
from .validation import validate_snapshot


def _node_id(step: int, entity: str, value: str) -> str:
    return f"step:{step}:{entity}:{value}"


def _load(store: RunStore, ref: ArtifactRef) -> JSONDict:
    with store.artifact_path(ref).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{ref.artifact_id} must contain an object")
    return value


def _knowledge_presentation(item: JSONDict) -> tuple[str, list[str], int, int]:
    visibility = item.get("visibility", "public")
    item_id = str(item.get("id", ""))
    if visibility == "formal_internal":
        rank = 1 if item_id.startswith("OR") else 3
        return "helpers", ["standard"], rank, 50
    return "claims", ["overview", "standard"], 2, 60


def _operator_rank(operator_type: str) -> int:
    return {"disjunction": 1, "equivalence": 2, "conjunction": 3, "implication": 3, "contradiction": 4}.get(operator_type, 3)


def _operator_symbol(operator_type: str) -> str:
    return {
        "implication": "→",
        "equivalence": "↔",
        "contradiction": "⊗",
        "complement": "⊕",
        "conjunction": "∧",
        "disjunction": "∨",
        "negation": "¬",
    }.get(operator_type, operator_type)


def _knowledge_display(item: JSONDict) -> tuple[str, str]:
    if item.get("title"):
        label = str(item["title"])
    else:
        label = ""
        for external in item.get("external_ids", []):
            if not isinstance(external, dict):
                continue
            if external.get("system") == "clean_claims" and external.get("id"):
                label = str(external["id"])
                break
        if not label:
            for external in item.get("external_ids", []):
                if not isinstance(external, dict) or external.get("system") != "lkm" or not external.get("id"):
                    continue
                source_id = str(external["id"])
                label = f"LKM · {source_id.rsplit('::', 1)[-1].replace('_', ' ')}"
                break
        item_id = str(item.get("id", ""))
        label = label or (item_id.removeprefix("claim_") if item_id.startswith("claim_") else item_id)
    return label, str(item.get("type", "knowledge"))


def _reasoning_state(strategy: JSONDict) -> str:
    if strategy.get("form") == "formal":
        return "confirmed"
    necessity_status = ((strategy.get("coarse") or {}).get("necessity_test") or {}).get("status")
    return "confirmed" if necessity_status == "confirmed" else "candidate"


def _weakpoint_index(formalization: JSONDict | None) -> dict[str, JSONDict]:
    if not formalization:
        return {}
    result: dict[str, JSONDict] = {}
    workflow = formalization.get("workflow", {})
    for proposal in workflow.get("proposals", []):
        if not isinstance(proposal, dict) or proposal.get("proposal_type") != "weakpoint":
            continue
        proposal_id = str(proposal.get("id", ""))
        prefix = "proposal_weakpoint_"
        if not proposal_id.startswith(prefix):
            continue
        result[proposal_id.removeprefix(prefix)] = proposal
    return result


def _weakpoint_label(strategy: JSONDict, weakpoint: JSONDict | None) -> str:
    payload = (weakpoint or {}).get("payload", {})
    reasoning_type = str(payload.get("reasoning_type") or strategy.get("type", "reasoning"))
    gap_type = payload.get("gap_type")
    return f"{reasoning_type} · {gap_type}" if gap_type else reasoning_type


class FormalizationViewAdapter:
    version = "3"

    def project(
        self,
        store: RunStore,
        artifacts: list[ArtifactRef],
        options: Mapping[str, Any] | None = None,
    ) -> ViewDocument:
        artifact_map = {artifact.artifact_id: artifact for artifact in artifacts}
        formalization_refs = [artifact for artifact in artifacts if artifact.kind in {"formalization", "formalization.invalid"}]
        formalization_refs.sort(key=lambda item: int(item.metadata.get("step", 0)))
        final_formalization = _load(store, formalization_refs[-1]) if formalization_refs else None

        def revision_payload(document: JSONDict, revision: JSONDict) -> JSONDict:
            graph = document.get("graph", {})
            workflow = document.get("workflow", {})
            active = revision.get("active", {})
            def selected(items: Any, id_field: str, active_key: str) -> list[JSONDict]:
                ids = set(active.get(active_key, []))
                return [dict(item) for item in items if isinstance(item, dict) and item.get(id_field) in ids]
            step = revision.get("step", {})
            return {
                "snapshot_id": revision.get("revision_id"), "step": step,
                "knowledge": selected(graph.get("knowledges", []), "id", "knowledge_ids"),
                "reasoning_units": selected(graph.get("strategies", []), "id", "strategy_ids"),
                "operators": selected(graph.get("operators", []), "id", "operator_ids"),
                "non_reasoning_links": selected(workflow.get("non_reasoning_links", []), "id", "non_reasoning_link_ids"),
                "source_anchors": selected(workflow.get("source_anchors", []), "anchor_id", "source_anchor_ids"),
                "review": revision.get("review", {}),
            }

        revision_views: list[tuple[ArtifactRef, JSONDict, JSONDict]] = []
        for ref in formalization_refs:
            document = _load(store, ref)
            revisions = document.get("workflow", {}).get("revisions", [])
            if isinstance(revisions, list) and revisions:
                revision = revisions[-1]
                if isinstance(revision, dict):
                    revision_views.append((ref, revision_payload(document, revision), revision))
        weakpoint_by_strategy = _weakpoint_index(final_formalization)
        validation_refs = [artifact for artifact in artifacts if artifact.kind == VALIDATION_KIND]
        validation_by_step = {
            int(report.get("step")): (ref, report)
            for ref in validation_refs
            for report in [_load(store, ref)] if isinstance(report.get("step"), int)
        }
        nodes: list[JSONDict] = []
        edges: list[JSONDict] = []
        documents: list[JSONDict] = []
        stages: list[JSONDict] = []
        source_targets: dict[str, JSONDict] = {}
        projected_findings: list[tuple[int, JSONDict]] = []

        for ref, snapshot, revision in revision_views:
            step = int(snapshot["step"]["number"])
            validation_ref, validation = validation_by_step.get(step, (None, {"summary": {"status": "unknown", "errors": 0, "warnings": 0}, "findings": []}))
            summary = validation.get("summary", {})
            stages.append({
                "number": step, "name": snapshot["step"]["name"], "artifact_id": ref.artifact_id,
                "revision_id": revision.get("revision_id"), "review_status": snapshot.get("review", {}).get("status", "unknown"),
                "revision": revision.get("revision", step),
                "validation_status": summary.get("status", "unknown"), "validation_errors": summary.get("errors", 0),
                "validation_warnings": summary.get("warnings", 0),
                "validation_artifact_id": validation_ref.artifact_id if validation_ref else None,
            })
            projected_findings.extend((step, finding) for finding in validation.get("findings", []) if isinstance(finding, dict))
            knowledge = {item["id"]: item for item in snapshot.get("knowledge", [])}

            def ensure_knowledge_node(knowledge_id: str) -> str:
                node_id = _node_id(step, "knowledge", knowledge_id)
                if not any(node["id"] == node_id for node in nodes):
                    nodes.append({
                        "id": node_id, "entity_id": knowledge_id, "label": f"missing: {knowledge_id}",
                        "kind": "missing_reference", "layer": "validation", "step": step,
                        "visible_at": ["overview", "standard"], "min_granularity": "overview",
                        "summary": "Referenced object is missing", "details": {"missing": True, "knowledge_id": knowledge_id},
                        "validation_status": "failed",
                    })
                return node_id

            for item in knowledge.values():
                visibility = item.get("visibility", "public")
                layer, visible_at, layout_rank, layout_order = _knowledge_presentation(item)
                display_label, display_meta = _knowledge_display(item)
                node_id = _node_id(step, "knowledge", item["id"])
                nodes.append({
                    "id": node_id,
                    "entity_id": item["id"],
                    "label": item.get("title") or item["id"],
                    "display_label": display_label,
                    "display_meta": display_meta,
                    "kind": item["type"],
                    "layer": layer,
                    "step": step,
                    "revision": item.get("revision", {"introduced_at": step, "updated_at": step, "history": []}),
                    "visible_at": visible_at,
                    "min_granularity": "standard" if visibility == "formal_internal" else "overview",
                    "layout_rank": layout_rank,
                    "layout_order": layout_order,
                    "summary": item.get("content", {}).get("canonical", ""),
                    "source_anchor_ids": item.get("source_anchor_ids", []),
                    "details": item,
                })
                documents.append({
                    "id": f"search:{step}:{item['id']}",
                    "title": item.get("title") or item["id"],
                    "text": " ".join(filter(None, [item.get("content", {}).get("canonical"), item.get("content", {}).get("en"), item.get("content", {}).get("zh"), *[f"{external.get('system')}:{external.get('id')}" for external in item.get("external_ids", []) if isinstance(external, dict)]])),
                    "tags": [item["type"], f"step-{step}", *[str(external.get("id")) for external in item.get("external_ids", []) if isinstance(external, dict) and external.get("id")]],
                    "refs": [node_id],
                    "metadata": {"step": step, "knowledge_id": item["id"], "source_anchor_ids": item.get("source_anchor_ids", [])},
                })

            for strategy in snapshot.get("reasoning_units", []):
                strategy_node = _node_id(step, "strategy", strategy["id"])
                coarse_classification = str((strategy.get("coarse") or {}).get("classification", ""))
                strategy_layer = "weakpoints" if strategy.get("form") == "coarse" and coarse_classification in {"weakpoint", "scope_alignment", "conditional_tension", "deduction_candidate"} else "reasoning"
                weakpoint = weakpoint_by_strategy.get(str(strategy["id"]))
                interface_items = [knowledge[item_id] for item_id in [*strategy.get("premises", []), strategy.get("conclusion")] if item_id in knowledge]
                interface_ranks = [_knowledge_presentation(item)[2] for item in interface_items]
                strategy_rank = round(sum(interface_ranks) / len(interface_ranks)) if interface_ranks else 2
                reasoning_state = _reasoning_state(strategy)
                coarse_weakpoint = strategy.get("form") == "coarse" and coarse_classification in {"weakpoint", "scope_alignment", "conditional_tension", "deduction_candidate"}
                fold_group = strategy.get("projection", {}).get("fold_group", f"fold_{strategy['id']}")
                strategy_details = dict(strategy)
                if weakpoint:
                    strategy_details["weakpoint"] = weakpoint
                nodes.append({
                    "id": strategy_node,
                    "entity_id": strategy["id"],
                    "label": f"↝ {strategy['id']}" if coarse_weakpoint else f"{strategy.get('type', 'strategy')}: {strategy['id']}",
                    "display_label": strategy["id"],
                    "display_meta": coarse_classification.replace("_", " ") or strategy.get("type", "strategy"),
                    "kind": "strategy",
                    "layer": strategy_layer,
                    "step": step,
                    "revision": strategy.get("revision", {"introduced_at": step, "updated_at": step, "history": []}),
                    "visible_at": ["overview"] if coarse_weakpoint else ["standard"],
                    "min_granularity": "overview" if coarse_weakpoint else "standard",
                    "summary": strategy.get("projection", {}).get("summary_label", f"{strategy.get('type')} reasoning"),
                    "fold_group": fold_group,
                    "layout_rank": strategy_rank,
                    "layout_order": 50,
                    "details": strategy_details,
                })
                conclusion_node = ensure_knowledge_node(strategy["conclusion"])
                for premise_index, premise in enumerate(strategy.get("premises", [])):
                    premise_node = ensure_knowledge_node(premise)
                    semantic_id = f"strategy:{strategy['id']}:premise:{premise_index}"
                    if not coarse_weakpoint:
                        edges.append({"id": f"summary:{step}:{strategy['id']}:{premise_index}", "semantic_id": semantic_id, "source": premise_node, "target": conclusion_node, "label": strategy.get("type", "supports"), "layer": "reasoning", "edge_class": "reasoning", "reasoning_state": reasoning_state, "semantic_type": strategy.get("type"), "step": step, "visible_at": ["overview"], "min_granularity": "overview", "fold_group": fold_group, "details": {"strategy_id": strategy["id"], "collapsed": True, "reasoning_state": reasoning_state}})
                    formal_edge = strategy.get("form") == "formal"
                    edges.append({"id": f"premise:{step}:{strategy['id']}:{premise_index}", "semantic_id": semantic_id, "source": premise_node, "target": strategy_node, "label": "premise", "layer": "reasoning" if formal_edge else "weakpoints", "edge_class": "reasoning" if formal_edge else "non_reasoning", "reasoning_state": "confirmed" if formal_edge else "candidate", "semantic_type": "premise", "step": step, "visible_at": ["overview"] if coarse_weakpoint else ["standard"], "min_granularity": "overview" if coarse_weakpoint else "standard", "fold_group": fold_group, "details": {"reasoning_state": reasoning_state, "weakpoint": weakpoint}})
                formal_edge = strategy.get("form") == "formal"
                edges.append({"id": f"conclusion:{step}:{strategy['id']}", "semantic_id": f"strategy:{strategy['id']}:conclusion", "source": strategy_node, "target": conclusion_node, "label": "conclusion", "layer": "reasoning" if formal_edge else "weakpoints", "edge_class": "reasoning" if formal_edge else "non_reasoning", "reasoning_state": "confirmed" if formal_edge else "candidate", "semantic_type": "conclusion", "step": step, "visible_at": ["overview"] if coarse_weakpoint else ["standard"], "min_granularity": "overview" if coarse_weakpoint else "standard", "fold_group": fold_group, "details": {"reasoning_state": reasoning_state, "weakpoint": weakpoint}})
                if coarse_weakpoint:
                    for background_index, background in enumerate(strategy.get("background", [])):
                        background_item = knowledge.get(background, {})
                        if background_item.get("type") == "setting":
                            continue
                        edges.append({"id": f"background:{step}:{strategy['id']}:{background_index}", "semantic_id": f"strategy:{strategy['id']}:background:{background_index}", "source": ensure_knowledge_node(background), "target": strategy_node, "label": "background", "layer": "weakpoints", "edge_class": "non_reasoning", "reasoning_state": "candidate", "semantic_type": "background", "step": step, "visible_at": ["overview"], "min_granularity": "overview", "fold_group": fold_group, "details": {"strategy_id": strategy["id"], "background": True}})

            for operator in snapshot.get("operators", []):
                operator_node = _node_id(step, "operator", operator["id"])
                operator_type = str(operator["type"])
                nodes.append({"id": operator_node, "entity_id": operator["id"], "label": operator_type, "display_label": _operator_symbol(operator_type), "display_meta": operator_type, "kind": "operator", "layer": "operators", "step": step, "revision": operator.get("revision", {"introduced_at": step, "updated_at": step, "history": []}), "visible_at": ["standard"], "min_granularity": "standard", "layout_rank": _operator_rank(operator_type), "layout_order": 40, "summary": operator.get("metadata", {}).get("canonical_name", operator_type), "details": operator})
                for variable_index, variable in enumerate(operator.get("variables", [])):
                    edges.append({"id": f"operator-in:{step}:{operator['id']}:{variable_index}", "semantic_id": f"operator:{operator['id']}:input:{variable_index}", "source": ensure_knowledge_node(variable), "target": operator_node, "label": "input", "layer": "operators", "edge_class": "reasoning", "reasoning_state": "confirmed", "semantic_type": operator["type"], "step": step, "visible_at": ["standard"], "min_granularity": "standard", "details": {"reasoning_state": "confirmed"}})
                edges.append({"id": f"operator-out:{step}:{operator['id']}", "semantic_id": f"operator:{operator['id']}:output", "source": operator_node, "target": ensure_knowledge_node(operator["conclusion"]), "label": "result", "layer": "operators", "edge_class": "reasoning", "reasoning_state": "confirmed", "semantic_type": operator["type"], "step": step, "visible_at": ["standard"], "min_granularity": "standard", "details": {"reasoning_state": "confirmed"}})

            for link in snapshot.get("non_reasoning_links", []):
                link_type = link["link_type"]
                layer = "scope" if "scope" in link_type else "registry"
                edges.append({"id": f"link:{step}:{link['id']}", "semantic_id": link["id"], "source": ensure_knowledge_node(link["source"]), "target": ensure_knowledge_node(link["target"]), "label": link_type, "layer": layer, "edge_class": "non_reasoning", "semantic_type": link_type, "step": step, "visible_at": ["overview", "standard"], "min_granularity": "overview", "details": link})

            for anchor in snapshot.get("source_anchors", []):
                source_ref = artifact_map.get(anchor["artifact_id"])
                target = {
                    "anchor_id": anchor["anchor_id"],
                    "step": step,
                    "artifact_id": anchor["artifact_id"],
                    "artifact_path": source_ref.path if source_ref else None,
                    "href": f"../{source_ref.path}" if source_ref else None,
                    "locator": anchor["locator"],
                    "source_kind": anchor["source_kind"],
                    "quote": anchor.get("quote"),
                    "relevance": anchor.get("relevance"),
                }
                source_targets[f"{step}:{anchor['anchor_id']}"] = target

        fold_memberships: dict[str, set[str]] = {}

        def add_fold_member(item_id: str, fold_group: str) -> None:
            fold_memberships.setdefault(item_id, set()).add(fold_group)

        for item in [*nodes, *edges]:
            if item.get("fold_group"):
                add_fold_member(str(item["id"]), str(item["fold_group"]))
        for strategy_node in [node for node in nodes if node.get("kind") == "strategy" and node.get("fold_group")]:
            fold_group = str(strategy_node["fold_group"])
            formal = strategy_node.get("details", {}).get("formal") or {}
            for private_claim in formal.get("private_claims", []):
                add_fold_member(_node_id(int(strategy_node["step"]), "knowledge", str(private_claim)), fold_group)
            for operator_id in formal.get("operator_ids", []):
                operator_id = str(operator_id)
                add_fold_member(_node_id(int(strategy_node["step"]), "operator", operator_id), fold_group)
                semantic_prefix = f"operator:{operator_id}:"
                for edge in edges:
                    if edge.get("step") == strategy_node.get("step") and str(edge.get("semantic_id", "")).startswith(semantic_prefix):
                        add_fold_member(str(edge["id"]), fold_group)
        for item in [*nodes, *edges]:
            memberships = fold_memberships.get(str(item["id"]))
            if memberships:
                item["fold_groups"] = sorted(memberships)

        node_by_target: dict[tuple[int, str, str], JSONDict] = {}
        node_type_map = {"reasoning_unit": "strategy", "knowledge": "knowledge", "operator": "operator", "source_anchor": "source"}
        for node in nodes:
            entity_id = node.get("entity_id")
            if entity_id is None:
                continue
            for target_type, entity_type in node_type_map.items():
                if f":{entity_type}:" in node["id"]:
                    node_by_target[(int(node.get("step", 0)), target_type, str(entity_id))] = node
        edge_by_target = {
            (int(edge.get("step", 0)), str(edge.get("semantic_id"))): edge
            for edge in edges if edge.get("semantic_id")
        }
        for step, finding in projected_findings:
            target = finding.get("target", {})
            target_type, target_id = str(target.get("type", "snapshot")), str(target.get("id", "unknown"))
            severity = str(finding.get("severity", "error"))
            finding_node_id = _node_id(step, "validation", str(finding.get("finding_id", target_id)))
            finding_node = {
                "id": finding_node_id, "entity_id": finding.get("finding_id"),
                "label": f"{'error' if severity == 'error' else 'warning'}: {finding.get('rule')}",
                "kind": "validation_finding", "layer": "validation", "step": step,
                "visible_at": ["overview", "standard"], "min_granularity": "overview",
                "summary": finding.get("message", ""), "details": finding,
                "validation_status": "failed" if severity == "error" else "warning",
            }
            nodes.append(finding_node)
            documents.append({
                "id": f"validation:{step}:{finding.get('finding_id')}", "title": str(finding.get("rule", "validation")),
                "text": " ".join([str(finding.get("message", "")), target_type, target_id, str(finding.get("suggested_action", ""))]),
                "tags": ["validation", severity, target_type, target_id, f"step-{step}"],
                "refs": [finding_node_id], "metadata": {"step": step, "finding_id": finding.get("finding_id")},
            })
            target_node = node_by_target.get((step, target_type, target_id))
            target_edge = edge_by_target.get((step, target_id)) if target_type == "edge" else None
            affected = target_node or target_edge
            if affected is not None:
                affected.setdefault("validation_findings", []).append(finding)
                affected["validation_status"] = "failed" if severity == "error" else affected.get("validation_status", "warning")
            link_target = target_node["id"] if target_node else target_edge["target"] if target_edge else None
            if link_target:
                edges.append({
                    "id": f"validation-link:{step}:{finding.get('finding_id')}",
                    "source": finding_node_id, "target": link_target, "label": str(finding.get("rule", "validation")),
                    "layer": "validation", "edge_class": "validation", "semantic_type": "validation",
                    "step": step, "visible_at": ["overview", "standard"], "min_granularity": "overview",
                    "details": {"finding": finding, "target_edge_id": target_edge.get("id") if target_edge else None},
                    "validation_status": "failed" if severity == "error" else "warning",
                })

        return ViewDocument(
            title="Automated paper formalization review",
            source_artifacts=[ref.artifact_id for ref in [*validation_refs, *formalization_refs]],
            nodes=nodes,
            edges=edges,
            search_documents=documents,
            layers=[
                {"id": "claims", "label": "Claims", "default_visible": True},
                {"id": "reasoning", "label": "Reasoning", "default_visible": True},
                {"id": "operators", "label": "Operators", "default_visible": True},
                {"id": "helpers", "label": "Helpers", "default_visible": True},
                {"id": "weakpoints", "label": "Weakpoints", "default_visible": True},
                {"id": "registry", "label": "Registry", "default_visible": True},
                {"id": "scope", "label": "Scope", "default_visible": True},
                {"id": "validation", "label": "Validation issues", "default_visible": False},
            ],
            stages=stages,
            source_targets=list(source_targets.values()),
            metadata={"latest_step": max((stage["number"] for stage in stages), default=None), "supports_step_history": True, "supports_folded_reasoning": True, "supports_validation_targets": True, "supports_dragging": True, "layout": "dagre_layered_with_ranked_fallback", "edge_style_basis": "semantic_class_and_confirmation"},
        )
