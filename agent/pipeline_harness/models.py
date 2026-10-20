from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


JSONDict = dict[str, Any]
RUN_STATUSES = {"created", "running", "waiting", "succeeded", "failed"}
FINDING_SEVERITIES = {"error", "warning", "info"}
STAGE_RESULT_STATUSES = {"succeeded", "waiting", "failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _dict(value: Any, key: str) -> JSONDict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return dict(value)


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    path: str
    sha256: str
    media_type: str
    producer_stage: str
    metadata: JSONDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "artifact_id",
            "kind",
            "path",
            "sha256",
            "media_type",
            "producer_stage",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256):
            raise ValueError("sha256 must be a lowercase 64-character hex digest")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")

    def to_dict(self) -> JSONDict:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "producer_stage": self.producer_stage,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            artifact_id=_required_string(data, "artifact_id"),
            kind=_required_string(data, "kind"),
            path=_required_string(data, "path"),
            sha256=_required_string(data, "sha256"),
            media_type=_required_string(data, "media_type"),
            producer_stage=_required_string(data, "producer_stage"),
            metadata=_dict(data.get("metadata"), "metadata"),
        )


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    stage: str
    artifacts: list[str]
    created_at: str

    def __post_init__(self) -> None:
        if not self.checkpoint_id or not self.stage or not self.created_at:
            raise ValueError("checkpoint_id, stage and created_at are required")
        if not isinstance(self.artifacts, list) or not all(
            isinstance(item, str) and item for item in self.artifacts
        ):
            raise ValueError("artifacts must be a list of non-empty strings")

    def to_dict(self) -> JSONDict:
        return {
            "checkpoint_id": self.checkpoint_id,
            "stage": self.stage,
            "artifacts": list(self.artifacts),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Checkpoint":
        artifacts = data.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("artifacts must be a list")
        return cls(
            checkpoint_id=_required_string(data, "checkpoint_id"),
            stage=_required_string(data, "stage"),
            artifacts=list(artifacts),
            created_at=_required_string(data, "created_at"),
        )


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    artifact_id: str | None = None
    location: JSONDict = field(default_factory=dict)
    details: JSONDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("finding code and message are required")
        if self.severity not in FINDING_SEVERITIES:
            raise ValueError(f"invalid finding severity: {self.severity}")
        if not isinstance(self.location, dict) or not isinstance(self.details, dict):
            raise ValueError("finding location and details must be objects")

    def to_dict(self) -> JSONDict:
        result: JSONDict = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "location": self.location,
            "details": self.details,
        }
        if self.artifact_id is not None:
            result["artifact_id"] = self.artifact_id
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Finding":
        artifact_id = data.get("artifact_id")
        if artifact_id is not None and not isinstance(artifact_id, str):
            raise ValueError("artifact_id must be a string or null")
        return cls(
            code=_required_string(data, "code"),
            severity=_required_string(data, "severity"),
            message=_required_string(data, "message"),
            artifact_id=artifact_id,
            location=_dict(data.get("location"), "location"),
            details=_dict(data.get("details"), "details"),
        )


@dataclass
class RunRecord:
    run_id: str
    pipeline_id: str
    pipeline_version: str
    status: str
    current_stage: str | None
    config: JSONDict
    config_sha256: str
    created_at: str
    updated_at: str
    input_artifacts: list[str] = field(default_factory=list)
    parent_run_id: str | None = None
    parent_checkpoint_id: str | None = None
    next_stage_index: int = 0
    last_checkpoint_id: str | None = None
    attempts: dict[str, int] = field(default_factory=dict)
    findings: list[JSONDict] = field(default_factory=list)
    waiting: JSONDict | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "pipeline_id", "pipeline_version", "created_at", "updated_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        if self.status not in RUN_STATUSES:
            raise ValueError(f"invalid run status: {self.status}")
        if self.current_stage is not None and not isinstance(self.current_stage, str):
            raise ValueError("current_stage must be a string or null")
        if not isinstance(self.config, dict):
            raise ValueError("config must be an object")
        if len(self.config_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.config_sha256
        ):
            raise ValueError("config_sha256 must be a lowercase 64-character hex digest")
        if not isinstance(self.input_artifacts, list) or not all(
            isinstance(item, str) and item for item in self.input_artifacts
        ):
            raise ValueError("input_artifacts must be a list of non-empty strings")
        if len(set(self.input_artifacts)) != len(self.input_artifacts):
            raise ValueError("input_artifacts must not contain duplicates")
        for name in ("parent_run_id", "parent_checkpoint_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a non-empty string or null")
        if (self.parent_run_id is None) != (self.parent_checkpoint_id is None):
            raise ValueError("parent_run_id and parent_checkpoint_id must be set together")
        if not isinstance(self.next_stage_index, int) or self.next_stage_index < 0:
            raise ValueError("next_stage_index must be a non-negative integer")
        if not isinstance(self.attempts, dict) or not all(
            isinstance(key, str) and isinstance(value, int) and value >= 0
            for key, value in self.attempts.items()
        ):
            raise ValueError("attempts must map stage names to non-negative integers")
        if not isinstance(self.findings, list):
            raise ValueError("findings must be a list")
        if self.waiting is not None and not isinstance(self.waiting, dict):
            raise ValueError("waiting must be an object or null")

    def to_dict(self) -> JSONDict:
        return {
            "run_id": self.run_id,
            "pipeline_id": self.pipeline_id,
            "pipeline_version": self.pipeline_version,
            "status": self.status,
            "current_stage": self.current_stage,
            "config": self.config,
            "config_sha256": self.config_sha256,
            "input_artifacts": list(self.input_artifacts),
            "parent_run_id": self.parent_run_id,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_stage_index": self.next_stage_index,
            "last_checkpoint_id": self.last_checkpoint_id,
            "attempts": self.attempts,
            "findings": self.findings,
            "waiting": self.waiting,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunRecord":
        current_stage = data.get("current_stage")
        if current_stage is not None and not isinstance(current_stage, str):
            raise ValueError("current_stage must be a string or null")
        last_checkpoint_id = data.get("last_checkpoint_id")
        if last_checkpoint_id is not None and not isinstance(last_checkpoint_id, str):
            raise ValueError("last_checkpoint_id must be a string or null")
        next_stage_index = data.get("next_stage_index", 0)
        if not isinstance(next_stage_index, int):
            raise ValueError("next_stage_index must be an integer")
        findings = data.get("findings", [])
        if not isinstance(findings, list):
            raise ValueError("findings must be a list")
        input_artifacts = data.get("input_artifacts", [])
        if not isinstance(input_artifacts, list):
            raise ValueError("input_artifacts must be a list")
        parent_run_id = data.get("parent_run_id")
        if parent_run_id is not None and not isinstance(parent_run_id, str):
            raise ValueError("parent_run_id must be a string or null")
        parent_checkpoint_id = data.get("parent_checkpoint_id")
        if parent_checkpoint_id is not None and not isinstance(parent_checkpoint_id, str):
            raise ValueError("parent_checkpoint_id must be a string or null")
        waiting = data.get("waiting")
        if waiting is not None and not isinstance(waiting, dict):
            raise ValueError("waiting must be an object or null")
        return cls(
            run_id=_required_string(data, "run_id"),
            pipeline_id=_required_string(data, "pipeline_id"),
            pipeline_version=_required_string(data, "pipeline_version"),
            status=_required_string(data, "status"),
            current_stage=current_stage,
            config=_dict(data.get("config"), "config"),
            config_sha256=_required_string(data, "config_sha256"),
            created_at=_required_string(data, "created_at"),
            updated_at=_required_string(data, "updated_at"),
            input_artifacts=list(input_artifacts),
            parent_run_id=parent_run_id,
            parent_checkpoint_id=parent_checkpoint_id,
            next_stage_index=next_stage_index,
            last_checkpoint_id=last_checkpoint_id,
            attempts=_dict(data.get("attempts"), "attempts"),
            findings=list(findings),
            waiting=dict(waiting) if waiting is not None else None,
        )
