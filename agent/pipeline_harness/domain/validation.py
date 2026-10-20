from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..models import JSONDict
from .contracts import (
    FORMALIZATION_SNAPSHOT_KIND,
    FORMALIZATION_SNAPSHOT_SCHEMA,
    KNOWLEDGE_TYPES,
    NON_REASONING_LINK_TYPES,
    OPERATOR_TYPES,
    SCHEMA_VERSION,
    SOURCE_LOCATOR_TYPES,
    STEP_NAMES,
    STRATEGY_FORMS,
    VALIDATION_SCHEMA,
    VALIDATOR_VERSION,
    canonical_hash,
)


def _target(target_type: str, target_id: Any) -> JSONDict:
    return {"type": target_type, "id": str(target_id)}


def make_validation_finding(
    rule: str,
    severity: str,
    target_type: str,
    target_id: Any,
    message: str,
    json_pointer: str,
    suggested_action: str,
    *,
    related_target: JSONDict | None = None,
    edge: JSONDict | None = None,
) -> JSONDict:
    identity = {"rule": rule, "target": _target(target_type, target_id), "location": json_pointer, "message": message}
    finding: JSONDict = {
        "finding_id": f"val_{canonical_hash(identity)[:16]}",
        "rule": rule,
        "severity": severity,
        "target": identity["target"],
        "message": message,
        "location": {"json_pointer": json_pointer},
        "suggested_action": suggested_action,
    }
    if related_target is not None:
        finding["related_target"] = related_target
    if edge is not None:
        finding["edge"] = edge
    return finding


@dataclass
class _Collector:
    findings: list[JSONDict] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)

    def check(
        self,
        rule: str,
        failed: bool,
        target_type: str,
        target_id: Any,
        message: str,
        pointer: str,
        action: str,
        *,
        severity: str = "error",
        related_target: JSONDict | None = None,
        edge: JSONDict | None = None,
    ) -> None:
        self.checked[rule] = self.checked.get(rule, 0) + 1
        if failed:
            self.findings.append(make_validation_finding(
                rule, severity, target_type, target_id, message, pointer, action,
                related_target=related_target, edge=edge,
            ))


def _object_list(value: Any) -> list[JSONDict] | None:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return [dict(item) for item in value]


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return None
    return list(value)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def collect_snapshot_findings(payload: Mapping[str, Any], *, expected_step: int | None = None) -> tuple[list[JSONDict], dict[str, int]]:
    collector = _Collector()
    snapshot_id = _text(payload.get("snapshot_id")) or "snapshot"
    collector.check("snapshot.schema_name", payload.get("schema_name") != FORMALIZATION_SNAPSHOT_SCHEMA,
                    "snapshot", snapshot_id, f"Expected {FORMALIZATION_SNAPSHOT_SCHEMA}", "/schema_name",
                    "Use the versioned formalization snapshot schema name.")
    collector.check("snapshot.schema_version", payload.get("schema_version") != SCHEMA_VERSION,
                    "snapshot", snapshot_id, f"Expected schema version {SCHEMA_VERSION}", "/schema_version",
                    "Migrate the snapshot to the supported schema version.")
    collector.check("snapshot.id_required", _text(payload.get("snapshot_id")) is None,
                    "snapshot", snapshot_id, "snapshot_id must be a non-empty string", "/snapshot_id",
                    "Assign a stable snapshot ID.")
    step = payload.get("step")
    step_number = step.get("number") if isinstance(step, dict) else None
    step_name = step.get("name") if isinstance(step, dict) else None
    collector.check("snapshot.step_contract", step_number not in STEP_NAMES or step_name != STEP_NAMES.get(step_number),
                    "snapshot", snapshot_id, "Step number and name do not match the workflow contract", "/step",
                    "Use the configured name for this step number.")
    collector.check("snapshot.expected_step", expected_step is not None and step_number != expected_step,
                    "snapshot", snapshot_id, f"Expected Step {expected_step}, found {step_number}", "/step/number",
                    "Emit the snapshot from the matching StagePlugin.")

    collections: dict[str, list[JSONDict]] = {}
    for field_name in ("inputs", "knowledge", "reasoning_units", "operators", "non_reasoning_links", "source_anchors"):
        values = _object_list(payload.get(field_name, []))
        collector.check("snapshot.collection_shape", values is None, "snapshot", snapshot_id,
                        f"{field_name} must be a list of objects", f"/{field_name}",
                        f"Replace {field_name} with an array of objects.")
        collections[field_name] = values or []
    collector.check("snapshot.changes_shape", not isinstance(payload.get("changes", {}), dict),
                    "snapshot", snapshot_id, "changes must be an object", "/changes", "Emit a changes object.")

    knowledge = collections["knowledge"]
    knowledge_ids: set[str] = set()
    knowledge_by_id: dict[str, JSONDict] = {}
    for index, item in enumerate(knowledge):
        pointer = f"/knowledge/{index}"
        item_id = _text(item.get("id"))
        target_id = item_id or f"knowledge[{index}]"
        collector.check("knowledge.id_required", item_id is None, "knowledge", target_id,
                        "Knowledge ID must be a non-empty string", f"{pointer}/id", "Assign a stable knowledge ID.")
        if item_id:
            collector.check("knowledge.id_unique", item_id in knowledge_ids, "knowledge", item_id,
                            f"Duplicate knowledge ID: {item_id}", f"{pointer}/id", "Deduplicate or rename the knowledge object.")
            knowledge_ids.add(item_id)
            knowledge_by_id[item_id] = item
        collector.check("knowledge.type", item.get("type") not in KNOWLEDGE_TYPES, "knowledge", target_id,
                        f"Unsupported knowledge type: {item.get('type')}", f"{pointer}/type", "Use a supported knowledge type.")
        content = item.get("content")
        collector.check("knowledge.canonical_text", not isinstance(content, dict) or not isinstance(content.get("canonical"), str),
                        "knowledge", target_id, "content.canonical must be a string", f"{pointer}/content/canonical",
                        "Provide canonical claim text.")
        collector.check("claim.self_contained", item.get("type") == "claim" and item.get("self_contained") is not True,
                        "knowledge", target_id, "Claim is not marked self-contained", f"{pointer}/self_contained",
                        "Rewrite the claim to be independently understandable, then set self_contained=true.")
        visibility = item.get("visibility", "public")
        collector.check("knowledge.visibility", visibility not in {"public", "formal_internal"}, "knowledge", target_id,
                        f"Unsupported visibility: {visibility}", f"{pointer}/visibility", "Use public or formal_internal.")
        epistemic = item.get("epistemic", {})
        collector.check("knowledge.epistemic_shape", not isinstance(epistemic, dict), "knowledge", target_id,
                        "epistemic must be an object", f"{pointer}/epistemic", "Emit a structured epistemic object.")
        collector.check("knowledge.private_prior", visibility == "formal_internal" and isinstance(epistemic, dict) and epistemic.get("prior_status") == "assigned",
                        "knowledge", target_id, "Formal internal claim cannot carry an assigned prior",
                        f"{pointer}/epistemic/prior_status", "Remove the prior from the private helper claim.")
        collector.check("knowledge.source_anchor_shape", _string_list(item.get("source_anchor_ids", [])) is None,
                        "knowledge", target_id, "source_anchor_ids must be a list of strings",
                        f"{pointer}/source_anchor_ids", "Normalize source anchors to a string array.")

    anchors = collections["source_anchors"]
    anchor_ids: set[str] = set()
    for index, anchor in enumerate(anchors):
        pointer = f"/source_anchors/{index}"
        anchor_id = _text(anchor.get("anchor_id"))
        target_id = anchor_id or f"source_anchor[{index}]"
        collector.check("source_anchor.id_required", anchor_id is None, "source_anchor", target_id,
                        "anchor_id must be a non-empty string", f"{pointer}/anchor_id", "Assign a stable source anchor ID.")
        if anchor_id:
            collector.check("source_anchor.id_unique", anchor_id in anchor_ids, "source_anchor", anchor_id,
                            f"Duplicate source anchor ID: {anchor_id}", f"{pointer}/anchor_id", "Deduplicate the source anchor.")
            anchor_ids.add(anchor_id)
        for field_name in ("artifact_id", "source_kind"):
            collector.check(f"source_anchor.{field_name}_required", _text(anchor.get(field_name)) is None,
                            "source_anchor", target_id, f"{field_name} must be a non-empty string",
                            f"{pointer}/{field_name}", f"Provide {field_name}.")
        locator = anchor.get("locator")
        locator_type = locator.get("type") if isinstance(locator, dict) else None
        collector.check("source_anchor.locator", not isinstance(locator, dict) or locator_type not in SOURCE_LOCATOR_TYPES,
                        "source_anchor", target_id, f"Unsupported source locator: {locator_type}",
                        f"{pointer}/locator", "Use markdown_span, json_pointer, or figure_region.")
        if isinstance(locator, dict) and locator_type == "markdown_span":
            start, end = locator.get("start_line"), locator.get("end_line")
            collector.check("source_anchor.markdown_span", not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start,
                            "source_anchor", target_id, "Invalid Markdown line span", f"{pointer}/locator",
                            "Use 1-based line numbers and end_line >= start_line.")
        elif isinstance(locator, dict) and locator_type == "json_pointer":
            value = locator.get("pointer")
            collector.check("source_anchor.json_pointer", not isinstance(value, str) or not value.startswith("/"),
                            "source_anchor", target_id, "Invalid JSON Pointer", f"{pointer}/locator/pointer",
                            "Provide an RFC 6901 pointer beginning with '/'.")
        elif isinstance(locator, dict) and locator_type == "figure_region":
            bbox = locator.get("bbox")
            invalid_bbox = bbox is not None and (not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in bbox))
            collector.check("source_anchor.figure_bbox", invalid_bbox, "source_anchor", target_id,
                            "Figure bbox must contain four normalized numbers", f"{pointer}/locator/bbox",
                            "Normalize the bounding box to [0,1].")
    for index, item in enumerate(knowledge):
        item_id = _text(item.get("id")) or f"knowledge[{index}]"
        source_ids = item.get("source_anchor_ids", []) if isinstance(item.get("source_anchor_ids", []), list) else []
        for source_index, anchor_id in enumerate(source_ids):
            collector.check("knowledge.source_anchors_closed", anchor_id not in anchor_ids, "knowledge", item_id,
                            f"Missing source anchor: {anchor_id}", f"/knowledge/{index}/source_anchor_ids/{source_index}",
                            "Add the source anchor or remove the stale reference.", related_target=_target("source_anchor", anchor_id))

    strategies = collections["reasoning_units"]
    strategy_ids: set[str] = set()
    private_owners: dict[str, str] = {}
    for index, strategy in enumerate(strategies):
        pointer = f"/reasoning_units/{index}"
        strategy_id = _text(strategy.get("id"))
        target_id = strategy_id or f"reasoning_unit[{index}]"
        collector.check("strategy.id_required", strategy_id is None, "reasoning_unit", target_id,
                        "Reasoning unit ID must be a non-empty string", f"{pointer}/id", "Assign a stable reasoning unit ID.")
        if strategy_id:
            collector.check("strategy.id_unique", strategy_id in strategy_ids, "reasoning_unit", strategy_id,
                            f"Duplicate reasoning unit ID: {strategy_id}", f"{pointer}/id", "Deduplicate the reasoning unit.")
            strategy_ids.add(strategy_id)
        form = strategy.get("form")
        collector.check("strategy.form", form not in STRATEGY_FORMS, "reasoning_unit", target_id,
                        f"Unsupported reasoning form: {form}", f"{pointer}/form", "Use coarse or formal.")
        premises = _string_list(strategy.get("premises", []))
        collector.check("strategy.premises_shape", premises is None, "reasoning_unit", target_id,
                        "premises must be a list of knowledge IDs", f"{pointer}/premises", "Normalize premises to a string array.")
        premises = premises or []
        conclusion = _text(strategy.get("conclusion"))
        collector.check("strategy.conclusion_required", conclusion is None, "reasoning_unit", target_id,
                        "conclusion must be a knowledge ID", f"{pointer}/conclusion", "Reference a conclusion claim.")
        background = _string_list(strategy.get("background", []))
        collector.check("strategy.background_shape", background is None, "reasoning_unit", target_id,
                        "background must be a list of knowledge IDs", f"{pointer}/background", "Normalize background to a string array.")
        for premise_index, premise in enumerate(premises):
            edge_id = f"strategy:{target_id}:premise:{premise_index}"
            collector.check("strategy.references_closed", premise not in knowledge_ids, "edge", edge_id,
                            f"Premise {premise} does not exist", f"{pointer}/premises/{premise_index}",
                            "Add the premise claim or remove the stale edge.", related_target=_target("reasoning_unit", target_id),
                            edge={"source": premise, "target": target_id, "role": "premise"})
        if conclusion:
            edge_id = f"strategy:{target_id}:conclusion"
            collector.check("strategy.references_closed", conclusion not in knowledge_ids, "edge", edge_id,
                            f"Conclusion {conclusion} does not exist", f"{pointer}/conclusion",
                            "Add the conclusion claim or remove the stale edge.", related_target=_target("reasoning_unit", target_id),
                            edge={"source": target_id, "target": conclusion, "role": "conclusion"})
        for background_index, background_id in enumerate(background or []):
            edge_id = f"strategy:{target_id}:background:{background_index}"
            collector.check("strategy.references_closed", background_id not in knowledge_ids, "edge", edge_id,
                            f"Background claim {background_id} does not exist", f"{pointer}/background/{background_index}",
                            "Add the background claim or remove the stale edge.", related_target=_target("reasoning_unit", target_id),
                            edge={"source": background_id, "target": target_id, "role": "background"})
        for interface_id in [*premises, *([conclusion] if conclusion else [])]:
            collector.check("strategy.interface_claims", interface_id in knowledge_by_id and knowledge_by_id[interface_id].get("type") != "claim",
                            "reasoning_unit", target_id, f"Strategy interface {interface_id} is not a claim", pointer,
                            "Use claim objects at reasoning interfaces.", related_target=_target("knowledge", interface_id))
        collector.check("strategy.form_consistency", form == "formal" and strategy.get("coarse") is not None,
                        "reasoning_unit", target_id, "Formal reasoning unit cannot retain coarse parameters",
                        f"{pointer}/coarse", "Remove coarse parameters after expansion.")
        coarse = strategy.get("coarse")
        collector.check("strategy.coarse_required", form == "coarse" and not isinstance(coarse, dict),
                        "reasoning_unit", target_id, "Coarse reasoning unit requires a coarse object",
                        f"{pointer}/coarse", "Provide coarse classification and uncertainty fields.")
        soft = coarse.get("soft_implication") if isinstance(coarse, dict) else None
        if soft is not None:
            valid_soft = isinstance(soft, dict)
            if valid_soft:
                p1, p2 = soft.get("p1"), soft.get("p2")
                valid_soft = isinstance(p1, (int, float)) and isinstance(p2, (int, float)) and 0 < p1 <= 1 and 0 <= p2 <= 1 and p1 + p2 > 1
            collector.check("strategy.soft_implication", not valid_soft, "reasoning_unit", target_id,
                            "Invalid soft implication parameters", f"{pointer}/coarse/soft_implication",
                            "Use 0 < p1 <= 1, 0 <= p2 <= 1, and p1 + p2 > 1.")
        formal = strategy.get("formal")
        collector.check("strategy.formal_required", form == "formal" and not isinstance(formal, dict),
                        "reasoning_unit", target_id, "Formal reasoning unit requires a formal object",
                        f"{pointer}/formal", "Provide private_claims and operator_ids.")
        private_ids = _string_list(formal.get("private_claims", [])) if isinstance(formal, dict) else []
        collector.check("strategy.private_claims_shape", private_ids is None, "reasoning_unit", target_id,
                        "private_claims must be a list of IDs", f"{pointer}/formal/private_claims",
                        "Normalize private_claims to a string array.")
        for private_index, private_id in enumerate(private_ids or []):
            collector.check("strategy.private_claims_closed", private_id not in knowledge_ids, "reasoning_unit", target_id,
                            f"Private claim {private_id} does not exist", f"{pointer}/formal/private_claims/{private_index}",
                            "Add the private helper claim.", related_target=_target("knowledge", private_id))
            collector.check("strategy.private_claim_visibility", private_id in knowledge_by_id and knowledge_by_id[private_id].get("visibility") != "formal_internal",
                            "knowledge", private_id, "Private claim must be formal_internal",
                            f"{pointer}/formal/private_claims/{private_index}", "Set private helper visibility to formal_internal.",
                            related_target=_target("reasoning_unit", target_id))
            collector.check("strategy.private_claim_owner", private_id in private_owners, "knowledge", private_id,
                            "Private claim has multiple owning strategies", f"{pointer}/formal/private_claims/{private_index}",
                            "Give each private helper exactly one owner.", related_target=_target("reasoning_unit", target_id))
            private_owners[private_id] = target_id

    operators = collections["operators"]
    operator_ids: set[str] = set()
    for index, operator in enumerate(operators):
        pointer = f"/operators/{index}"
        operator_id = _text(operator.get("id"))
        target_id = operator_id or f"operator[{index}]"
        collector.check("operator.id_required", operator_id is None, "operator", target_id,
                        "Operator ID must be a non-empty string", f"{pointer}/id", "Assign a stable operator ID.")
        if operator_id:
            collector.check("operator.id_unique", operator_id in operator_ids, "operator", operator_id,
                            f"Duplicate operator ID: {operator_id}", f"{pointer}/id", "Deduplicate the operator.")
            operator_ids.add(operator_id)
        collector.check("operator.type", operator.get("type") not in OPERATOR_TYPES, "operator", target_id,
                        f"Unsupported operator type: {operator.get('type')}", f"{pointer}/type", "Use a supported operator.")
        variables = _string_list(operator.get("variables", []))
        collector.check("operator.variables_shape", variables is None, "operator", target_id,
                        "variables must be a list of knowledge IDs", f"{pointer}/variables", "Normalize variables to a string array.")
        variables = variables or []
        conclusion = _text(operator.get("conclusion"))
        for variable_index, variable in enumerate(variables):
            edge_id = f"operator:{target_id}:input:{variable_index}"
            collector.check("operator.references_closed", variable not in knowledge_ids, "edge", edge_id,
                            f"Operator input {variable} does not exist", f"{pointer}/variables/{variable_index}",
                            "Add the input knowledge object or repair the edge.", related_target=_target("operator", target_id),
                            edge={"source": variable, "target": target_id, "role": "input"})
        edge_id = f"operator:{target_id}:output"
        collector.check("operator.references_closed", conclusion is None or conclusion not in knowledge_ids, "edge", edge_id,
                        f"Operator output {conclusion} does not exist", f"{pointer}/conclusion",
                        "Add the output knowledge object or repair the edge.", related_target=_target("operator", target_id),
                        edge={"source": target_id, "target": conclusion, "role": "output"})
        collector.check("operator.output_not_input", conclusion is not None and conclusion in variables, "operator", target_id,
                        "Operator conclusion cannot also be an input", f"{pointer}/conclusion",
                        "Introduce a separate structural result claim.")
        collector.check("operator.no_probability", any(name in operator for name in ("p1", "p2", "probability")),
                        "operator", target_id, "Deterministic operator cannot carry probabilities", pointer,
                        "Keep uncertainty on coarse reasoning units.")
    for index, strategy in enumerate(strategies):
        strategy_id = _text(strategy.get("id")) or f"reasoning_unit[{index}]"
        formal = strategy.get("formal")
        refs = _string_list(formal.get("operator_ids", [])) if isinstance(formal, dict) else []
        collector.check("strategy.operator_ids_shape", refs is None, "reasoning_unit", strategy_id,
                        "operator_ids must be a list of IDs", f"/reasoning_units/{index}/formal/operator_ids",
                        "Normalize operator_ids to a string array.")
        for ref_index, operator_id in enumerate(refs or []):
            collector.check("strategy.operators_closed", operator_id not in operator_ids, "reasoning_unit", strategy_id,
                            f"Referenced operator {operator_id} does not exist",
                            f"/reasoning_units/{index}/formal/operator_ids/{ref_index}",
                            "Add the operator or remove the stale reference.", related_target=_target("operator", operator_id))

    link_ids: set[str] = set()
    for index, link in enumerate(collections["non_reasoning_links"]):
        pointer = f"/non_reasoning_links/{index}"
        link_id = _text(link.get("id"))
        target_id = link_id or f"edge[{index}]"
        collector.check("edge.id_required", link_id is None, "edge", target_id,
                        "Edge ID must be a non-empty string", f"{pointer}/id", "Assign a stable edge ID.")
        if link_id:
            collector.check("edge.id_unique", link_id in link_ids, "edge", link_id,
                            f"Duplicate edge ID: {link_id}", f"{pointer}/id", "Deduplicate the edge.")
            link_ids.add(link_id)
        collector.check("edge.type", link.get("link_type") not in NON_REASONING_LINK_TYPES, "edge", target_id,
                        f"Unsupported edge type: {link.get('link_type')}", f"{pointer}/link_type", "Use a supported link type.")
        collector.check("edge.non_reasoning_flag", link.get("reasoning") is not False, "edge", target_id,
                        "Non-reasoning edge must set reasoning=false", f"{pointer}/reasoning",
                        "Set reasoning=false or model a reasoning unit.")
        source, target = link.get("source"), link.get("target")
        descriptor = {"source": source, "target": target, "role": link.get("link_type")}
        collector.check("edge.references_closed", not isinstance(source, str) or source not in knowledge_ids, "edge", target_id,
                        f"Edge source {source} does not exist", f"{pointer}/source", "Repair the edge source.", edge=descriptor)
        collector.check("edge.references_closed", not isinstance(target, str) or target not in knowledge_ids, "edge", target_id,
                        f"Edge target {target} does not exist", f"{pointer}/target", "Repair the edge target.", edge=descriptor)

    public_interfaces = {
        value for strategy in strategies
        for value in [*(strategy.get("premises", []) if isinstance(strategy.get("premises"), list) else []), strategy.get("conclusion")]
        if isinstance(value, str) and value
    }
    for private_id in sorted(set(private_owners) & public_interfaces):
        collector.check("formal.private_isolation", True, "knowledge", private_id,
                        "Private claim is referenced by a public strategy interface", "/reasoning_units",
                        "Keep the private helper inside its owning strategy.",
                        related_target=_target("reasoning_unit", private_owners[private_id]))
    if isinstance(step_number, int) and step_number >= 4 and payload.get("formalization_level") == "fine":
        for index, strategy in enumerate(strategies):
            collector.check("formalization.level_consistency", strategy.get("form") == "coarse", "reasoning_unit",
                            strategy.get("id", f"reasoning_unit[{index}]"), "fine formalization contains coarse reasoning",
                            f"/reasoning_units/{index}/form", "Expand the unit or mark the formalization mixed.")
    return collector.findings, collector.checked


def build_validation_report(
    payload: Mapping[str, Any],
    *,
    expected_step: int | None = None,
    additional_findings: list[JSONDict] | None = None,
) -> JSONDict:
    findings, checked = collect_snapshot_findings(payload, expected_step=expected_step)
    findings.extend(additional_findings or [])
    errors = sum(item.get("severity") == "error" for item in findings)
    warnings = sum(item.get("severity") == "warning" for item in findings)
    status = "failed" if errors else "warning" if warnings else "passed"
    all_rules = sorted(set(checked) | {str(item["rule"]) for item in findings})
    by_rule = {rule: [item for item in findings if item["rule"] == rule] for rule in all_rules}
    checks = [{
        "rule": rule,
        "status": "failed" if any(item["severity"] == "error" for item in items) else "warning" if items else "passed",
        "checked_objects": checked.get(rule, len(items)),
        "finding_ids": [item["finding_id"] for item in items],
    } for rule, items in by_rule.items()]
    snapshot_id = str(payload.get("snapshot_id", "unknown"))
    return {
        "schema_name": VALIDATION_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "report_id": f"validation_{snapshot_id}",
        "artifact_under_test": {"kind": FORMALIZATION_SNAPSHOT_KIND, "snapshot_id": snapshot_id, "content_hash": canonical_hash(payload)},
        "validator": {"name": "gaia-formalization-validator", "version": VALIDATOR_VERSION},
        "step": payload.get("step", {}).get("number") if isinstance(payload.get("step"), dict) else expected_step,
        "summary": {"status": status, "errors": errors, "warnings": warnings, "findings": len(findings)},
        "checks": checks,
        "findings": findings,
    }


def validate_snapshot(payload: Mapping[str, Any], *, expected_step: int | None = None) -> None:
    report = build_validation_report(payload, expected_step=expected_step)
    first = next((item for item in report["findings"] if item["severity"] == "error"), None)
    if first is not None:
        raise ValueError(f"{first['rule']}: {first['message']}")


def validate_source_anchor(anchor: Mapping[str, Any]) -> None:
    payload = {
        "schema_name": FORMALIZATION_SNAPSHOT_SCHEMA, "schema_version": SCHEMA_VERSION,
        "snapshot_id": "anchor_validation", "step": {"number": 1, "name": STEP_NAMES[1]},
        "inputs": [], "knowledge": [], "reasoning_units": [], "operators": [],
        "non_reasoning_links": [], "source_anchors": [dict(anchor)], "changes": {},
    }
    findings, _ = collect_snapshot_findings(payload)
    finding = next((item for item in findings if item["target"]["type"] == "source_anchor"), None)
    if finding:
        raise ValueError(f"{finding['rule']}: {finding['message']}")
