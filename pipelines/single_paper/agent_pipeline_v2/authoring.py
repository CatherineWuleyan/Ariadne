"""V2 authoring contract: a knowledge registry plus graph membership references."""
from __future__ import annotations

import copy
from typing import Any, Mapping

from pipeline_harness.domain.contracts import canonical_hash
from pipeline_harness.models import Finding, JSONDict
from pipeline_harness.plugins import ArtifactDraft, StageContext, StageResult
from pipeline_harness.store import atomic_write_json


SCHEMA_VERSION = "1.1.0"
TOP_LEVEL_FIELDS = {"schema_version", "revision", "package", "graph", "knowledges", "workflow"}
GRAPH_FIELDS = {"nodes", "operators", "strategies", "composes"}
STRATEGY_FIELDS = {"scope", "type", "premises", "conclusion", "background"}
REASONING_TYPES = {"deduction", "abduction", "analogy"}
WORKFLOW_FIELDS = {"source_records", "source_anchors", "weakpoints", "gaps", "non_reasoning_links", "revisions"}
KNOWLEDGE_TYPES = {
    "claim",
    "note",
    "observation_claim",
}
OPERATOR_TYPES = {
    "implication", "negation", "conjunction", "disjunction",
    "equivalence", "contradiction", "complement",
}
OPERATOR_FIELDS = {"id", "type", "variables", "conclusion", "background", "metadata"}


def content_hash(document: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(document))
    if isinstance(payload.get("revision"), dict):
        payload["revision"].pop("content_hash", None)
    return canonical_hash(payload)


def weakpoint_target_ids(payload: Mapping[str, Any]) -> list[str]:
    """Read canonical multi-target weakpoints and legacy scalar revisions."""
    value = payload.get("target_claim_id")
    if isinstance(value, str):
        targets = [value]
    elif isinstance(value, list):
        targets = value
    else:
        raise ValueError("weakpoint target_claim_id must be a claim ID or a non-empty list of claim IDs")
    if (not targets or any(not isinstance(item, str) or not item for item in targets)
            or len(targets) != len(set(targets))):
        raise ValueError("weakpoint target_claim_id must contain unique non-empty claim IDs")
    return list(targets)


def canonical_strategy(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the named-leaf subset and let official Gaia assign its ID.

    No local ID algorithm or Operator lowering: use a runtime with Gaia installed.
    """
    if set(payload) not in (STRATEGY_FIELDS, STRATEGY_FIELDS | {"strategy_id"}):
        raise ValueError("strategy requires scope, type, premises, conclusion and background")
    premises, conclusion, background = (payload.get(key) for key in ("premises", "conclusion", "background"))
    if (payload.get("scope") != "local" or payload.get("type") not in REASONING_TYPES | {"infer"}
            or not isinstance(premises, list) or not premises
            or any(not isinstance(key, str) or not key for key in premises)
            or len(premises) != len(set(premises))
            or not isinstance(conclusion, str) or not conclusion or conclusion in premises):
        raise ValueError("strategy requires local scope, a supported type and distinct claim inputs/output")
    # Reasoning arity is not intrinsically binary.  Abduction may combine
    # several observations (and optionally an alternative explanation), while
    # analogy may require several bridge/source premises.  Keep only the
    # semantic minimums here; downstream formalization creates conjunctions.
    if payload["type"] == "abduction" and len(premises) < 1:
        raise ValueError("abduction requires at least one premise")
    if payload["type"] == "analogy" and len(premises) < 2:
        raise ValueError("analogy requires at least two ordered premises")
    if (not isinstance(background, list) or any(not isinstance(key, str) or not key for key in background)
            or len(background) != len(set(background))):
        raise ValueError("strategy.background must be a list of unique Knowledge note IDs")
    try:
        from gaia.engine.ir.strategy import Strategy
    except ImportError as exc:
        raise RuntimeError("Step 4 strategies require the official Gaia package in the active Python runtime") from exc
    result = Strategy.model_validate({key: payload[key] for key in STRATEGY_FIELDS}).model_dump(mode="json", exclude_none=True)
    if "strategy_id" in payload and payload["strategy_id"] != result["strategy_id"]:
        raise ValueError("strategy_id does not match official Gaia's derived ID")
    return result


def validate(document: Mapping[str, Any]) -> None:
    if set(document) != TOP_LEVEL_FIELDS or document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("V2 formalization fields do not match the contract")
    revision, package, graph, knowledges, workflow = (document.get(key) for key in ("revision", "package", "graph", "knowledges", "workflow"))
    if not all(isinstance(value, dict) for value in (revision, package, graph, knowledges, workflow)):
        raise ValueError("revision, package, graph, knowledges, and workflow must be objects")
    # Read old revisions without rewriting their content or hash.
    if set(graph) not in (GRAPH_FIELDS, GRAPH_FIELDS - {"strategies"}) or set(workflow) != WORKFLOW_FIELDS:
        raise ValueError("V2 graph or workflow fields do not match the contract")
    if not isinstance(revision.get("revision_id"), str) or revision.get("content_hash") != content_hash(document):
        raise ValueError("V2 revision is invalid")
    if any(not isinstance(package.get(key), str) or not package[key] for key in ("paper_id", "namespace", "name", "version")):
        raise ValueError("V2 package fields must be non-empty strings")
    nodes = graph["nodes"]
    if not isinstance(nodes, list) or not all(isinstance(item, str) and item for item in nodes) or len(nodes) != len(set(nodes)):
        raise ValueError("graph.nodes must be unique non-empty Knowledge IDs")
    if not isinstance(graph["operators"], list) or not all(isinstance(item, dict) for item in graph["operators"]):
        raise ValueError("graph.operators must be an array of objects")
    strategies = graph.get("strategies", [])
    if not isinstance(strategies, list) or not all(isinstance(item, dict) for item in strategies):
        raise ValueError("graph.strategies must be an array of objects")
    strategy_ids: set[str] = set()
    for strategy in strategies:
        canonical = canonical_strategy(strategy)
        if strategy.get("strategy_id") != canonical["strategy_id"] or canonical["strategy_id"] in strategy_ids:
            raise ValueError("graph.strategies must have unique official strategy IDs")
        strategy_ids.add(canonical["strategy_id"])
    anchors = workflow["source_anchors"]
    if not isinstance(anchors, list) or not all(isinstance(item, dict) and isinstance(item.get("anchor_id"), str) for item in anchors):
        raise ValueError("workflow.source_anchors must be identified objects")
    anchor_ids = {item["anchor_id"] for item in anchors}
    if len(anchor_ids) != len(anchors):
        raise ValueError("workflow.source_anchors contains duplicate IDs")
    for knowledge_id, knowledge in knowledges.items():
        if not isinstance(knowledge_id, str) or not knowledge_id or not isinstance(knowledge, dict):
            raise ValueError("knowledges must map non-empty IDs to objects")
        if knowledge.get("type") not in KNOWLEDGE_TYPES:
            raise ValueError(f"knowledge {knowledge_id} has unsupported type")
        content = knowledge.get("content")
        if content is None:
            # Abduction keeps an explicit, non-factual alternative-explanation
            # placeholder in the authoring graph.  Gaia later replaces this
            # interface claim with its canonical derived identifier.
            if knowledge.get("type") != "claim" or "AltExp" not in knowledge_id:
                raise ValueError(f"knowledge {knowledge_id} requires content.canonical")
        elif not isinstance(content, dict) or not isinstance(content.get("canonical"), str) or not content["canonical"].strip():
            raise ValueError(f"knowledge {knowledge_id} requires content.canonical")
        source_ids = knowledge.get("source_anchor_ids", [])
        if not isinstance(source_ids, list) or any(not isinstance(item, str) or item not in anchor_ids for item in source_ids):
            raise ValueError(f"knowledge {knowledge_id} has unresolved source anchors")
    if any(node not in knowledges for node in nodes):
        raise ValueError("graph.nodes references an unknown Knowledge")
    if any(knowledges[node]["type"] not in {"claim", "observation_claim"} for node in nodes):
        raise ValueError("graph.nodes may contain only formal claims or approved observation Knowledge")
    for strategy in strategies:
        if any(key not in nodes or knowledges[key]["type"] not in {"claim", "observation_claim"}
               for key in [*strategy["premises"], strategy["conclusion"]]):
            raise ValueError("strategy premises and conclusion must reference graph claims")
        if any(key not in knowledges or knowledges[key]["type"] != "note" or key in nodes for key in strategy["background"]):
            raise ValueError("strategy.background must reference notes outside graph.nodes")
    operator_ids: set[str] = set()
    for operator in graph["operators"]:
        if not set(operator) <= OPERATOR_FIELDS or not {"id", "type", "variables"} <= set(operator):
            raise ValueError("authoring Operators contain unexpected or missing fields")
        operator_id = operator.get("id")
        if not isinstance(operator_id, str) or not operator_id or operator_id in operator_ids:
            raise ValueError("graph.operators must have unique IDs")
        operator_ids.add(operator_id)
        kind = operator.get("type")
        if kind not in OPERATOR_TYPES:
            raise ValueError("authoring Operator has an unsupported type")
        if "metadata" in operator and not isinstance(operator["metadata"], dict):
            raise ValueError("authoring Operator metadata must be an object")
        variables = operator.get("variables")
        if (not isinstance(variables, list) or not variables
                or any(not isinstance(key, str) or key not in nodes for key in variables)
                or len(variables) != len(set(variables))
                or any(knowledges[key]["type"] not in {"claim", "observation_claim"} for key in variables)):
            raise ValueError("authoring Operator variables must be distinct graph claims")
        has_conclusion = operator.get("conclusion") is not None
        if ((kind == "negation" and len(variables) != 1)
                or (kind in {"equivalence", "contradiction", "complement"} and len(variables) != 2)
                or (kind == "implication" and len(variables) != (1 if has_conclusion else 2))
                or (kind in {"conjunction", "disjunction"} and len(variables) < 2)):
            raise ValueError("authoring Operator has invalid arity")
        background = operator.get("background", [])
        if (not isinstance(background, list) or any(not isinstance(key, str) for key in background)
                or len(background) != len(set(background))
                or any(key not in knowledges or knowledges[key]["type"] != "note" or key in nodes for key in background)):
            raise ValueError("operator.background must reference notes outside graph.nodes")
        conclusion = operator.get("conclusion")
        if conclusion is not None and (
            not isinstance(conclusion, str) or conclusion not in nodes or conclusion in variables
            or knowledges[conclusion]["type"] not in {"claim", "observation_claim"}
        ):
            raise ValueError("authoring Operator conclusion must be a separate graph claim")
    weakpoints = workflow["weakpoints"]
    if not isinstance(weakpoints, list) or not all(isinstance(item, dict) for item in weakpoints):
        raise ValueError("workflow.weakpoints must be objects")
    weakpoint_ids: set[str] = set()
    for weakpoint in weakpoints:
        weakpoint_id = weakpoint.get("id")
        if not isinstance(weakpoint_id, str) or not weakpoint_id or weakpoint_id in weakpoint_ids:
            raise ValueError("workflow.weakpoints must have unique IDs")
        weakpoint_ids.add(weakpoint_id)
        payload = weakpoint.get("payload")
        if not isinstance(payload, dict) or set(payload) != {
            "evidence_claim_ids", "target_claim_id", "reasoning_type", "evidence_anchor_ids", "expression",
        }:
            raise ValueError(f"weakpoint {weakpoint_id} has an invalid payload")
        evidence_claim_ids = payload["evidence_claim_ids"]
        if (
            not isinstance(evidence_claim_ids, list)
            or not evidence_claim_ids
            or len(evidence_claim_ids) != len(set(evidence_claim_ids))
            or any(not isinstance(item, str) or item not in nodes for item in evidence_claim_ids)
        ):
            raise ValueError(f"weakpoint {weakpoint_id} has unresolved evidence claims")
        try:
            target_claim_ids = weakpoint_target_ids(payload)
        except ValueError as exc:
            raise ValueError(f"weakpoint {weakpoint_id} has an invalid target claim list") from exc
        if any(target not in nodes for target in target_claim_ids):
            raise ValueError(f"weakpoint {weakpoint_id} has an unresolved target claim")
        if payload["reasoning_type"] not in REASONING_TYPES | {None}:
            raise ValueError(f"weakpoint {weakpoint_id} has an invalid reasoning type")
        if set(evidence_claim_ids) & set(target_claim_ids):
            raise ValueError(f"weakpoint {weakpoint_id} cannot use one claim as both evidence and target")
        evidence_anchor_ids = payload["evidence_anchor_ids"]
        if (
            not isinstance(evidence_anchor_ids, list)
            or len(evidence_anchor_ids) != len(set(evidence_anchor_ids))
            or any(not isinstance(item, str) or item not in anchor_ids for item in evidence_anchor_ids)
        ):
            raise ValueError(f"weakpoint {weakpoint_id} has unresolved evidence anchors")
        if not isinstance(payload["expression"], str) or not payload["expression"].strip():
            raise ValueError(f"weakpoint {weakpoint_id} requires an expression")
    proposal_ids = {
        knowledge_id
        for knowledge_id, knowledge in knowledges.items()
        if knowledge["type"] == "observation_proposal"
    }
    if proposal_ids & set(nodes):
        raise ValueError("observation proposals must remain outside graph.nodes")
    approved_observations = {
        knowledge_id
        for knowledge_id, knowledge in knowledges.items()
        if knowledge["type"] == "observation_claim"
    }
    if not approved_observations <= set(nodes):
        raise ValueError("every approved observation claim must enter graph.nodes")
    links = workflow["non_reasoning_links"]
    if not isinstance(links, list) or not all(isinstance(item, dict) for item in links):
        raise ValueError("workflow.non_reasoning_links must be objects")
    link_ids: set[str] = set()
    for link in links:
        link_id = link.get("id")
        if not isinstance(link_id, str) or not link_id or link_id in link_ids:
            raise ValueError("workflow.non_reasoning_links must have unique IDs")
        link_ids.add(link_id)
        if link.get("reasoning") is not False or link.get("link_type") != "imported_relation":
            raise ValueError(f"relation {link_id} is not a supported non-reasoning link")
        sources = link.get("sources")
        if (
            not isinstance(sources, list)
            or not sources
            or any(not isinstance(source, str) or source not in nodes for source in sources)
            or len(sources) != len(set(sources))
            or link.get("target") not in nodes
        ):
            raise ValueError(f"relation {link_id} references a non-graph Knowledge")
    if workflow["revisions"] != []:
        raise ValueError("workflow.revisions is a compatibility field and must remain empty")


def emit_formalization(
    context: StageContext,
    document: JSONDict,
    *,
    step: int,
    step_name: str,
) -> StageResult:
    try:
        document["revision"]["content_hash"] = content_hash(document)
        validate(document)
    except ValueError as exc:
        return StageResult("failed", findings=[Finding("V2_FORMALIZATION_INVALID", "error", str(exc))])
    formalization_path = context.work_dir / "formalization.json"
    validation_path = context.work_dir / "formalization_validation.json"
    atomic_write_json(formalization_path, document)
    report = {"schema_name": "gaia.formalization.validation", "schema_version": SCHEMA_VERSION, "step": step, "summary": {"status": "passed", "errors": 0, "warnings": 0, "findings": 0}, "findings": []}
    atomic_write_json(validation_path, report)
    return StageResult("succeeded", [
        ArtifactDraft(formalization_path, "formalization", "application/json", {"schema_name": "gaia.formalization.v2", "schema_version": SCHEMA_VERSION, "step": step, "step_name": step_name, "revision_id": document["revision"]["revision_id"], "validation_status": "passed"}),
        ArtifactDraft(validation_path, "formalization.validation", "application/json", {"schema_name": "gaia.formalization.validation", "schema_version": SCHEMA_VERSION, "step": step, "validation_status": "passed", "error_count": 0, "warning_count": 0}),
    ], metadata={"step": step, "knowledge_count": len(document["knowledges"]), "graph_node_count": len(document["graph"]["nodes"]), "validation_status": "passed"})


def emit_step1(context: StageContext, document: JSONDict) -> StageResult:
    return emit_formalization(
        context,
        document,
        step=1,
        step_name="step1_extract_evidence",
    )
