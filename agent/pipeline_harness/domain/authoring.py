from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from ..models import JSONDict
from .contracts import SCHEMA_VERSION, canonical_hash


TOP_LEVEL_FIELDS = {"schema_version", "revision", "package", "graph", "workflow"}
GRAPH_FIELDS = {"knowledges", "operators", "strategies", "composes"}
WORKFLOW_FIELDS = {"source_records", "source_anchors", "proposals", "gaps"}
PROPOSAL_TYPES = {
    "new_observation", "relation", "weakpoint", "formalization", "warrant_review",
}
def formalization_content_hash(document: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(document))
    revision = payload.get("revision")
    if isinstance(revision, dict):
        revision.pop("content_hash", None)
    return canonical_hash(payload)


def _identified(items: Any, field: str, label: str) -> tuple[list[JSONDict], set[str]]:
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(f"formalization {label} must be a list of objects")
    identifiers = [item.get(field) for item in items]
    if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
        raise ValueError(f"formalization {label} require non-empty {field}")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"formalization {label} contain duplicate {field}")
    return items, set(identifiers)


def validate_formalization(document: Mapping[str, Any]) -> None:
    if set(document) != TOP_LEVEL_FIELDS or document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("formalization must contain only schema_version, revision, package, graph, and workflow")
    revision = document.get("revision")
    package = document.get("package")
    graph = document.get("graph")
    workflow = document.get("workflow")
    if not all(isinstance(item, dict) for item in (revision, package, graph, workflow)):
        raise ValueError("formalization revision/package/graph/workflow must be objects")
    assert isinstance(revision, dict) and isinstance(package, dict) and isinstance(graph, dict) and isinstance(workflow, dict)
    if not isinstance(revision.get("revision_id"), str) or not revision["revision_id"]:
        raise ValueError("formalization revision_id is required")
    if revision.get("content_hash") != formalization_content_hash(document):
        raise ValueError("formalization revision content_hash is invalid")
    if any(not isinstance(package.get(field), str) or not package[field] for field in ("paper_id", "namespace", "name", "version")):
        raise ValueError("formalization package fields must be non-empty strings")
    revisioned_workflow_fields = {*WORKFLOW_FIELDS, "non_reasoning_links", "revisions"}
    if set(graph) != GRAPH_FIELDS or (set(workflow) != WORKFLOW_FIELDS and set(workflow) != revisioned_workflow_fields):
        raise ValueError("formalization graph/workflow fields do not match the supported contract")
    if "revisions" in workflow:
        if not isinstance(workflow["revisions"], list) or not workflow["revisions"]:
            raise ValueError("revisioned formalization requires a non-empty workflow.revisions list")
        if not isinstance(workflow.get("non_reasoning_links"), list):
            raise ValueError("revisioned formalization requires workflow.non_reasoning_links")
        return

    knowledges, knowledge_ids = _identified(graph["knowledges"], "id", "knowledges")
    anchors, anchor_ids = _identified(workflow["source_anchors"], "id", "source_anchors")
    proposals, _ = _identified(workflow["proposals"], "id", "proposals")
    if not isinstance(graph["operators"], list) or not isinstance(graph["strategies"], list) or not isinstance(graph["composes"], list):
        raise ValueError("formalization graph collections must be lists")
    if not isinstance(workflow["source_records"], list) or not isinstance(workflow["gaps"], list):
        raise ValueError("formalization workflow collections must be lists")

    for item in knowledges:
        if not isinstance(item.get("content"), str):
            raise ValueError(f"knowledge {item['id']} content must be canonical text")
        referenced_anchors = item.get("source_anchor_ids", [])
        if not isinstance(referenced_anchors, list) or set(referenced_anchors) - anchor_ids:
            raise ValueError(f"knowledge {item['id']} has unresolved source anchors")
    for anchor in anchors:
        if not isinstance(anchor.get("artifact_id"), str) or not isinstance(anchor.get("locator"), dict):
            raise ValueError(f"source anchor {anchor['id']} is incomplete")

    def validate_operator(operator: Any, owner: str) -> None:
        if not isinstance(operator, dict) or not isinstance(operator.get("operator"), str):
            raise ValueError(f"{owner} contains an invalid operator")
        variables = operator.get("variables")
        conclusion = operator.get("conclusion")
        if not isinstance(variables, list) or any(value not in knowledge_ids for value in variables):
            raise ValueError(f"{owner} operator has unresolved variables")
        if not isinstance(conclusion, str) or conclusion not in knowledge_ids:
            raise ValueError(f"{owner} operator has an unresolved conclusion")

    for operator in graph["operators"]:
        validate_operator(operator, "top-level graph")

    strategy_ids: set[str] = set()
    for strategy in graph["strategies"]:
        if not isinstance(strategy, dict) or not isinstance(strategy.get("strategy_id"), str):
            raise ValueError("formalization strategies require strategy_id")
        if strategy["strategy_id"] in strategy_ids:
            raise ValueError("formalization strategies contain duplicate strategy_id")
        strategy_ids.add(strategy["strategy_id"])
        references = [*strategy.get("premises", []), *strategy.get("background", []), strategy.get("conclusion")]
        if any(not isinstance(ref, str) or ref not in knowledge_ids for ref in references):
            raise ValueError(f"strategy {strategy['strategy_id']} has unresolved knowledge references")
        formal_operators = strategy.get("formal_expr", {}).get("operators")
        if not isinstance(formal_operators, list):
            raise ValueError(f"strategy {strategy['strategy_id']} requires embedded formal_expr operators")
        for operator in formal_operators:
            validate_operator(operator, f"strategy {strategy['strategy_id']}")

    for proposal in proposals:
        if proposal.get("proposal_type") not in PROPOSAL_TYPES or not isinstance(proposal.get("payload"), dict):
            raise ValueError(f"proposal {proposal['id']} has an invalid type or payload")
        review = proposal.get("review")
        if review is not None and (
            not isinstance(review, dict) or review.get("decision") not in {"approved", "rejected"}
        ):
            raise ValueError(f"proposal {proposal['id']} review must be null, approved, or rejected")
