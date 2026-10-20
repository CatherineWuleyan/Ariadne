"""Step 1: freeze and mechanically validate Gaia Package inputs."""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from pipeline_harness.domain.compiler import (
    _validate_compiler_input,
    _validate_with_official_gaia,
)
from pipeline_harness.domain.indexing import validate_knowledge_index
from pipeline_harness.models import ArtifactRef, Finding, JSONDict
from pipeline_harness.plugins import StageContext, StageResult
from pipeline_harness.store import RunStore, sha256_file

from .step0 import validate_step0_options


class Step1InputError(ValueError):
    """A fail-closed, mechanically detected Step 1 input error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _read_json(context: StageContext, ref: ArtifactRef) -> JSONDict:
    path = context.artifact_path(ref)
    if sha256_file(path) != ref.sha256:
        raise Step1InputError(
            "STEP1_INPUT_HASH_MISMATCH",
            f"frozen artifact hash does not match ArtifactRef: {ref.artifact_id}",
        )
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Step1InputError(
            "STEP1_INPUT_INVALID",
            f"artifact {ref.artifact_id} is not a readable JSON document: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise Step1InputError(
            "STEP1_INPUT_INVALID",
            f"artifact {ref.artifact_id} must contain a JSON object",
        )
    return value


def _step0_options(context: StageContext) -> JSONDict:
    stages = RunStore(context.run_dir).load_run().config.get("stages", [])
    matches = [
        stage
        for stage in stages
        if isinstance(stage, dict) and stage.get("name") == "step0_select_scope"
    ]
    if len(matches) != 1:
        raise Step1InputError(
            "STEP1_SCOPE_MISSING",
            "Step 1 requires exactly one frozen step0_select_scope stage",
        )
    try:
        return validate_step0_options(matches[0].get("options", {}))
    except ValueError as exc:
        raise Step1InputError("STEP1_SCOPE_INVALID", str(exc)) from exc


def _package_identity(ir: Mapping[str, Any], artifact_id: str) -> str:
    namespace = ir.get("namespace")
    package_name = ir.get("package_name")
    if not isinstance(namespace, str) or not namespace:
        raise Step1InputError(
            "STEP1_GAIA_IR_INVALID", f"gaia.ir {artifact_id} has no namespace"
        )
    if not isinstance(package_name, str) or not package_name:
        raise Step1InputError(
            "STEP1_GAIA_IR_INVALID", f"gaia.ir {artifact_id} has no package_name"
        )
    return f"{namespace}:{package_name}"


def _validate_gaia_ir(payload: JSONDict, artifact_id: str) -> tuple[JSONDict, str]:
    try:
        return _validate_with_official_gaia(payload)
    except RuntimeError as exc:
        raise Step1InputError("GAIA_VALIDATOR_UNAVAILABLE", str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise Step1InputError(
            "STEP1_GAIA_IR_INVALID",
            f"gaia.ir {artifact_id} failed official validation: {exc}",
        ) from exc


def _formalization_package(document: Mapping[str, Any], artifact_id: str) -> tuple[str, str]:
    try:
        _validate_compiler_input(document)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise Step1InputError(
            "STEP1_FORMALIZATION_INVALID",
            f"formalization {artifact_id} is invalid: {exc}",
        ) from exc
    package = document.get("package")
    if not isinstance(package, Mapping):
        raise Step1InputError(
            "STEP1_FORMALIZATION_INVALID",
            f"formalization {artifact_id} has no package object",
        )
    namespace = package.get("namespace")
    name = package.get("name")
    paper_id = package.get("paper_id")
    if not all(isinstance(value, str) and value for value in (namespace, name, paper_id)):
        raise Step1InputError(
            "STEP1_FORMALIZATION_INVALID",
            f"formalization {artifact_id} has incomplete package identity",
        )
    return f"{namespace}:{name}", paper_id


def _knowledge_pairs(ir: Mapping[str, Any]) -> set[tuple[str, str | None]]:
    result: set[tuple[str, str | None]] = set()
    for item in ir.get("knowledges", []):
        if not isinstance(item, Mapping):
            continue
        metadata = item.get("metadata")
        local_id = metadata.get("source_knowledge_id") if isinstance(metadata, Mapping) else None
        if not isinstance(local_id, str) or not local_id:
            label = item.get("label")
            local_id = label if isinstance(label, str) and label else None
        if local_id is not None:
            content = item.get("content")
            result.add((local_id, content if isinstance(content, str) else None))
    return result


def _bind_indexes(
    indexes: list[tuple[ArtifactRef, JSONDict]],
    packages: Mapping[str, JSONDict],
) -> dict[str, str]:
    package_pairs = {identity: _knowledge_pairs(ir) for identity, ir in packages.items()}
    bindings: dict[str, str] = {}
    for ref, index in indexes:
        try:
            validate_knowledge_index(index)
        except (TypeError, ValueError) as exc:
            raise Step1InputError(
                "STEP1_KNOWLEDGE_INDEX_INVALID",
                f"knowledge.index {ref.artifact_id} is invalid: {exc}",
            ) from exc
        expected = {
            (entry["knowledge_id"], entry.get("canonical_text"))
            for entry in index["entries"]
            if isinstance(entry, Mapping)
        }
        candidates = [
            identity
            for identity, actual in package_pairs.items()
            if expected and expected.issubset(actual)
        ]
        if len(candidates) != 1:
            raise Step1InputError(
                "STEP1_OPTIONAL_BINDING_INVALID",
                f"knowledge.index {ref.artifact_id} must match exactly one gaia.ir Package; matched {len(candidates)}",
            )
        bindings[ref.artifact_id] = candidates[0]
    return bindings


def _selection_targets(ir: Mapping[str, Any], identity: str) -> set[str]:
    targets = {identity, identity.split(":", 1)[1]}
    for item in ir.get("knowledges", []):
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            targets.add(item["id"])
    for graph in ir.get("formula_graphs", []):
        if not isinstance(graph, Mapping):
            continue
        for field in ("id", "name"):
            if isinstance(graph.get(field), str) and graph[field]:
                targets.add(graph[field])
    return targets


def validate_step1_inputs(context: StageContext) -> JSONDict:
    """Validate required Gaia IR and any supplied authoring/retrieval companions."""
    selection = _step0_options(context)
    gaia_refs = context.find_all("gaia.ir")
    if not gaia_refs:
        raise Step1InputError(
            "STEP1_GAIA_IR_REQUIRED", "Step 1 requires at least one gaia.ir input"
        )

    packages: dict[str, JSONDict] = {}
    schemas: dict[str, str] = {}
    for ref in gaia_refs:
        normalized, schema = _validate_gaia_ir(_read_json(context, ref), ref.artifact_id)
        identity = _package_identity(normalized, ref.artifact_id)
        if identity in packages:
            raise Step1InputError(
                "STEP1_PACKAGE_DUPLICATE",
                f"multiple gaia.ir inputs declare Package {identity}",
            )
        packages[identity] = normalized
        schemas[identity] = schema

    paper_aliases: dict[str, set[str]] = defaultdict(set)
    formalization_bindings: dict[str, str] = {}
    for ref in context.find_all("formalization"):
        document = _read_json(context, ref)
        identity, paper_id = _formalization_package(document, ref.artifact_id)
        if identity not in packages:
            raise Step1InputError(
                "STEP1_OPTIONAL_BINDING_INVALID",
                f"formalization {ref.artifact_id} does not match an input gaia.ir Package",
            )
        if identity in formalization_bindings.values():
            raise Step1InputError(
                "STEP1_OPTIONAL_BINDING_INVALID",
                f"multiple formalizations were supplied for Package {identity}",
            )
        formalization_bindings[ref.artifact_id] = identity
        paper_aliases[paper_id].add(identity)

    index_documents = [
        (ref, _read_json(context, ref))
        for ref in context.find_all("knowledge.index")
    ]
    index_bindings = _bind_indexes(index_documents, packages)

    target_packages: dict[str, set[str]] = defaultdict(set)
    target_kinds: dict[str, str] = {}
    for identity, ir in packages.items():
        for target in _selection_targets(ir, identity):
            target_packages[target].add(identity)
            target_kinds[target] = "package" if target in {identity, identity.split(":", 1)[1]} else "graph_member"
    for paper_id, identities in paper_aliases.items():
        target_packages[paper_id].update(identities)
        target_kinds[paper_id] = "package"

    selected_packages: set[str] = set()
    selected_kinds: set[str] = set()
    unresolved: list[str] = []
    ambiguous: list[str] = []
    for target in selection["scope"]["selection"]:
        matches = target_packages.get(target, set())
        if not matches:
            unresolved.append(target)
        elif len(matches) > 1:
            ambiguous.append(target)
        else:
            selected_packages.update(matches)
            selected_kinds.add(target_kinds[target])
    if unresolved:
        raise Step1InputError(
            "STEP1_SELECTION_UNRESOLVED",
            f"Step 0 selections do not resolve against frozen Gaia inputs: {unresolved}",
        )
    if ambiguous:
        raise Step1InputError(
            "STEP1_SELECTION_AMBIGUOUS",
            f"Step 0 selections resolve to multiple Packages: {ambiguous}",
        )

    mode = selection["mode"]
    all_packages = set(packages)
    if mode == "bootstrap":
        if selected_kinds != {"package"} or selected_packages != all_packages:
            raise Step1InputError(
                "STEP1_MODE_INPUT_MISMATCH",
                "bootstrap selections must identify every frozen gaia.ir Package",
            )
    elif mode == "incremental":
        if selected_kinds != {"package"}:
            raise Step1InputError(
                "STEP1_MODE_INPUT_MISMATCH",
                "incremental selections must identify new Gaia Packages",
            )
        if not (all_packages - selected_packages):
            raise Step1InputError(
                "STEP1_MODE_INPUT_MISMATCH",
                "incremental requires at least one unselected gaia.ir Package as the current domain baseline",
            )

    return {
        "validated": True,
        "mode": mode,
        "domain": selection["scope"]["domain"],
        "package_count": len(packages),
        "selected_packages": sorted(selected_packages),
        "baseline_packages": sorted(all_packages - selected_packages),
        "gaia_ir_schemas": schemas,
        "formalization_bindings": formalization_bindings,
        "knowledge_index_bindings": index_bindings,
    }


class Step1FreezeInputsPlugin:
    """Validate frozen inputs without creating a second input or domain artifact."""

    def run(self, context: StageContext) -> StageResult:
        try:
            metadata = validate_step1_inputs(context)
        except Step1InputError as exc:
            return StageResult(
                "failed",
                findings=[Finding(exc.code, "error", str(exc))],
            )
        return StageResult("succeeded", metadata=metadata)
