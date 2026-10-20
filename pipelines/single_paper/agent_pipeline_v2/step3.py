"""Pipeline 7.0 Step 3: mechanically classify imported relations."""
from __future__ import annotations

import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pipeline_harness.domain.stages import _expression_tokens, _parse_expression
from pipeline_harness.domain.tools import DomainTool, ToolCallRequest, ToolCallResponse, validate_tool_response
from pipeline_harness.models import Finding, JSONDict
from pipeline_harness.plugins import ArtifactDraft, StageContext, StageResult, instantiate
from pipeline_harness.store import atomic_write_json

from .authoring import REASONING_TYPES, emit_formalization, weakpoint_target_ids
from .step1 import PAPER_TEXT_KIND


STEP_NAME = "step3_analyze_reasoning"
WEAKPOINT_TOOL_SPEC = "agent_pipeline_v2.step2:DeepSeekV4FlashObservationTool"
_OPERATOR_BY_EXPRESSION = (
    ("不可同时成立", "contradiction"),
    ("矛盾", "contradiction"),
    ("等价", "equivalence"),
    ("且", "conjunction"),
    ("和", "conjunction"),
    ("与", "conjunction"),
    ("或", "disjunction"),
    ("非", "negation"),
)


def _latest_formalization(context: StageContext) -> JSONDict:
    references = context.find_all("formalization")
    if not references:
        raise ValueError("Step 3 requires a Step 2 formalization input")
    reference = max(references, key=lambda item: int(item.metadata.get("step", 0)))
    import json
    value = json.loads(context.artifact_path(reference).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("latest formalization must be a JSON object")
    return value


def _is_weakpoint_expression(expression: str) -> bool:
    """Recognize the controlled evidence vocabulary used by cleaned inputs."""
    return any(token in expression for token in ("例子或证据", "举例", "证据", "推出"))


def _is_claims_final_link(link: JSONDict) -> bool:
    """Identify the high-confidence relations declared directly in claims_final."""
    metadata = link.get("metadata")
    return isinstance(metadata, dict) and metadata.get("source") == "claims_final"


_NUMBER_REFERENCE_PATTERN = re.compile(r"\[(\d+)\]")


def _imported_weakpoint_expression(document: JSONDict, expression: str) -> str:
    """Rewrite claims_final numeric references such as [3] to the graph claim id."""

    def replace(match: re.Match[str]) -> str:
        knowledge_id = f"claim_{match.group(1)}"
        return f"[{knowledge_id}]" if knowledge_id in document["knowledges"] else match.group(0)

    return _NUMBER_REFERENCE_PATTERN.sub(replace, expression)


def _adopt_imported_weakpoint(document: JSONDict, link: JSONDict) -> JSONDict | None:
    """Adopt one claims_final relation verbatim as a protected unresolved weakpoint.

    Stage 2 treats claims_final relations as high confidence: they are carried
    into the reasoning graph in document order without an LLM "does it hold"
    judgment, and no later screening, flattening, or shortcut suppression may
    delete them.
    """
    relation = link.get("metadata", {}).get("relation", {})
    expression = str(relation.get("expression", "")).strip()
    sources = list(link.get("sources", []))
    target = str(link.get("target", ""))
    if not expression or not sources or not target:
        return None
    if any(source not in document["knowledges"] for source in sources) or target not in document["knowledges"]:
        return None
    expression = _imported_weakpoint_expression(document, expression)
    anchor_ids = list(dict.fromkeys(
        anchor_id
        for source in sources
        for anchor_id in document["knowledges"][source].get("source_anchor_ids", [])
    ))
    return {
        "id": f"weakpoint_{link['id']}",
        "payload": {
            "evidence_claim_ids": sources,
            "target_claim_id": [target],
            "reasoning_type": None,
            "evidence_anchor_ids": anchor_ids,
            "expression": expression,
        },
    }


def _operator_type(expression: str) -> str | None:
    if _is_weakpoint_expression(expression):
        return None
    return next((operator for token, operator in _OPERATOR_BY_EXPRESSION if token in expression), None)


def _fixed_expression_ast(document: JSONDict, expression: str) -> JSONDict:
    references = {key: key for key in document["graph"]["nodes"]}
    for key in document["graph"]["nodes"]:
        match = re.fullmatch(r"claim_(\d+)", key)
        if match:
            references[match.group(1)] = key
    postfix = re.fullmatch(r"\s*(.+?)\s+与\s+(.+?)\s+(矛盾|不可同时成立)\s*", expression)
    if postfix:
        expression = f"{postfix.group(1)} {postfix.group(3)} {postfix.group(2)}"
    return _parse_expression(_expression_tokens(expression, references))


def _materialize_fixed_expression(
    document: JSONDict, relation_id: str, expression: str, ast: JSONDict,
) -> tuple[list[JSONDict], list[str]]:
    """Persist nested fixed subexpressions and one root Operator; never materialize implication."""
    operators: list[JSONDict] = []
    added: list[str] = []
    counter = 0

    def operand(node: JSONDict) -> str:
        nonlocal counter
        if node["kind"] == "claim":
            return str(node["id"])
        if node["kind"] == "implies":
            raise ValueError("nested implication is not a fixed Operator")
        if node["kind"] == "negation":
            variables = [operand(node["operand"])]
        else:
            variables = [operand(node["left"]), operand(node["right"])]
        counter += 1
        helper_id = f"helper_{relation_id}_{counter}"
        operator_id = f"operator_{relation_id}_ast_{counter}"
        if helper_id in document["knowledges"]:
            raise ValueError(f"duplicate AST helper ID: {helper_id}")
        document["knowledges"][helper_id] = {
            "type": "claim",
            "content": {"canonical": f"{node['kind']}({','.join(variables)})"},
            "source_anchor_ids": [],
        }
        document["graph"]["nodes"].append(helper_id)
        operators.append({
            "id": operator_id, "type": node["kind"], "variables": variables, "conclusion": helper_id,
            "metadata": {"expression": expression, "source_relation_id": relation_id, "derived_ast_helper": True},
        })
        added.extend([helper_id, operator_id])
        return helper_id

    if ast["kind"] == "implies":
        operand(ast["left"])
        operand(ast["right"])
        return operators, added
    if ast["kind"] == "claim":
        raise ValueError("a fixed relation requires an operator")
    if ast["kind"] == "negation":
        variables = [operand(ast["operand"])]
    else:
        variables = [operand(ast["left"]), operand(ast["right"])]
    root_id = f"operator_{relation_id}"
    operators.append({
        "id": root_id, "type": ast["kind"], "variables": variables,
        "metadata": {"expression": expression, "source_relation_id": relation_id},
    })
    added.append(root_id)
    return operators, added


def _json_pointer(value: Any, pointer: str) -> Any:
    current = value
    for token in pointer.lstrip("/").split("/"):
        if not token:
            continue
        token = token.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def _anchor_excerpt(context: StageContext, anchor: JSONDict) -> str | None:
    artifact_id = anchor.get("artifact_id")
    locator = anchor.get("locator")
    reference = next((item for item in context.inputs if item.artifact_id == artifact_id), None)
    if reference is None or not isinstance(locator, dict):
        return None
    try:
        path = context.artifact_path(reference)
        if locator.get("type") == "markdown_span":
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            start, end = int(locator["start_line"]), int(locator["end_line"])
            return "\n".join(lines[start - 1:end])
        if locator.get("type") == "json_pointer":
            value = _json_pointer(json.loads(path.read_text(encoding="utf-8-sig")), str(locator["pointer"]))
            return value.get("text") if isinstance(value, dict) and isinstance(value.get("text"), str) else json.dumps(value, ensure_ascii=False)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _classification_records(context: StageContext, document: JSONDict, weakpoints: list[JSONDict]) -> list[JSONDict]:
    anchors = {str(item["anchor_id"]): item for item in document["workflow"]["source_anchors"]}
    records: list[JSONDict] = []
    for weakpoint in weakpoints:
        payload = weakpoint["payload"]
        evidence_ids = list(payload["evidence_claim_ids"])
        evidence_anchor_ids = list(payload["evidence_anchor_ids"])
        records.append({
            "weakpoint_id": weakpoint["id"],
            "evidence_claims": [
                {"claim_id": claim_id, "content": document["knowledges"][claim_id]["content"]["canonical"]}
                for claim_id in evidence_ids
            ],
            "target_claims": [
                {"claim_id": claim_id, "content": document["knowledges"][claim_id]["content"]["canonical"]}
                for claim_id in weakpoint_target_ids(payload)
            ],
            "expression": payload["expression"],
            "evidence_anchors": [
                {"anchor_id": anchor_id, "text": excerpt}
                for anchor_id in evidence_anchor_ids
                if (anchor := anchors.get(anchor_id)) is not None
                and (excerpt := _anchor_excerpt(context, anchor))
            ],
        })
    return records


def _screen_classifications(
    document: JSONDict, weakpoints: list[JSONDict], classifications: dict[str, str | None],
) -> tuple[dict[str, str | None], set[str]]:
    """Apply conservative, domain-agnostic gates before Step 4 expansion.

    A non-null label authorizes a later expansion, so it must at least have
    valid distinct endpoints and a paper-text anchor.  Semantic decisions
    about whether an intermediate rule is recoverable remain with the model;
    this gate only rejects structurally unsupported or plainly duplicated
    links and never upgrades a null label.
    """
    knowledge = document["knowledges"]
    paper_anchor_ids = {
        str(item["anchor_id"])
        for item in document["workflow"]["source_anchors"]
        if item.get("source_kind") == PAPER_TEXT_KIND
    }
    screened: dict[str, str | None] = {}
    rejected: set[str] = set()
    for weakpoint in weakpoints:
        weakpoint_id = str(weakpoint["id"])
        label = classifications.get(weakpoint_id)
        if label is None:
            screened[weakpoint_id] = None
            continue
        payload = weakpoint["payload"]
        evidence = list(dict.fromkeys(payload.get("evidence_claim_ids", [])))
        targets = list(dict.fromkeys(weakpoint_target_ids(payload)))
        valid_ids = (
            bool(evidence) and bool(targets)
            and all(item in knowledge for item in [*evidence, *targets])
            and not set(evidence) & set(targets)
        )
        anchors = {
            anchor_id
            for item in [*evidence, *targets]
            for anchor_id in knowledge[item].get("source_anchor_ids", [])
        }
        canonical = {
            str(knowledge[item].get("content", {}).get("canonical", "")).strip().casefold()
            for item in [*evidence, *targets]
        }
        # Identical endpoint content is an equivalence/duplicate candidate,
        # not a reasoning edge, and must not be forced into Step 4.
        duplicate_content = len(canonical) < len(evidence) + len(targets)
        if not valid_ids or not (anchors & paper_anchor_ids) or duplicate_content:
            screened[weakpoint_id] = None
            rejected.add(weakpoint_id)
        else:
            screened[weakpoint_id] = label
    return screened, rejected


def _paper_anchor_ids(document: JSONDict, knowledge_ids: set[str]) -> set[str]:
    paper_anchors = {
        item["anchor_id"] for item in document["workflow"]["source_anchors"]
        if item.get("source_kind") == PAPER_TEXT_KIND
    }
    return {
        anchor_id for knowledge_id in knowledge_ids
        for anchor_id in document["knowledges"][knowledge_id].get("source_anchor_ids", [])
        if anchor_id in paper_anchors
    }


def _deduplicate_weakpoint_links(document: JSONDict, links: list[JSONDict]) -> tuple[list[JSONDict], list[str]]:
    """Drop only redundant relation candidates while preserving Step1 imports.

    High-confidence claims_final relations are never removed and never act as
    the redundancy witness that removes another candidate.
    """
    kept: list[JSONDict] = []
    removed: list[str] = []
    for link in links:
        if _is_claims_final_link(link):
            kept.append(link)
            continue
        relation = link.get("metadata", {}).get("relation", {})
        conclusion = str(relation.get("conclusion", ""))
        target = str(link.get("target", ""))
        sources = set(link.get("sources", []))
        redundant = False
        survivors: list[JSONDict] = []
        for prior in kept:
            if _is_claims_final_link(prior):
                survivors.append(prior)
                continue
            prior_relation = prior.get("metadata", {}).get("relation", {})
            if (str(prior_relation.get("conclusion", "")) == conclusion
                    and str(prior.get("target", "")) == target):
                prior_sources = set(prior.get("sources", []))
                if sources <= prior_sources:
                    redundant = True
                    break
                if prior_sources < sources:
                    removed.append(str(prior.get("id")))
                    continue
            survivors.append(prior)
        if redundant:
            removed.append(str(link.get("id")))
            continue
        survivors.append(link)
        kept = survivors
    return kept, removed


def _transitive_shortcut_indexes(edges: list[tuple[int, set[str], str, str]]) -> set[int]:
    """Return the indexes of same-kind edges made redundant by a two-hop chain.

    ``edges`` holds one ``(index, source_set, target, kind)`` per candidate.  A
    direct edge is a shortcut only when a same-kind ``first -> midpoint`` edge
    and a same-kind ``second -> direct_target`` edge (with ``midpoint`` among
    ``second``'s sources) together cover **every** source of the direct edge;
    the two-hop path then replaces the direct edge without orphaning a source.
    A chain that only covers a strict subset must never remove the direct edge,
    because its remaining sources would lose their only connection.
    """
    remove: set[int] = set()
    for direct_index, direct_sources, direct_target, kind in edges:
        for first_index, first_sources, midpoint, first_kind in edges:
            if first_index == direct_index or first_kind != kind or not first_sources <= direct_sources:
                continue
            for second_index, second_sources, second_target, second_kind in edges:
                if (
                    second_index in {direct_index, first_index}
                    or second_kind != kind
                    or second_target != direct_target
                    or midpoint not in second_sources
                ):
                    continue
                second_leaves = second_sources - {midpoint}
                covered = first_sources | second_leaves
                if second_leaves <= direct_sources and direct_sources <= covered:
                    remove.add(direct_index)
                    break
            if direct_index in remove:
                break
    return remove


def _remove_transitive_shortcuts(
    weakpoints: list[JSONDict], protected: frozenset[str] = frozenset(),
) -> tuple[list[JSONDict], list[str]]:
    """Remove same-type transitive shortcuts over evidence hyperedges."""
    edges: list[tuple[int, set[str], str, str]] = []
    for index, item in enumerate(weakpoints):
        payload = item.get("payload", {})
        evidence = frozenset(map(str, payload.get("evidence_claim_ids", [])))
        targets = payload.get("target_claim_id", [])
        if evidence and len(targets) == 1:
            edges.append((index, evidence, str(targets[0]), str(payload.get("reasoning_type", ""))))
    remove = {
        index for index in _transitive_shortcut_indexes(edges)
        if str(weakpoints[index].get("id")) not in protected
    }
    return (
        [item for index, item in enumerate(weakpoints) if index not in remove],
        [str(weakpoints[index].get("id")) for index in sorted(remove)],
    )


def _flatten_redundant_premises(
    weakpoints: list[JSONDict], protected: frozenset[str] = frozenset(),
) -> list[str]:
    """Substitute derived premises with their direct evidence when redundant.

    This handles multi-premise triangles such as ``A→B`` and
    ``(A+B+X)→C`` without rejecting legitimate multi-hop reasoning.  Protected
    claims_final weakpoints are carried verbatim and never rewritten here.
    """
    changed: list[str] = []
    for item in weakpoints:
        if str(item.get("id")) in protected:
            continue
        payload = item.get("payload", {})
        evidence = list(payload.get("evidence_claim_ids", []))
        targets = list(payload.get("target_claim_id", []))
        if len(targets) != 1:
            continue
        for candidate in weakpoints:
            source = list(candidate.get("payload", {}).get("evidence_claim_ids", []))
            derived = list(candidate.get("payload", {}).get("target_claim_id", []))
            if (len(derived) != 1 or derived[0] not in evidence
                    or candidate.get("payload", {}).get("reasoning_type") != payload.get("reasoning_type")
                    or not source or set(source) & set(evidence) == set(evidence)):
                continue
            if set(source) <= (set(evidence) - {derived[0]}):
                evidence = [key for key in evidence if key != derived[0]]
                for key in source:
                    if key not in evidence:
                        evidence.append(key)
                payload["evidence_claim_ids"] = evidence
                expression = payload.get("expression")
                if isinstance(expression, str):
                    expression = re.sub(rf"\[{re.escape(str(derived[0]))}\]", " ".join(f"[{key}]" for key in source), expression)
                    payload["expression"] = expression
                changed.append(str(item.get("id")))
                break
    return changed


def _relation_clusters(context: StageContext, document: JSONDict, links: list[JSONDict]) -> list[JSONDict]:
    """Build experiment-bounded clusters plus one adjacent coarse-relation layer.

    High-confidence claims_final relations are adopted directly and are never
    sent to the cluster/LLM normalization path.
    """
    candidates = [
        item for item in links
        if not _is_claims_final_link(item) and _is_weakpoint_expression(
            str(item.get("metadata", {}).get("relation", {}).get("expression", ""))
        )
    ]
    seeded: dict[str, list[JSONDict]] = {}
    loose: list[JSONDict] = []
    for link in candidates:
        context_id = link.get("metadata", {}).get("relation", {}).get("relation_context_id")
        if isinstance(context_id, str) and context_id:
            seeded.setdefault(context_id, []).append(link)
        else:
            loose.append(link)
    groups: list[tuple[str | None, list[JSONDict]]] = [(key, value) for key, value in seeded.items()]
    frozen_nodes = [
        {key for link in group for key in [*link["sources"], link["target"]]}
        for _, group in groups
    ]
    frozen_anchors = [_paper_anchor_ids(document, nodes) for nodes in frozen_nodes]
    unassigned: list[JSONDict] = []
    for link in loose:
        endpoints = {*link["sources"], link["target"]}
        link_anchors = _paper_anchor_ids(document, endpoints)
        scores = [
            len(link_anchors & anchors) if endpoints & nodes else 0
            for nodes, anchors in zip(frozen_nodes, frozen_anchors)
        ]
        best = max(scores, default=0)
        matches = [index for index, score in enumerate(scores) if score == best and score > 0]
        if len(matches) == 1:
            groups[matches[0]][1].append(link)
        else:
            unassigned.append(link)
    groups.extend((None, [link]) for link in unassigned)

    anchor_registry = {item["anchor_id"]: item for item in document["workflow"]["source_anchors"]}
    clusters: list[JSONDict] = []
    for index, (context_id, group) in enumerate(groups, 1):
        node_ids = sorted({key for link in group for key in [*link["sources"], link["target"]]})
        paper_anchor_ids = sorted(_paper_anchor_ids(document, set(node_ids)))
        clusters.append({
            "cluster_id": f"cluster_{index:03d}",
            "relation_context_id": context_id,
            "claims": [{
                "claim_id": key,
                "type": document["knowledges"][key]["type"],
                "content": document["knowledges"][key]["content"]["canonical"],
                "source_anchor_ids": list(document["knowledges"][key].get("source_anchor_ids", [])),
            } for key in node_ids],
            "candidate_relations": [{
                "relation_id": link["id"],
                "sources": list(link["sources"]),
                "target": link["target"],
                "expression": str(link.get("metadata", {}).get("relation", {}).get("expression", "")),
            } for link in group],
            "source_excerpts": [
                {"anchor_id": anchor_id, "text": excerpt}
                for anchor_id in paper_anchor_ids
                if (anchor := anchor_registry.get(anchor_id)) is not None
                and (excerpt := _anchor_excerpt(context, anchor))
            ],
        })
    return clusters


def _validate_cluster_result(document: JSONDict, clusters: list[JSONDict], normalized: Any) -> list[JSONDict]:
    if not isinstance(normalized, dict) or set(normalized) != {"clusters"} or not isinstance(normalized["clusters"], list):
        raise ValueError("Step 3 cluster tool must return only clusters")
    source_by_id = {item["cluster_id"]: item for item in clusters}
    results = normalized["clusters"]
    if len(results) != len(clusters):
        raise ValueError("Step 3 cluster tool must return every cluster exactly once")
    result_by_id: dict[str, JSONDict] = {}
    for result in results:
        if (not isinstance(result, dict) or set(result) != {"cluster_id", "weakpoints", "rejected_relation_ids"}
                or result.get("cluster_id") not in source_by_id or result["cluster_id"] in result_by_id):
            raise ValueError("Step 3 cluster tool returned invalid or duplicate cluster data")
        result_by_id[result["cluster_id"]] = result
    if set(result_by_id) != set(source_by_id):
        raise ValueError("Step 3 cluster tool omitted a cluster")
    all_members: set[str] = set()
    weakpoints: list[JSONDict] = []
    for source in clusters:
        result = result_by_id[source["cluster_id"]]
        relation_ids = {item["relation_id"] for item in source["candidate_relations"]}
        relation_order = {item["relation_id"]: index for index, item in enumerate(source["candidate_relations"])}
        node_ids = {item["claim_id"] for item in source["claims"]}
        rejected = result["rejected_relation_ids"]
        items = result["weakpoints"]
        if (not isinstance(rejected, list) or any(not isinstance(key, str) for key in rejected)
                or not isinstance(items, list)):
            raise ValueError("Step 3 cluster result requires weakpoints and rejected relation IDs")
        consumed = list(rejected)
        cluster_weakpoints: list[tuple[int, JSONDict]] = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "member_relation_ids", "evidence_claim_ids", "target_claim_id", "reasoning_type", "expression",
            }:
                raise ValueError("Step 3 normalized weakpoint fields are invalid")
            members = item["member_relation_ids"]
            evidence = item["evidence_claim_ids"]
            targets = item["target_claim_id"]
            if any(not isinstance(values, list) or not values or len(values) != len(set(values))
                   or any(not isinstance(key, str) or not key for key in values)
                   for values in (members, evidence, targets)):
                raise ValueError("Step 3 normalized weakpoint IDs must be non-empty and unique")
            if (not set(members) <= relation_ids or not set(evidence + targets) <= node_ids
                    or set(evidence) & set(targets) or item["reasoning_type"] not in REASONING_TYPES | {None}
                    or not isinstance(item["expression"], str) or not item["expression"].strip()
                    or any(f"[{key}]" not in item["expression"] for key in [*evidence, *targets])):
                raise ValueError("Step 3 normalized weakpoint has unsupported references, type, or expression")
            members = sorted(members, key=relation_order.__getitem__)
            consumed.extend(members)
            first = members[0]
            evidence_anchor_ids = sorted({
                anchor_id for key in evidence
                for anchor_id in document["knowledges"][key].get("source_anchor_ids", [])
            })
            cluster_weakpoints.append((relation_order[first], {
                "id": f"weakpoint_{first}",
                "payload": {
                    "evidence_claim_ids": list(evidence), "target_claim_id": list(targets),
                    "reasoning_type": item["reasoning_type"], "evidence_anchor_ids": evidence_anchor_ids,
                    "expression": item["expression"],
                },
            }))
        if len(consumed) != len(set(consumed)) or set(consumed) != relation_ids:
            raise ValueError("every candidate relation must be consumed or rejected exactly once")
        if all_members & set(consumed):
            raise ValueError("candidate relation was assigned across multiple clusters")
        all_members.update(consumed)
        weakpoints.extend(item for _, item in sorted(cluster_weakpoints, key=lambda pair: pair[0]))
    return weakpoints


def _classify_weakpoints(context: StageContext, document: JSONDict, weakpoints: list[JSONDict]) -> tuple[dict[str, str | None], ArtifactDraft]:
    spec = str(context.options.get("tool_plugin", WEAKPOINT_TOOL_SPEC))
    tool = instantiate(spec)
    if not isinstance(tool, DomainTool):
        raise TypeError(f"{spec} does not implement DomainTool")
    request = ToolCallRequest(
        call_id=f"semantic_step_3_{context.run_id}_{context.attempt}", tool_name=tool.name,
        tool_version=tool.version, operation="classify_weakpoints",
        inputs=[{"artifact_id": ref.artifact_id, "kind": ref.kind, "sha256": ref.sha256} for ref in context.inputs],
        parameters={"weakpoints": _classification_records(context, document, weakpoints)},
    )
    response: ToolCallResponse | None = None
    error_message = ""
    for attempt in range(2):
        current_request = request if attempt == 0 else ToolCallRequest(
            f"{request.call_id}_repair", tool.name, tool.version, "classify_weakpoints",
            request.inputs, {**copy.deepcopy(request.parameters), "repair_feedback": error_message},
        )
        try:
            response = tool.invoke(current_request)
            if not isinstance(response, ToolCallResponse):
                raise TypeError(f"{spec} returned {type(response).__name__}, not ToolCallResponse")
            validate_tool_response(current_request, response)
            if response.status != "succeeded":
                raise ValueError(str((response.error or {}).get("message", "Step 3 semantic tool failed")))
            break
        except Exception as exc:
            error_message = str(exc)
            response = ToolCallResponse(current_request.call_id, "failed", None,
                                        error={"type": type(exc).__name__, "message": error_message})
    assert response is not None
    response_path = context.work_dir / "semantic_step_3_tool_response.json"
    atomic_write_json(response_path, {"request": request.to_dict(), "response": response.to_dict()})
    draft = ArtifactDraft(response_path, "tool.semantic_review.response", "application/json", {
        "schema_version": "1.0.0", "step": 3, "tool_call_id": request.call_id,
        "tool_name": tool.name, "tool_version": tool.version, "status": response.status,
    })
    if response.status != "succeeded" or not isinstance(response.normalized, dict):
        return {}, draft
    classifications = response.normalized.get("classifications")
    if not isinstance(classifications, list):
        raise ValueError("Step 3 semantic tool returned no classifications")
    return {str(item["weakpoint_id"]): item["reasoning_type"] for item in classifications if isinstance(item, dict)}, draft


def _normalize_clusters(
    context: StageContext, document: JSONDict, clusters: list[JSONDict],
) -> tuple[list[JSONDict], list[ArtifactDraft]]:
    spec = str(context.options.get("tool_plugin", WEAKPOINT_TOOL_SPEC))
    frozen_document = copy.deepcopy(document)
    frozen_clusters = copy.deepcopy(clusters)
    inputs = [{"artifact_id": ref.artifact_id, "kind": ref.kind, "sha256": ref.sha256} for ref in context.inputs]

    def normalize_one(index_and_cluster: tuple[int, JSONDict]) -> tuple[JSONDict, ArtifactDraft]:
        index, cluster = index_and_cluster
        tool = instantiate(spec)
        if not isinstance(tool, DomainTool):
            raise TypeError(f"{spec} does not implement DomainTool")
        request = ToolCallRequest(
            call_id=f"semantic_step_3_cluster_{context.run_id}_{context.attempt}_{index}",
            tool_name=tool.name, tool_version=tool.version, operation="normalize_weakpoint_clusters",
            inputs=inputs, parameters={"clusters": [copy.deepcopy(cluster)]},
        )
        error_message = ""
        response: ToolCallResponse | None = None
        for attempt in range(2):
            current_request = request if attempt == 0 else ToolCallRequest(
                f"{request.call_id}_repair", tool.name, tool.version, "normalize_weakpoint_clusters",
                inputs, {"clusters": [copy.deepcopy(cluster)], "repair_feedback": error_message},
            )
            try:
                response = tool.invoke(current_request)
                if not isinstance(response, ToolCallResponse):
                    raise TypeError(f"{spec} returned {type(response).__name__}, not ToolCallResponse")
                validate_tool_response(current_request, response)
                if response.status != "succeeded":
                    raise ValueError(str((response.error or {}).get("message", "Step 3 cluster tool failed")))
                _validate_cluster_result(frozen_document, [cluster], response.normalized)
                break
            except Exception as exc:
                error_message = str(exc)
                response = ToolCallResponse(
                    current_request.call_id, "failed", None,
                    error={"type": type(exc).__name__, "message": error_message},
                )
        if response is None or response.status != "succeeded":
            # Deterministic conservative fallback: retain each supplied
            # non-reasoning relation as an unresolved weakpoint.  This keeps
            # the graph complete without inventing a merge, direction, or
            # reasoning family after both LLM attempts failed.
            weakpoints = []
            for relation in cluster["candidate_relations"]:
                sources = list(relation["sources"])
                target = str(relation["target"])
                endpoints = [*sources, target]
                weakpoints.append({
                    "member_relation_ids": [relation["relation_id"]],
                    "evidence_claim_ids": sources,
                    "target_claim_id": [target], "reasoning_type": None,
                    "expression": " 推出 ".join(f"[{key}]" for key in endpoints),
                })
            response = ToolCallResponse(
                request.call_id, "succeeded", None,
                {"clusters": [{"cluster_id": cluster["cluster_id"], "weakpoints": weakpoints, "rejected_relation_ids": []}]},
                metadata={"fallback": "unresolved_weakpoints", "error": error_message},
            )
        response_path = context.work_dir / f"semantic_step_3_cluster_tool_response_{index}.json"
        atomic_write_json(response_path, {"request": request.to_dict(), "response": response.to_dict()})
        draft = ArtifactDraft(response_path, "tool.semantic_review.response", "application/json", {
            "schema_version": "1.0.0", "step": 3, "tool_call_id": request.call_id,
            "tool_name": tool.name, "tool_version": tool.version, "status": response.status,
        })
        if response.status != "succeeded" or not isinstance(response.normalized, dict):
            raise ValueError(str((response.error or {}).get("message", "Step 3 cluster tool failed")))
        result_clusters = response.normalized.get("clusters")
        if not isinstance(result_clusters, list) or len(result_clusters) != 1:
            raise ValueError("Step 3 cluster tool must return exactly its frozen cluster")
        return result_clusters[0], draft

    with ThreadPoolExecutor() as executor:
        results = list(executor.map(normalize_one, enumerate(frozen_clusters, 1)))
    normalized = {"clusters": [item for item, _ in results]}
    weakpoints = _validate_cluster_result(frozen_document, frozen_clusters, normalized)
    return weakpoints, [draft for _, draft in results]


class Step3AnalyzeReasoningPlugin:
    """Normalize bounded relation clusters into weakpoints and retain fixed operators."""

    def run(self, context: StageContext) -> StageResult:
        findings: list[Finding] = []
        try:
            document = copy.deepcopy(_latest_formalization(context))
            workflow = document["workflow"]
            graph = document["graph"]
            links, redundant_relation_ids = _deduplicate_weakpoint_links(document, workflow["non_reasoning_links"])
            if redundant_relation_ids:
                findings.append(Finding(
                    "STEP3_REDUNDANT_RELATION_SUPPRESSED", "warning",
                    f"Suppressed redundant weakpoint relations: {sorted(set(redundant_relation_ids))}",
                ))
            # Freeze only the deduplicated candidate set; Step1/Step2 artifacts
            # remain untouched and continue to preserve full imports.
            frozen_step2 = copy.deepcopy(document)
            frozen_step2["workflow"]["non_reasoning_links"] = links
            existing_weakpoints: list[JSONDict] = list(workflow["weakpoints"])
            operators: list[JSONDict] = list(graph["operators"])
            added: list[str] = []
            adopted_weakpoints: list[JSONDict] = []
            protected_ids: set[str] = set()
            known_weakpoint_ids = {str(item.get("id")) for item in existing_weakpoints}
            for link in links:
                relation_id = str(link["id"])
                expression = str(link.get("metadata", {}).get("relation", {}).get("expression", "")).strip()
                if not expression:
                    findings.append(Finding("STEP3_REJECTED_RELATION", "warning", f"Retained unresolved relation {relation_id}: no relation expression"))
                    continue
                sources = list(link["sources"])
                target = str(link["target"])
                if _is_claims_final_link(link):
                    # Stage 2: claims_final relations are high confidence.  Keep
                    # their mechanical fixed-operator lowering (if any) and adopt
                    # the reasoning relation verbatim and in document order,
                    # without an LLM "does it hold" judgment.  These weakpoints
                    # are protected from every later deletion pass.
                    destination = ""
                    if not _is_weakpoint_expression(expression) or "推出" in expression:
                        try:
                            ast = _fixed_expression_ast(document, expression)
                            fixed_operators, fixed_added = _materialize_fixed_expression(
                                document, relation_id, expression, ast,
                            )
                            operators.extend(fixed_operators)
                            added.extend(fixed_added)
                            if fixed_added:
                                destination = "operator"
                        except Exception as exc:
                            findings.append(Finding("STEP3_REJECTED_RELATION", "warning", f"Retained unresolved relation {relation_id}: {exc}"))
                    if _is_weakpoint_expression(expression):
                        adopted_weakpoint = _adopt_imported_weakpoint(document, link)
                        if adopted_weakpoint is not None and str(adopted_weakpoint["id"]) not in known_weakpoint_ids:
                            adopted_weakpoints.append(adopted_weakpoint)
                            protected_ids.add(str(adopted_weakpoint["id"]))
                            known_weakpoint_ids.add(str(adopted_weakpoint["id"]))
                            destination = destination or "weakpoint"
                    # Reconciliation: every imported relation must land somewhere.
                    if not destination:
                        findings.append(Finding(
                            "STEP3_UNRESOLVED_IMPORTED_RELATION", "warning",
                            f"Imported claims_final relation {relation_id} produced no reasoning destination",
                        ))
                    continue
                try:
                    if not _is_weakpoint_expression(expression):
                        ast = _fixed_expression_ast(document, expression)
                        fixed_operators, fixed_added = _materialize_fixed_expression(
                            document, relation_id, expression, ast,
                        )
                        operators.extend(fixed_operators)
                        added.extend(fixed_added)
                    elif "推出" in expression:
                        ast = _fixed_expression_ast(document, expression)
                        fixed_operators, fixed_added = _materialize_fixed_expression(
                            document, relation_id, expression, ast,
                        )
                        operators.extend(fixed_operators)
                        added.extend(fixed_added)
                except Exception as exc:
                    findings.append(Finding("STEP3_REJECTED_RELATION", "warning", f"Retained unresolved relation {relation_id}: {exc}"))
                    anchor_ids = []
                    for source_id in sources:
                        anchor_ids.extend(document["knowledges"].get(source_id, {}).get("source_anchor_ids", []))
                    fallback_id = f"weakpoint_{relation_id}"
                    if anchor_ids and fallback_id not in known_weakpoint_ids:
                        existing_weakpoints.append({
                            "id": fallback_id,
                            "payload": {
                                "evidence_claim_ids": sources,
                                "target_claim_id": target,
                                "reasoning_type": None,
                                "evidence_anchor_ids": list(dict.fromkeys(anchor_ids)),
                                "expression": expression or f"{sources} 推出 {target}",
                            },
                        })
                        known_weakpoint_ids.add(fallback_id)
            drafts: list[ArtifactDraft] = []
            clusters = _relation_clusters(context, frozen_step2, frozen_step2["workflow"]["non_reasoning_links"])
            normalized_weakpoints: list[JSONDict] = []
            if clusters:
                try:
                    normalized_weakpoints, cluster_drafts = _normalize_clusters(context, frozen_step2, clusters)
                    drafts.extend(cluster_drafts)
                except Exception as exc:
                    findings.append(Finding("STEP3_CLUSTER_PARTIAL_FAILURE", "warning", f"Cluster normalization partially failed: {exc}"))
                    for cluster in clusters:
                        for relation in cluster:
                            relation_id = str(relation["id"])
                            sources = list(relation.get("sources", []))
                            target = str(relation.get("target", ""))
                            anchors = list(dict.fromkeys(
                                anchor
                                for source in sources
                                for anchor in document["knowledges"].get(source, {}).get("source_anchor_ids", [])
                            ))
                            if anchors:
                                normalized_weakpoints.append({
                                    "id": f"weakpoint_{relation_id}",
                                    "payload": {
                                        "evidence_claim_ids": sources,
                                        "target_claim_id": target,
                                        "reasoning_type": None,
                                        "evidence_anchor_ids": anchors,
                                        "expression": str(relation.get("metadata", {}).get("relation", {}).get("expression", "")) or f"{sources} 推出 {target}",
                                    },
                                })
                added.extend(item["id"] for item in normalized_weakpoints)
            # High-confidence claims_final weakpoints are kept first, in their
            # original document order, ahead of the LLM-normalized candidates.
            weakpoints = [*existing_weakpoints, *adopted_weakpoints, *normalized_weakpoints]
            # Cluster normalization only groups/orients endpoints.  Treat its
            # newly created weakpoints exactly like direct input weakpoints and
            # send them through the canonical classifier as well.  If that
            # second call fails, retain any non-null cluster label as a safe
            # fallback and leave only genuinely unresolved items as null.
            # Adopted claims_final weakpoints are deliberately excluded: they
            # are already high-confidence and take no "does it hold" judgment.
            unclassified_existing = [
                item for item in existing_weakpoints if item["payload"]["reasoning_type"] is None
            ]
            classification_targets = [*unclassified_existing, *normalized_weakpoints]
            if classification_targets:
                try:
                    classifications, classification_draft = _classify_weakpoints(context, document, classification_targets)
                except Exception as exc:
                    classifications, classification_draft = {}, None
                    findings.append(Finding("STEP3_UNRESOLVED_CLASSIFICATION", "warning", str(exc)))
                if classification_draft is not None:
                    drafts.append(classification_draft)
                expected = {str(item["id"]) for item in classification_targets}
                if set(classifications) != expected:
                    findings.append(Finding(
                        "STEP3_UNRESOLVED_CLASSIFICATION", "warning",
                        "Retained weakpoints whose reasoning classification could not be completed"))
                else:
                    classifications, rejected_ids = _screen_classifications(document, classification_targets, classifications)
                    rejected_ids -= protected_ids
                    if rejected_ids:
                        weakpoints = [item for item in weakpoints if str(item["id"]) not in rejected_ids]
                        findings.extend(
                            Finding("STEP3_REJECTED_WEAKPOINT", "warning", f"Deleted {item_id}: screening gate rejected the relation")
                            for item_id in sorted(rejected_ids)
                        )
                    for weakpoint in classification_targets:
                        if str(weakpoint["id"]) not in rejected_ids:
                            weakpoint["payload"]["reasoning_type"] = classifications[str(weakpoint["id"])]
            flattened_ids = _flatten_redundant_premises(weakpoints, frozenset(protected_ids))
            weakpoints, transitive_ids = _remove_transitive_shortcuts(weakpoints, frozenset(protected_ids))
            if flattened_ids:
                findings.append(Finding(
                    "STEP3_DERIVED_PREMISE_FLATTENED", "warning",
                    f"Flattened redundant derived premises in: {sorted(set(flattened_ids))}",
                ))
            if transitive_ids:
                findings.append(Finding(
                    "STEP3_TRANSITIVE_SHORTCUT_SUPPRESSED", "warning",
                    f"Suppressed transitive weakpoint shortcuts: {sorted(transitive_ids)}",
                ))
            graph["operators"] = operators
            workflow["weakpoints"] = weakpoints
            workflow["non_reasoning_links"] = []
            reasoning_operator_count = sum(
                1 for operator in operators if operator.get("type") not in {"equivalence"}
            )
            observation_count = sum(
                1 for knowledge in document["knowledges"].values()
                if knowledge.get("type") == "observation_claim"
            )
            if reasoning_operator_count == 0 and observation_count:
                findings.append(Finding(
                    "STEP3_NO_TYPED_REASONING", "warning",
                    "No non-equivalence reasoning operator was produced despite observation claims; route to human review",
                ))
            if observation_count == 0:
                findings.append(Finding(
                    "STEP3_NO_OBSERVATION_CLAIM", "warning",
                    "No observation claim survived Step 3; experimental coverage requires human review",
                ))
            prior = document["revision"]
            revision_id = f"revision_{context.run_id}_step_3"
            document["revision"] = {
                "revision_id": revision_id, "supersedes": prior.get("revision_id"),
                "parent_hash": prior.get("content_hash"), "content_hash": "",
            }
            emitted = emit_formalization(context, document, step=3, step_name=STEP_NAME)
            return StageResult(
                emitted.status,
                [*emitted.artifacts, *drafts],
                emitted.findings,
                emitted.metadata,
            )
        except Exception as exc:
            return StageResult("failed", findings=[Finding("STEP3_CLASSIFICATION_FAILED", "error", str(exc))])
