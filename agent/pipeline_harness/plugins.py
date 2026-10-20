from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .models import ArtifactRef, Finding, JSONDict, STAGE_RESULT_STATUSES


@dataclass(frozen=True)
class ArtifactDraft:
    path: Path
    kind: str
    media_type: str = "application/octet-stream"
    metadata: JSONDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise ValueError("artifact draft path must be a pathlib.Path")
        if not self.kind or not self.media_type:
            raise ValueError("artifact draft kind and media_type are required")
        if not isinstance(self.metadata, dict):
            raise ValueError("artifact draft metadata must be an object")


@dataclass(frozen=True)
class StageContext:
    run_id: str
    run_dir: Path
    work_dir: Path
    stage_name: str
    attempt: int
    inputs: list[ArtifactRef]
    options: JSONDict

    def artifact_path(self, artifact: ArtifactRef) -> Path:
        candidate = (self.run_dir / artifact.path).resolve()
        root = self.run_dir.resolve()
        if root not in candidate.parents:
            raise ValueError(f"artifact path escapes run directory: {artifact.path}")
        return candidate

    def find_all(self, kind: str) -> list[ArtifactRef]:
        if not isinstance(kind, str) or not kind:
            raise ValueError("artifact kind must be a non-empty string")
        return [artifact for artifact in self.inputs if artifact.kind == kind]

    def require_one(self, kind: str) -> ArtifactRef:
        matches = self.find_all(kind)
        if len(matches) != 1:
            raise ValueError(
                f"expected exactly one {kind} input artifact, found {len(matches)}"
            )
        return matches[0]

    def latest(self, kind: str) -> ArtifactRef | None:
        matches = self.find_all(kind)
        return matches[-1] if matches else None


@dataclass(frozen=True)
class StageResult:
    status: str
    artifacts: list[ArtifactDraft] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    metadata: JSONDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STAGE_RESULT_STATUSES:
            raise ValueError(f"invalid stage result status: {self.status}")
        if not all(isinstance(item, ArtifactDraft) for item in self.artifacts):
            raise ValueError("stage result artifacts must contain ArtifactDraft values")
        if not all(isinstance(item, Finding) for item in self.findings):
            raise ValueError("stage result findings must contain Finding values")
        if not isinstance(self.metadata, dict):
            raise ValueError("stage result metadata must be an object")


@runtime_checkable
class StagePlugin(Protocol):
    def run(self, context: StageContext) -> StageResult: ...


@runtime_checkable
class ViewAdapter(Protocol):
    version: str

    def project(
        self,
        store: Any,
        artifacts: list[ArtifactRef],
        options: Mapping[str, Any] | None = None,
    ) -> Any: ...


def load_symbol(spec: str) -> Any:
    if not isinstance(spec, str) or ":" not in spec:
        raise ValueError("plugin spec must use module:object syntax")
    module_name, object_name = spec.split(":", 1)
    if not module_name or not object_name:
        raise ValueError("plugin spec must use module:object syntax")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, object_name)
    except AttributeError as exc:
        raise ValueError(f"plugin object not found: {spec}") from exc


def instantiate(spec: str) -> Any:
    symbol = load_symbol(spec)
    return symbol() if isinstance(symbol, type) else symbol
