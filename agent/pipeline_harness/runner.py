from __future__ import annotations

import traceback
import uuid
from pathlib import Path
from typing import Any

from .models import ArtifactRef, Finding, RunRecord
from .plugins import StageContext, StagePlugin, StageResult, instantiate
from .store import RunStore, sha256_json


def _stage_input_refs(store: RunStore, run: RunRecord) -> list[ArtifactRef]:
    artifacts = store.artifact_map()
    artifact_ids = list(run.input_artifacts)
    if run.last_checkpoint_id is not None:
        checkpoint = store.load_checkpoint(run.last_checkpoint_id)
        artifact_ids.extend(checkpoint.artifacts)
    seen: set[str] = set()
    refs: list[ArtifactRef] = []
    for artifact_id in artifact_ids:
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        refs.append(artifacts[artifact_id])
    return refs


def _record_finding(run: RunRecord, stage: str, attempt: int, finding: Finding) -> None:
    run.findings.append(
        {"stage": stage, "attempt": attempt, **finding.to_dict()}
    )


def _plugin_failure(code: str, message: str, **details: Any) -> Finding:
    return Finding(code=code, severity="error", message=message, details=details)


def run_pipeline(
    run_dir: Path | str,
    *,
    max_stages: int | None = None,
) -> RunRecord:
    if max_stages is not None and max_stages <= 0:
        raise ValueError("max_stages must be positive")
    store = RunStore(run_dir)
    run = store.load_run()
    if sha256_json(run.config) != run.config_sha256:
        raise ValueError("run config does not match its frozen hash")
    stages = run.config.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("run config has no stages")
    if run.status == "succeeded":
        return run

    completed_this_call = 0
    while run.next_stage_index < len(stages):
        if max_stages is not None and completed_this_call >= max_stages:
            run.status = "waiting"
            run.current_stage = stages[run.next_stage_index]["name"]
            store.save_run(run)
            store.append_event("run_paused", {"reason": "max_stages"})
            return run

        stage_config = stages[run.next_stage_index]
        if not isinstance(stage_config, dict):
            raise ValueError("stage config must be an object")
        stage_name = stage_config.get("name")
        plugin_spec = stage_config.get("plugin")
        options = stage_config.get("options", {})
        if not isinstance(stage_name, str) or not stage_name:
            raise ValueError("stage name must be a non-empty string")
        if not isinstance(plugin_spec, str):
            raise ValueError(f"stage {stage_name} has no plugin")
        if not isinstance(options, dict):
            raise ValueError(f"stage {stage_name} options must be an object")

        attempt = run.attempts.get(stage_name, 0) + 1
        run.attempts[stage_name] = attempt
        run.status = "running"
        run.current_stage = stage_name
        run.waiting = None
        store.save_run(run)
        store.append_event(
            "stage_started", {"stage": stage_name, "attempt": attempt, "plugin": plugin_spec}
        )

        work_dir = (
            store.run_dir
            / "work"
            / stage_name
            / f"attempt_{attempt}_{uuid.uuid4().hex[:8]}"
        )
        work_dir.mkdir(parents=True, exist_ok=False)
        context = StageContext(
            run_id=run.run_id,
            run_dir=store.run_dir,
            work_dir=work_dir,
            stage_name=stage_name,
            attempt=attempt,
            inputs=_stage_input_refs(store, run),
            options=dict(options),
        )

        try:
            plugin = instantiate(plugin_spec)
            if not isinstance(plugin, StagePlugin):
                raise TypeError(f"{plugin_spec} does not implement StagePlugin")
            result = plugin.run(context)
            if not isinstance(result, StageResult):
                raise TypeError(f"{plugin_spec} returned {type(result).__name__}, not StageResult")
        except Exception as exc:
            result = StageResult(
                status="failed",
                findings=[
                    _plugin_failure(
                        "PLUGIN_EXCEPTION",
                        f"stage plugin raised {type(exc).__name__}: {exc}",
                        exception_type=type(exc).__name__,
                        traceback="".join(traceback.format_exception(exc)),
                    )
                ],
            )

        registered: list[ArtifactRef] = []
        findings = list(result.findings)
        for draft in result.artifacts:
            try:
                metadata = dict(draft.metadata)
                harness_metadata = metadata.get("_harness", {})
                if not isinstance(harness_metadata, dict):
                    harness_metadata = {}
                metadata["_harness"] = {
                    **harness_metadata,
                    "stage": stage_name,
                    "attempt": attempt,
                    "declared_status": result.status,
                }
                registered.append(
                    store.register_file(
                        draft.path,
                        kind=draft.kind,
                        media_type=draft.media_type,
                        producer_stage=stage_name,
                        metadata=metadata,
                    )
                )
            except Exception as exc:
                findings.append(
                    Finding(
                        "ARTIFACT_REGISTRATION_FAILED",
                        "error" if draft.kind in {"formalization", "gaia.ir"} else "warning",
                        f"could not register artifact from {draft.path.name}: {exc}",
                        details={"path": str(draft.path), "kind": draft.kind},
                    )
                )

        for finding in findings:
            _record_finding(run, stage_name, attempt, finding)
            store.append_event(
                "finding_recorded",
                {"stage": stage_name, "attempt": attempt, **finding.to_dict()},
            )

        outcome = result.status
        if any(finding.severity == "error" for finding in findings):
            outcome = "failed"

        if outcome == "waiting":
            run.status = "waiting"
            run.waiting = {
                "stage": stage_name,
                "attempt": attempt,
                "payload": dict(result.metadata),
            }
            store.save_run(run)
            store.append_event(
                "stage_waiting",
                {
                    "stage": stage_name,
                    "attempt": attempt,
                    "payload": result.metadata,
                    "audit_artifacts": [artifact.artifact_id for artifact in registered],
                },
            )
            return run

        if outcome == "failed":
            run.status = "failed"
            run.waiting = None
            store.save_run(run)
            store.append_event(
                "stage_failed",
                {
                    "stage": stage_name,
                    "attempt": attempt,
                    "payload": result.metadata,
                    "diagnostic_artifacts": [artifact.artifact_id for artifact in registered],
                },
            )
            return run

        checkpoint = store.create_checkpoint(
            stage_name, [artifact.artifact_id for artifact in registered]
        )
        run.last_checkpoint_id = checkpoint.checkpoint_id
        run.next_stage_index += 1
        completed_this_call += 1
        run.waiting = None
        store.append_event(
            "stage_succeeded",
            {
                "stage": stage_name,
                "attempt": attempt,
                "checkpoint": checkpoint.checkpoint_id,
                "metadata": result.metadata,
            },
        )
        if run.next_stage_index < len(stages):
            run.current_stage = stages[run.next_stage_index]["name"]
            run.status = "running"
        else:
            run.current_stage = None
            run.status = "succeeded"
        store.save_run(run)

    return run
