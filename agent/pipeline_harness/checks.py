from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import ArtifactRef, Checkpoint, Finding, RunRecord
from .store import RunStore, read_json, resolve_within, sha256_file, sha256_json
from .view.model import ViewDocument


def _finding(
    code: str,
    message: str,
    *,
    artifact_id: str | None = None,
    **details: Any,
) -> Finding:
    return Finding(
        code=code,
        severity="error",
        message=message,
        artifact_id=artifact_id,
        details=details,
    )


def check_run(run_dir: Path | str) -> list[Finding]:
    store = RunStore(run_dir)
    findings: list[Finding] = []
    if not store.run_dir.is_dir():
        return [
            _finding(
                "RUN_DIRECTORY_MISSING",
                f"run directory does not exist: {store.run_dir}",
            )
        ]

    run: RunRecord | None = None
    try:
        run = store.load_run()
    except Exception as exc:
        findings.append(_finding("RUN_INVALID", f"run.json is invalid: {exc}"))

    artifacts: dict[str, ArtifactRef] = {}
    try:
        artifacts = store.artifact_map()
    except Exception as exc:
        findings.append(_finding("ARTIFACT_INDEX_INVALID", f"artifact index is invalid: {exc}"))

    for artifact_id, artifact in artifacts.items():
        try:
            path = store.artifact_path(artifact)
        except Exception as exc:
            findings.append(
                _finding(
                    "ARTIFACT_PATH_INVALID",
                    f"artifact path is invalid: {exc}",
                    artifact_id=artifact_id,
                )
            )
            continue
        if not path.is_file():
            findings.append(
                _finding(
                    "ARTIFACT_FILE_MISSING",
                    f"artifact file does not exist: {artifact.path}",
                    artifact_id=artifact_id,
                )
            )
            continue
        actual_hash = sha256_file(path)
        if actual_hash != artifact.sha256:
            findings.append(
                _finding(
                    "ARTIFACT_HASH_MISMATCH",
                    f"artifact hash mismatch for {artifact_id}",
                    artifact_id=artifact_id,
                    expected=artifact.sha256,
                    actual=actual_hash,
                )
            )

    checkpoints: dict[str, Checkpoint] = {}
    checkpoint_dir = store.run_dir / "checkpoints"
    if not checkpoint_dir.is_dir():
        findings.append(
            _finding("CHECKPOINT_DIRECTORY_MISSING", "checkpoints directory is missing")
        )
    else:
        for path in sorted(checkpoint_dir.glob("*.json")):
            try:
                checkpoint = Checkpoint.from_dict(read_json(path))
                if checkpoint.checkpoint_id in checkpoints:
                    raise ValueError(f"duplicate checkpoint ID: {checkpoint.checkpoint_id}")
                checkpoints[checkpoint.checkpoint_id] = checkpoint
                for artifact_id in checkpoint.artifacts:
                    if artifact_id not in artifacts:
                        findings.append(
                            _finding(
                                "CHECKPOINT_DANGLING_ARTIFACT",
                                f"checkpoint {checkpoint.checkpoint_id} references "
                                f"unknown artifact {artifact_id}",
                                artifact_id=artifact_id,
                                checkpoint_id=checkpoint.checkpoint_id,
                            )
                        )
                    else:
                        harness_metadata = artifacts[artifact_id].metadata.get("_harness")
                        declared_status = (
                            harness_metadata.get("declared_status")
                            if isinstance(harness_metadata, dict)
                            else None
                        )
                        if declared_status in {"waiting", "failed"}:
                            findings.append(
                                _finding(
                                    "CHECKPOINT_NON_SUCCESS_ARTIFACT",
                                    f"checkpoint {checkpoint.checkpoint_id} contains "
                                    f"a {declared_status} artifact",
                                    artifact_id=artifact_id,
                                    checkpoint_id=checkpoint.checkpoint_id,
                                )
                            )
            except Exception as exc:
                findings.append(
                    _finding("CHECKPOINT_INVALID", f"checkpoint {path.name} is invalid: {exc}")
                )

    if run is not None:
        actual_config_hash = sha256_json(run.config)
        if actual_config_hash != run.config_sha256:
            findings.append(
                _finding(
                    "CONFIG_HASH_MISMATCH",
                    "run config does not match its frozen hash",
                    expected=run.config_sha256,
                    actual=actual_config_hash,
                )
            )
        stages = run.config.get("stages", [])
        if isinstance(stages, list):
            if run.next_stage_index > len(stages):
                findings.append(
                    _finding("RUN_STAGE_INDEX_INVALID", "next_stage_index exceeds stage count")
                )
            if run.status == "succeeded" and (
                run.next_stage_index != len(stages) or run.current_stage is not None
            ):
                findings.append(
                    _finding(
                        "RUN_TERMINAL_STATE_INCONSISTENT",
                        "succeeded run must be past the final stage with no current_stage",
                    )
                )
        if run.last_checkpoint_id is not None and run.last_checkpoint_id not in checkpoints:
            findings.append(
                _finding(
                    "RUN_CHECKPOINT_MISSING",
                    f"run references missing checkpoint {run.last_checkpoint_id}",
                )
            )

        for artifact_id in run.input_artifacts:
            artifact = artifacts.get(artifact_id)
            if artifact is None:
                findings.append(
                    _finding(
                        "INPUT_ARTIFACT_MISSING",
                        f"run input references unknown artifact {artifact_id}",
                        artifact_id=artifact_id,
                    )
                )
            elif artifact.producer_stage != "input":
                findings.append(
                    _finding(
                        "INPUT_PRODUCER_INVALID",
                        f"run input {artifact_id} must have producer_stage=input",
                        artifact_id=artifact_id,
                    )
                )

    input_manifest: dict[str, Any] | None = None
    if not store.input_manifest_path.is_file():
        findings.append(_finding("INPUT_MANIFEST_MISSING", "inputs/manifest.json is missing"))
    else:
        try:
            input_manifest = read_json(store.input_manifest_path)
            if input_manifest.get("schema_version") != "1":
                raise ValueError("schema_version must be 1")
            entries = input_manifest.get("artifacts")
            if not isinstance(entries, list):
                raise ValueError("artifacts must be a list")
            manifest_ids: list[str] = []
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise ValueError(f"artifact {index} must be an object")
                artifact_id = entry.get("artifact_id")
                if not isinstance(artifact_id, str) or not artifact_id:
                    raise ValueError(f"artifact {index} artifact_id must be a string")
                if artifact_id in manifest_ids:
                    raise ValueError(f"duplicate input artifact ID: {artifact_id}")
                manifest_ids.append(artifact_id)
                artifact = artifacts.get(artifact_id)
                if artifact is None:
                    findings.append(
                        _finding(
                            "INPUT_MANIFEST_DANGLING_ARTIFACT",
                            f"input manifest references unknown artifact {artifact_id}",
                            artifact_id=artifact_id,
                        )
                    )
                    continue
                for field in ("kind", "media_type", "sha256"):
                    if entry.get(field) != getattr(artifact, field):
                        findings.append(
                            _finding(
                                "INPUT_MANIFEST_MISMATCH",
                                f"input manifest {field} is stale for {artifact_id}",
                                artifact_id=artifact_id,
                                field=field,
                                expected=getattr(artifact, field),
                                actual=entry.get(field),
                            )
                        )
            if run is not None and manifest_ids != run.input_artifacts:
                findings.append(
                    _finding(
                        "INPUT_MANIFEST_MISMATCH",
                        "input manifest artifact IDs do not match run.input_artifacts",
                        expected=run.input_artifacts,
                        actual=manifest_ids,
                    )
                )
        except Exception as exc:
            findings.append(
                _finding("INPUT_MANIFEST_INVALID", f"input manifest is invalid: {exc}")
            )

    if run is not None and run.parent_run_id is not None:
        parent_store = RunStore(store.run_dir.parent / run.parent_run_id)
        if not parent_store.run_dir.is_dir():
            findings.append(
                _finding(
                    "PARENT_RUN_MISSING",
                    f"parent run directory does not exist: {parent_store.run_dir}",
                )
            )
        else:
            try:
                parent_run = parent_store.load_run()
                if parent_run.run_id != run.parent_run_id:
                    findings.append(
                        _finding(
                            "PARENT_RUN_ID_MISMATCH",
                            "parent run directory contains a different run_id",
                            expected=run.parent_run_id,
                            actual=parent_run.run_id,
                        )
                    )
                actual_parent_config_hash = sha256_json(parent_run.config)
                if actual_parent_config_hash != parent_run.config_sha256:
                    findings.append(
                        _finding(
                            "PARENT_CONFIG_HASH_MISMATCH",
                            "parent config does not match its frozen hash",
                            expected=parent_run.config_sha256,
                            actual=actual_parent_config_hash,
                        )
                    )
                if parent_run.config_sha256 != run.config_sha256:
                    findings.append(
                        _finding(
                            "PARENT_CONFIG_MISMATCH",
                            "child and parent config hashes differ",
                            expected=parent_run.config_sha256,
                            actual=run.config_sha256,
                        )
                    )
                parent_checkpoint: Checkpoint | None = None
                try:
                    parent_checkpoint = parent_store.load_checkpoint(
                        str(run.parent_checkpoint_id)
                    )
                except Exception as exc:
                    findings.append(
                        _finding(
                            "PARENT_CHECKPOINT_MISSING",
                            f"parent checkpoint is unavailable: {exc}",
                        )
                    )
                parent_artifacts = parent_store.artifact_map()
                expected_source_ids = set(parent_run.input_artifacts)
                if parent_checkpoint is not None:
                    expected_source_ids.update(parent_checkpoint.artifacts)
                copied_source_ids: set[str] = set()
                for artifact_id, artifact in artifacts.items():
                    fork_metadata = artifact.metadata.get("_fork")
                    if fork_metadata is None:
                        continue
                    if not isinstance(fork_metadata, dict):
                        findings.append(
                            _finding(
                                "FORK_SOURCE_INVALID",
                                "_fork metadata must be an object",
                                artifact_id=artifact_id,
                            )
                        )
                        continue
                    source_id = fork_metadata.get("source_artifact_id")
                    source = parent_artifacts.get(source_id)
                    if isinstance(source_id, str):
                        copied_source_ids.add(source_id)
                    if (
                        fork_metadata.get("source_run_id") != parent_run.run_id
                        or source is None
                    ):
                        findings.append(
                            _finding(
                                "FORK_SOURCE_MISSING",
                                f"fork source is unavailable for {artifact_id}",
                                artifact_id=artifact_id,
                            )
                        )
                    elif (
                        fork_metadata.get("source_sha256") != source.sha256
                        or artifact.sha256 != source.sha256
                    ):
                        findings.append(
                            _finding(
                                "FORK_SOURCE_HASH_MISMATCH",
                                f"forked artifact hash differs from parent for {artifact_id}",
                                artifact_id=artifact_id,
                            )
                        )
                for source_id in sorted(expected_source_ids - copied_source_ids):
                    findings.append(
                        _finding(
                            "FORK_SOURCE_MISSING",
                            f"fork provenance is missing for parent artifact {source_id}",
                            artifact_id=source_id,
                        )
                    )
                if input_manifest is not None:
                    forked_from = input_manifest.get("forked_from")
                    expected_fork = {
                        "run_id": run.parent_run_id,
                        "checkpoint_id": run.parent_checkpoint_id,
                    }
                    if forked_from != expected_fork:
                        findings.append(
                            _finding(
                                "FORK_MANIFEST_MISMATCH",
                                "input manifest lineage does not match run parent fields",
                                expected=expected_fork,
                                actual=forked_from,
                            )
                        )
            except Exception as exc:
                findings.append(_finding("PARENT_RUN_INVALID", f"parent run is invalid: {exc}"))

    events_path = store.run_dir / "events.ndjson"
    if not events_path.is_file():
        findings.append(_finding("EVENT_LOG_MISSING", "events.ndjson is missing"))
    else:
        with events_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                        raise ValueError("event must be an object with an event string")
                except Exception as exc:
                    findings.append(
                        _finding(
                            "EVENT_INVALID",
                            f"events.ndjson line {line_number} is invalid: {exc}",
                            line=line_number,
                        )
                    )

    view_path = store.run_dir / "views/view_model.json"
    manifest_path = store.run_dir / "views/manifest.json"
    if view_path.exists():
        try:
            view = ViewDocument.from_dict(read_json(view_path))
            for artifact_id in view.source_artifacts:
                if artifact_id not in artifacts:
                    findings.append(
                        _finding(
                            "VIEW_DANGLING_SOURCE",
                            f"view references unknown source artifact {artifact_id}",
                            artifact_id=artifact_id,
                        )
                    )
        except Exception as exc:
            findings.append(_finding("VIEW_INVALID", f"view model is invalid: {exc}"))
    if manifest_path.exists():
        try:
            manifest = read_json(manifest_path)
            relative_view_path = manifest.get("view_model")
            if not isinstance(relative_view_path, str):
                raise ValueError("manifest view_model must be a string")
            declared_view_path = resolve_within(store.run_dir, relative_view_path)
            if not declared_view_path.is_file():
                raise ValueError("declared view model does not exist")
            expected_hash = manifest.get("view_sha256")
            actual_hash = sha256_file(declared_view_path)
            if expected_hash != actual_hash:
                findings.append(
                    _finding(
                        "VIEW_HASH_MISMATCH",
                        "view model hash does not match manifest",
                        expected=expected_hash,
                        actual=actual_hash,
                    )
                )
            sources = manifest.get("source_artifacts", [])
            if not isinstance(sources, list):
                raise ValueError("manifest source_artifacts must be a list")
            for source in sources:
                if not isinstance(source, dict):
                    raise ValueError("manifest source artifact must be an object")
                artifact_id = source.get("artifact_id")
                if artifact_id not in artifacts:
                    findings.append(
                        _finding(
                            "VIEW_MANIFEST_DANGLING_SOURCE",
                            f"view manifest references unknown artifact {artifact_id}",
                        )
                    )
                elif source.get("sha256") != artifacts[artifact_id].sha256:
                    findings.append(
                        _finding(
                            "VIEW_SOURCE_HASH_MISMATCH",
                            f"view manifest source hash is stale for {artifact_id}",
                            artifact_id=artifact_id,
                        )
                    )
            viewer = manifest.get("viewer")
            if not isinstance(viewer, str) or not resolve_within(store.run_dir, viewer).is_file():
                findings.append(_finding("VIEWER_MISSING", "view manifest viewer is missing"))
        except Exception as exc:
            findings.append(_finding("VIEW_MANIFEST_INVALID", f"view manifest is invalid: {exc}"))

    findings.extend(_formalization_connectivity_findings(store, artifacts))

    return findings


def _formalization_connectivity_findings(
    store: RunStore, artifacts: Mapping[str, ArtifactRef]
) -> list[Finding]:
    """Report reasoning-graph fragmentation the official IR validation cannot see."""

    from .domain.graph import audit_formalization_connectivity

    candidates: list[tuple[int, ArtifactRef]] = []
    for artifact in artifacts.values():
        if artifact.kind != "formalization":
            continue
        step = artifact.metadata.get("step")
        candidates.append((step if isinstance(step, int) else 0, artifact))
    if not candidates:
        return []
    step, artifact = max(candidates, key=lambda item: item[0])
    try:
        document = read_json(store.artifact_path(artifact))
    except Exception as exc:
        return [
            _finding(
                "FORMALIZATION_UNREADABLE",
                f"formalization artifact {artifact.artifact_id} cannot be read: {exc}",
                artifact_id=artifact.artifact_id,
            )
        ]

    report = audit_formalization_connectivity(document)
    findings: list[Finding] = []
    if report["floating_node_count"]:
        findings.append(
            Finding(
                code="GRAPH_FLOATING_CLAIMS",
                severity="warning",
                message=(
                    f"step {step} reasoning graph has {report['floating_node_count']} Knowledge "
                    "node(s) in no operator or strategy"
                ),
                artifact_id=artifact.artifact_id,
                details={
                    "step": step,
                    "floating_node_ids": report["floating_node_ids"],
                    "component_count": report["component_count"],
                    "largest_component_ratio": report["largest_component_ratio"],
                },
            )
        )
    if report["component_count"] > 1:
        findings.append(
            Finding(
                code="GRAPH_DISCONNECTED",
                severity="warning",
                message=(
                    f"step {step} reasoning graph has {report['component_count']} independent "
                    f"components; the largest holds {report['largest_component_ratio']:.0%} of nodes"
                ),
                artifact_id=artifact.artifact_id,
                details={
                    "step": step,
                    "component_sizes": report["component_sizes"],
                    "largest_component_size": report["largest_component_size"],
                    "node_count": report["node_count"],
                },
            )
        )
    return findings
