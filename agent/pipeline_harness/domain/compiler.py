from __future__ import annotations

import copy

import hashlib
from collections.abc import Mapping
from typing import Any

from ..models import Finding, JSONDict
from ..plugins import ArtifactDraft, StageContext, StageResult, instantiate
from ..store import RunStore, atomic_write_json
from .authoring import validate_formalization
from .contracts import SCHEMA_VERSION, canonical_hash
from .graph import audit_formalization_connectivity
from .indexing import validate_knowledge_index
from .runtime import emit_snapshot, inherit_snapshot, read_json_ref
from .tools import DomainTool, ToolCallRequest, ToolCallResponse, validate_tool_response


V2_FORMALIZATION_FIELDS = {
    "schema_version", "revision", "package", "graph", "knowledges", "workflow",
}


def _is_v2_formalization(payload: Mapping[str, Any]) -> bool:
    return set(payload) == V2_FORMALIZATION_FIELDS and payload.get("schema_version") == "1.1.0"


def _validate_compiler_input(payload: Mapping[str, Any]) -> bool:
    """Validate either existing authoring contract without introducing a third one."""

    if _is_v2_formalization(payload):
        try:
            from agent_pipeline_v2.authoring import validate as validate_v2
        except ImportError as exc:
            raise RuntimeError(
                "V2/V3 formalization requires the active agent_pipeline_v2 package"
            ) from exc
        validate_v2(payload)
        return True
    validate_formalization(payload)
    return False


def _qid(namespace: str, package_name: str, local_id: str) -> str:
    from gaia.engine.ir.knowledge import is_qid, make_qid

    qid = make_qid(namespace, package_name, local_id)
    if not is_qid(qid):
        raise ValueError(f"cannot map local Knowledge ID to an official Gaia QID: {local_id!r}")
    return qid


def _helper_name(operator: str, variables: list[str]) -> str:
    if operator == "conjunction":
        return f"all_true({','.join(variables)})"
    if operator == "disjunction":
        return f"any_true({','.join(variables)})"
    if operator == "negation":
        return f"not({variables[0]})"
    if operator == "equivalence":
        return f"same_truth({variables[0]},{variables[1]})"
    if operator == "contradiction":
        return f"not_both_true({variables[0]},{variables[1]})"
    if operator == "complement":
        return f"opposite_truth({variables[0]},{variables[1]})"
    if operator == "implication":
        return f"implies({variables[0]},{variables[1]})"
    raise ValueError(f"unsupported authoring Operator type: {operator}")


def _compile_v2_formalization(
    document: Mapping[str, Any], *, namespace: str, package_name: str,
) -> tuple[JSONDict, str]:
    """Mechanically lower the shared V2/V3 authoring contract to Gaia 0.5 IR."""

    try:
        from gaia._meta import IR_SCHEMA
        from gaia.engine.ir.graphs import LocalCanonicalGraph
        from gaia.engine.ir.knowledge import Knowledge, is_qid
        from gaia.engine.ir.operator import Operator
        from gaia.engine.ir.strategy import Strategy
        from gaia.engine.ir.validator import validate_local_graph
    except ImportError as exc:
        raise RuntimeError("official Gaia package is unavailable") from exc

    graph = document["graph"]
    if graph["composes"]:
        raise ValueError("V2/V3 graph.composes is not mapped to Gaia 0.5; refusing compilation")
    registry = document["knowledges"]
    graph_ids = list(graph["nodes"])
    for operator in graph["operators"]:
        for knowledge_id in operator.get("background", []):
            if knowledge_id not in graph_ids:
                graph_ids.append(knowledge_id)
    for strategy in graph.get("strategies", []):
        for knowledge_id in strategy["background"]:
            if knowledge_id not in graph_ids:
                graph_ids.append(knowledge_id)

    # Integration Packages may reference already-qualified Knowledge owned by
    # Paper Packages.  Preserve those external identities; mint a local QID
    # only for Integration-owned labels such as candidate K and helper claims.
    bindings = {
        knowledge_id: knowledge_id if is_qid(knowledge_id) else _qid(namespace, package_name, knowledge_id)
        for knowledge_id in graph_ids
    }
    knowledges = []
    for knowledge_id in graph_ids:
        source = registry[knowledge_id]
        content = source["content"]
        # Authoring keeps AltExp placeholders non-factual (content=null), but
        # Gaia's LocalCanonicalGraph requires every local Knowledge node to
        # carry text. Materialize only this interface placeholder at the
        # lowering boundary; the authoring contract and viewer semantics stay
        # explicitly null/non-factual.
        if content is None and "AltExp" in str(knowledge_id):
            content = {"canonical": "Unknown alternative explanation (content=null)"}
        knowledges.append(Knowledge(
            id=bindings[knowledge_id],
            label=knowledge_id,
            type="claim" if source["type"] == "observation_claim" else source["type"],
            content=content["canonical"] if content is not None else None,
            metadata={
                **(dict(source.get("metadata", {})) if isinstance(source.get("metadata"), Mapping) else {}),
                "source_knowledge_id": knowledge_id,
                "source_anchor_ids": list(source.get("source_anchor_ids", [])),
            },
        ))

    operators = []
    for source in graph["operators"]:
        operator_type = source.get("type")
        if not isinstance(operator_type, str):
            raise ValueError("authoring Operator requires a type")
        local_variables = list(source.get("variables", []))
        explicit_conclusion = source.get("conclusion")
        if explicit_conclusion is not None and not isinstance(explicit_conclusion, str):
            raise ValueError(f"authoring Operator {source.get('id')} has an invalid conclusion")

        if explicit_conclusion is not None and operator_type in {
            "negation", "conjunction", "disjunction", "equivalence", "contradiction",
        }:
            variables = [bindings[key] for key in local_variables]
            conclusion = bindings[explicit_conclusion]
        else:
            relation_ids = list(local_variables)
            if explicit_conclusion is not None and explicit_conclusion not in relation_ids:
                relation_ids.append(explicit_conclusion)
            variables = [bindings[key] for key in relation_ids]
            # Pydantic checks the official arity before any graph is emitted.
            canonical_name = _helper_name(operator_type, variables)
            source_id = str(source.get("id", canonical_name))
            digest = hashlib.sha256(
                f"{operator_type}|{variables}|{source_id}".encode("utf-8")
            ).hexdigest()[:16]
            helper_label = f"__operator_result_{digest}"
            conclusion = _qid(namespace, package_name, helper_label)
            knowledges.append(Knowledge(
                id=conclusion,
                label=helper_label,
                type="claim",
                content=canonical_name,
                metadata={
                    "generated": True,
                    "generated_kind": "helper_claim",
                    "helper_kind": f"{operator_type}_result",
                    "helper_visibility": "formal_internal",
                    "source_operator_id": source_id,
                },
            ))
        source_id = str(source.get("id", ""))
        operator_digest = hashlib.sha256(
            f"{operator_type}|{variables}|{conclusion}|{source_id}".encode("utf-8")
        ).hexdigest()[:16]
        operators.append(Operator(
            operator_id=f"lco_{operator_digest}",
            scope="local",
            operator=operator_type,
            variables=variables,
            conclusion=conclusion,
            metadata={
                **dict(source.get("metadata", {})),
                "source_operator_id": source_id,
                "background": [bindings[key] for key in source.get("background", [])],
            },
        ))

    strategies = []
    for source in graph.get("strategies", []):
        local_strategy_id = source["strategy_id"]
        leaf = Strategy.model_validate({
            "scope": source["scope"],
            "type": source["type"],
            "premises": [bindings[key] for key in source["premises"]],
            "conclusion": bindings[source["conclusion"]],
            "background": [bindings[key] for key in source["background"]],
            "metadata": {"source_strategy_id": local_strategy_id},
        })
        if source["type"] == "infer":
            strategies.append(leaf)
            continue
        formalized = leaf.formalize(namespace=namespace, package_name=package_name)
        knowledges.extend(formalized.knowledges)
        strategies.append(formalized.strategy)

    official = LocalCanonicalGraph(
        namespace=namespace,
        package_name=package_name,
        knowledges=knowledges,
        operators=operators,
        strategies=strategies,
        composes=[],
        formula_graphs=[],
    )
    validation = validate_local_graph(official)
    if not validation.valid:
        raise ValueError("; ".join(validation.errors))
    return official.model_dump(mode="json"), IR_SCHEMA


class Gaia05OfficialCompilerTool:
    """Shared official compiler for the byte-identical V2/V3 authoring contract."""

    name = "gaia-0.5-official-compiler"
    # The approved single-paper baseline uses the Gaia 0.5.0a7 prerelease.
    version = "0.5.0a7"

    def invoke(self, request: ToolCallRequest) -> ToolCallResponse:
        try:
            from gaia._meta import get_library_version
        except ImportError as exc:
            raise RuntimeError("official Gaia package is unavailable") from exc
        runtime_version = get_library_version()
        if runtime_version != self.version:
            raise RuntimeError(
                f"Gaia version drift: compiler requires {self.version}, found {runtime_version}"
            )
        if request.operation != "compile_formalization":
            raise ValueError(f"unsupported compiler operation: {request.operation}")
        formalization = request.parameters.get("formalization")
        namespace = request.parameters.get("namespace")
        package_name = request.parameters.get("package_name")
        if not isinstance(formalization, dict):
            raise ValueError("compile_formalization requires a formalization object")
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("compile_formalization requires namespace")
        if not isinstance(package_name, str) or not package_name:
            raise ValueError("compile_formalization requires package_name")
        if not _is_v2_formalization(formalization):
            raise ValueError("Gaia05OfficialCompilerTool accepts the shared V2/V3 authoring contract")
        _validate_compiler_input(formalization)
        graph, ir_schema = _compile_v2_formalization(
            formalization, namespace=namespace, package_name=package_name,
        )
        return ToolCallResponse(
            request.call_id,
            "succeeded",
            {
                "compiler": self.name,
                "gaia_version": runtime_version,
                "ir_schema": ir_schema,
            },
            {"gaia_ir": graph},
            metadata={
                "contract_status": "official",
                "gaia_version": runtime_version,
                "ir_schema": ir_schema,
            },
        )


def _v2_first_seen_steps(context: StageContext, final_document: Mapping[str, Any]) -> dict[str, int]:
    """Derive introduction steps from the immutable formalization Artifact chain."""

    store = RunStore(context.run_dir)
    by_step: dict[int, Mapping[str, Any]] = {}
    for ref in store.load_artifacts():
        step = ref.metadata.get("step")
        if ref.kind != "formalization" or not isinstance(step, int) or step > 4:
            continue
        payload = read_json_ref(context, ref)
        if _is_v2_formalization(payload):
            by_step[step] = payload
    if 4 not in by_step or by_step[4]["revision"]["content_hash"] != final_document["revision"]["content_hash"]:
        raise ValueError("knowledge index requires the current Step 4 formalization in the Artifact chain")
    first_seen: dict[str, int] = {}
    for step, payload in sorted(by_step.items()):
        for knowledge_id in payload["knowledges"]:
            first_seen.setdefault(knowledge_id, step)
    return first_seen


def _build_v2_knowledge_index(
    document: Mapping[str, Any], *, first_seen_steps: Mapping[str, int],
) -> JSONDict:
    """Derive the existing final index artifact from V2/V3 Step 4 truth."""

    graph = document["graph"]
    registry = document["knowledges"]
    referenced = list(graph["nodes"])
    for operator in graph["operators"]:
        for knowledge_id in operator.get("background", []):
            if knowledge_id not in referenced:
                referenced.append(knowledge_id)
    for strategy in graph.get("strategies", []):
        for knowledge_id in strategy["background"]:
            if knowledge_id not in referenced:
                referenced.append(knowledge_id)
    incoming: dict[str, list[str]] = {}
    outgoing: dict[str, list[str]] = {}
    internal_helpers = {
        operator["conclusion"]
        for operator in graph["operators"]
        if isinstance(operator.get("conclusion"), str)
        and operator.get("metadata", {}).get("derived_ast_helper") is True
    }
    for operator in graph["operators"]:
        operator_id = operator["id"]
        for variable in operator["variables"]:
            outgoing.setdefault(variable, []).append(operator_id)
        conclusion = operator.get("conclusion")
        if isinstance(conclusion, str):
            incoming.setdefault(conclusion, []).append(operator_id)
        for background in operator.get("background", []):
            outgoing.setdefault(background, []).append(operator_id)
    for strategy in graph.get("strategies", []):
        strategy_id = strategy["strategy_id"]
        incoming.setdefault(strategy["conclusion"], []).append(strategy_id)
        for premise in strategy["premises"]:
            outgoing.setdefault(premise, []).append(strategy_id)
        for background in strategy["background"]:
            outgoing.setdefault(background, []).append(strategy_id)
    entries = []
    for knowledge_id in referenced:
        if knowledge_id not in first_seen_steps:
            raise ValueError(f"knowledge index has no Artifact provenance for {knowledge_id}")
        knowledge = registry[knowledge_id]
        canonical = (
            knowledge["content"]["canonical"] if knowledge["content"] is not None else ""
        )
        entries.append({
            "knowledge_id": knowledge_id,
            "type": "claim" if knowledge["type"] == "observation_claim" else knowledge["type"],
            "title": None,
            "canonical_text": canonical,
            "content_hash": canonical_hash({"type": knowledge["type"], "content": canonical}),
            "languages": [],
            "external_ids": [],
            "source_anchor_ids": list(knowledge.get("source_anchor_ids", [])),
            "first_seen_step": first_seen_steps[knowledge_id],
            "current_step": 5,
            "incoming_reasoning": sorted(incoming.get(knowledge_id, [])),
            "outgoing_reasoning": sorted(outgoing.get(knowledge_id, [])),
            "non_reasoning_links": [],
            "visibility": "formal_internal" if knowledge_id in internal_helpers else "public",
            "search_text": " ".join(filter(None, [knowledge_id, canonical])),
        })
    return {
        "schema_name": "gaia.knowledge.index",
        "schema_version": SCHEMA_VERSION,
        "source": {
            "snapshot_id": document["revision"]["revision_id"],
            "step": 4,
            "snapshot_hash": document["revision"]["content_hash"],
        },
        "entries": entries,
        "external_id_map": {},
        "source_anchors": list(document["workflow"]["source_anchors"]),
    }


def _validate_with_official_gaia(payload: JSONDict) -> tuple[JSONDict, str]:
    """Parse, validate, and canonicalize with the installed official Gaia package."""

    try:
        from gaia._meta import IR_SCHEMA
        from gaia.engine.ir.graphs import LocalCanonicalGraph
        from gaia.engine.ir.validator import validate_local_graph
    except ImportError as exc:
        raise RuntimeError("official Gaia package is unavailable") from exc

    unexpected = set(payload) - set(LocalCanonicalGraph.model_fields)
    if unexpected:
        raise ValueError(f"official Gaia IR contains unsupported fields: {sorted(unexpected)}")
    graph = LocalCanonicalGraph.model_validate(payload)
    result = validate_local_graph(graph)
    if not result.valid:
        raise ValueError("; ".join(result.errors))
    return graph.model_dump(mode="json"), IR_SCHEMA


class Step5CompileGaiaIRPlugin:
    def run(self, context: StageContext) -> StageResult:
        formalization_ref = context.require_one("formalization")
        formalization = read_json_ref(context, formalization_ref)
        is_v2 = _validate_compiler_input(formalization)
        snapshot = None
        if not is_v2:
            _, snapshot = inherit_snapshot(context, 5)
        compiler_spec = context.options.get("compiler_plugin")
        if not isinstance(compiler_spec, str):
            return StageResult(
                status="waiting",
                findings=[Finding(code="GAIA_COMPILER_REQUIRED", severity="warning", message="Step 5 requires an explicit compiler_plugin")],
                metadata={"step": 5, "required_option": "compiler_plugin"},
            )
        compiler = instantiate(compiler_spec)
        if not isinstance(compiler, DomainTool):
            raise TypeError(f"{compiler_spec} does not implement DomainTool")
        package = formalization["package"]
        namespace = str(context.options.get("namespace", package["namespace"]))
        package_name = str(context.options.get("package_name", package["name"]))
        request = ToolCallRequest(
            call_id=f"gaia_compile_{context.run_id}_{context.attempt}",
            tool_name=compiler.name, tool_version=compiler.version, operation="compile_formalization",
            inputs=[{"artifact_id": formalization_ref.artifact_id, "kind": formalization_ref.kind, "sha256": formalization_ref.sha256}],
            parameters={"namespace": namespace, "package_name": package_name, "formalization": formalization},
        )
        try:
            response = compiler.invoke(request)
            if not isinstance(response, ToolCallResponse):
                raise TypeError(f"{compiler_spec} returned {type(response).__name__}, not ToolCallResponse")
            validate_tool_response(request, response)
        except Exception as exc:
            response = ToolCallResponse(request.call_id, "failed", None, error={"type": type(exc).__name__, "message": str(exc)})

        audit_request = request.to_dict()
        audit_request["parameters"] = {
            "namespace": namespace, "package_name": package_name,
            "formalization_ref": request.inputs[0], "formalization_hash": canonical_hash(formalization),
        }
        response_path = context.work_dir / "gaia_compile_response.json"
        atomic_write_json(response_path, {"request": audit_request, "response": response.to_dict()})
        response_draft = ArtifactDraft(response_path, "tool.gaia_compile.response", "application/json", {
            "schema_version": SCHEMA_VERSION, "step": 5, "tool_call_id": request.call_id, "status": response.status,
        })
        salvage_finding = None
        if response.status == "failed":
            strategies = formalization.get("graph", {}).get("strategies", []) if is_v2 else []
            candidate = copy.deepcopy(formalization)
            rejected: list[str] = []
            recovered = None
            while isinstance(strategies, list) and candidate["graph"].get("strategies"):
                for index, strategy in enumerate(list(candidate["graph"]["strategies"])):
                    trial = copy.deepcopy(candidate)
                    trial["graph"]["strategies"].pop(index)
                    trial["revision"]["content_hash"] = ""
                    trial["revision"]["content_hash"] = canonical_hash(trial)
                    try:
                        recovered = compiler.invoke(ToolCallRequest(
                            f"{request.call_id}_salvage_{len(rejected)}_{index}", compiler.name, compiler.version,
                            "compile_formalization", request.inputs,
                            {"namespace": namespace, "package_name": package_name, "formalization": trial},
                        ))
                        if recovered.status == "succeeded":
                            candidate = trial
                            rejected.append(str(strategy.get("strategy_id", index)))
                            break
                    except Exception:
                        recovered = None
                else:
                    break
            if recovered is not None and recovered.status == "succeeded":
                response = recovered
                formalization = candidate
                salvage_finding = Finding(
                    "GAIA_COMPILER_STRATEGY_REJECTED", "warning",
                    "Compiled remaining strategies after isolating invalid strategies",
                    details={"strategy_ids": rejected},
                )
            else:
                return StageResult(
                    status="failed", artifacts=[response_draft],
                    findings=[Finding(code="GAIA_COMPILER_FAILED", severity="error", message=str(response.error.get("message", "Gaia compiler failed")), details={"tool_call_id": request.call_id, "error": response.error})],
                    metadata={"step": 5, "tool_call_id": request.call_id},
                )
        normalized_ir = (response.normalized or {}).get("gaia_ir")
        if not isinstance(normalized_ir, dict):
            return StageResult(
                status="failed", artifacts=[response_draft],
                findings=[Finding(code="GAIA_COMPILER_OUTPUT_INVALID", severity="error", message="Compiler normalized output must contain gaia_ir")],
                metadata={"step": 5, "tool_call_id": request.call_id},
            )
        if response.metadata.get("contract_status") != "official":
            return StageResult(
                status="failed", artifacts=[response_draft],
                findings=[Finding(
                    code="GAIA_COMPILER_CONTRACT_UNDECLARED", severity="error",
                    message="Compiler must explicitly declare metadata.contract_status='official'",
                    details={"tool_call_id": request.call_id, "compiler_plugin": compiler_spec},
                )],
                metadata={"step": 5, "tool_call_id": request.call_id},
            )
        try:
            normalized_ir, ir_schema = _validate_with_official_gaia(normalized_ir)
        except (RuntimeError, ValueError) as exc:
            return StageResult(
                status="failed", artifacts=[response_draft],
                findings=[Finding(
                    code="GAIA_COMPILER_OUTPUT_INVALID", severity="error", message=str(exc),
                    details={"tool_call_id": request.call_id, "validator": "gaia.engine.ir.validator.validate_local_graph"},
                )],
                metadata={"step": 5, "tool_call_id": request.call_id},
            )
        normalized_path = context.work_dir / "gaia_ir.json"
        atomic_write_json(normalized_path, normalized_ir)
        gaia_draft = ArtifactDraft(normalized_path, "gaia.ir", "application/json", {
            "ir_schema": ir_schema, "step": 5,
            "tool_call_id": request.call_id, "contract_status": "official",
            "validation_status": "passed",
        })
        connectivity = audit_formalization_connectivity(formalization) if is_v2 else None
        connectivity_findings: list[Finding] = []
        if connectivity is not None and connectivity["floating_node_count"]:
            connectivity_findings.append(Finding(
                code="GRAPH_FLOATING_CLAIMS", severity="warning",
                message=(
                    f"{connectivity['floating_node_count']} Knowledge node(s) carry no reasoning edge; "
                    "the compiled IR contains them as isolated graph nodes"
                ),
                details={
                    "floating_node_ids": connectivity["floating_node_ids"],
                    "component_count": connectivity["component_count"],
                    "largest_component_ratio": connectivity["largest_component_ratio"],
                },
            ))
        if connectivity is not None and connectivity["component_count"] > 1:
            connectivity_findings.append(Finding(
                code="GRAPH_DISCONNECTED", severity="warning",
                message=(
                    f"reasoning graph has {connectivity['component_count']} independent components; "
                    f"the largest holds {connectivity['largest_component_ratio']:.0%} of its nodes"
                ),
                details={
                    "component_sizes": connectivity["component_sizes"],
                    "node_count": connectivity["node_count"],
                },
            ))
        connectivity_metadata = connectivity or {}
        if is_v2:
            try:
                index = _build_v2_knowledge_index(
                    formalization,
                    first_seen_steps=_v2_first_seen_steps(context, formalization),
                )
                validate_knowledge_index(index)
            except ValueError as exc:
                return StageResult(
                    status="succeeded", artifacts=[gaia_draft, response_draft],
                    findings=[Finding(
                        code="KNOWLEDGE_INDEX_INVALID", severity="warning", message=str(exc),
                        details={"step": 5, "formalization_revision_id": formalization["revision"]["revision_id"]},
                    )] + ([salvage_finding] if salvage_finding else []) + connectivity_findings,
                    metadata={"step": 5, "tool_call_id": request.call_id, "index_status": "not_written",
                              "connectivity": connectivity_metadata},
                )
            index_path = context.work_dir / "knowledge_index.json"
            atomic_write_json(index_path, index)
            return StageResult(
                status="succeeded",
                artifacts=[
                    gaia_draft,
                    ArtifactDraft(index_path, "knowledge.index", "application/json", {
                        "schema_name": "gaia.knowledge.index", "schema_version": SCHEMA_VERSION,
                        "logical_name": index_path.name, "step": 5,
                        "revision_id": formalization["revision"]["revision_id"], "final": True,
                    }),
                    response_draft,
                ],
                findings=([salvage_finding] if salvage_finding else []) + connectivity_findings,
                metadata={
                    "step": 5, "tool_call_id": request.call_id,
                    "formalization_revision_id": formalization["revision"]["revision_id"],
                    "validation_status": "passed",
                    "connectivity": connectivity_metadata,
                },
            )
        assert snapshot is not None
        snapshot["formalization_revision_id"] = formalization["revision"]["revision_id"]
        snapshot["formalization_content_hash"] = formalization["revision"]["content_hash"]
        snapshot["compile"] = {
            "tool_call_id": request.call_id, "output_kind": "gaia.ir",
            "output_hash": canonical_hash(normalized_ir), "contract_status": "official",
        }
        snapshot["review"] = {
            "status": snapshot.get("review", {}).get("status", "needs_review"),
            "issues": snapshot.get("review", {}).get("issues", []),
        }
        emitted = emit_snapshot(context, 5, snapshot, emit_final_index=True)
        if emitted.status != "succeeded":
            return emitted
        return StageResult(
            status="succeeded",
            artifacts=[
                *emitted.artifacts,
                gaia_draft,
                response_draft,
            ],
            findings=emitted.findings,
            metadata=emitted.metadata,
        )
