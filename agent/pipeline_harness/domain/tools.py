from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from ..models import JSONDict


TOOL_CALL_SCHEMA_VERSION = "1.0.0"
TOOL_STATUSES = {"succeeded", "failed"}


@dataclass(frozen=True)
class ToolCallRequest:
    call_id: str
    tool_name: str
    tool_version: str
    operation: str
    inputs: list[JSONDict]
    parameters: JSONDict = field(default_factory=dict)

    def to_dict(self) -> JSONDict:
        return {
            "schema_version": TOOL_CALL_SCHEMA_VERSION,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "operation": self.operation,
            "inputs": self.inputs,
            "parameters": self.parameters,
        }


@dataclass(frozen=True)
class ToolCallResponse:
    call_id: str
    status: str
    raw: Any
    normalized: JSONDict | None = None
    error: JSONDict | None = None
    metadata: JSONDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in TOOL_STATUSES:
            raise ValueError(f"invalid tool status: {self.status}")
        if self.status == "succeeded" and self.normalized is None:
            raise ValueError("successful tool response requires normalized output")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed tool response requires an error object")

    def to_dict(self) -> JSONDict:
        return {
            "schema_version": TOOL_CALL_SCHEMA_VERSION,
            "call_id": self.call_id,
            "status": self.status,
            "raw": self.raw,
            "normalized": self.normalized,
            "error": self.error,
            "metadata": self.metadata,
        }


@runtime_checkable
class DomainTool(Protocol):
    name: str
    version: str

    def invoke(self, request: ToolCallRequest) -> ToolCallResponse: ...


@runtime_checkable
class ArtifactAwareDomainTool(DomainTool, Protocol):
    """Optional in-process capability for tools that need frozen source bytes.

    Paths are injected only for the duration of a stage invocation.  They are
    deliberately not copied to ``ToolCallRequest`` or its audit artifact.
    """

    def bind_artifacts(self, artifacts: Mapping[str, tuple[Path, str]]) -> None: ...


def validate_tool_response(request: ToolCallRequest, response: ToolCallResponse) -> None:
    if response.call_id != request.call_id:
        raise ValueError("tool response call_id does not match request")
    response.__post_init__()
