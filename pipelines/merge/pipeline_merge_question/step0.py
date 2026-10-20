"""Step 0: mechanically validate the human-selected merge mode and scope."""
from __future__ import annotations

from typing import Any, Mapping

from pipeline_harness.models import Finding, JSONDict
from pipeline_harness.plugins import StageContext, StageResult


MERGE_MODES = frozenset({"bootstrap", "incremental", "reconcile"})
_OPTION_FIELDS = frozenset({"mode", "scope"})
_SCOPE_FIELDS = frozenset({"domain", "selection"})


def _non_empty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{name} must not contain leading or trailing whitespace")
    return value


def validate_step0_options(options: Mapping[str, Any]) -> JSONDict:
    """Return a normalized copy after validating the frozen manual selection."""
    if set(options) != _OPTION_FIELDS:
        raise ValueError("Step 0 options must contain exactly mode and scope")

    mode = _non_empty_string(options.get("mode"), "mode")
    if mode not in MERGE_MODES:
        raise ValueError(
            "mode must be one of bootstrap, incremental, or reconcile"
        )

    raw_scope = options.get("scope")
    if not isinstance(raw_scope, Mapping) or set(raw_scope) != _SCOPE_FIELDS:
        raise ValueError("scope must contain exactly domain and selection")
    domain = _non_empty_string(raw_scope.get("domain"), "scope.domain")

    raw_selection = raw_scope.get("selection")
    if not isinstance(raw_selection, list):
        raise ValueError("scope.selection must be a list")
    selection = [
        _non_empty_string(value, f"scope.selection[{index}]")
        for index, value in enumerate(raw_selection)
    ]
    if len(selection) != len(set(selection)):
        raise ValueError("scope.selection must not contain duplicate identifiers")

    minimum = 2 if mode == "bootstrap" else 1
    if len(selection) < minimum:
        if mode == "bootstrap":
            raise ValueError("bootstrap requires at least two selected Paper Packages")
        if mode == "incremental":
            raise ValueError("incremental requires at least one selected new Paper Package")
        raise ValueError("reconcile requires at least one selected package, Knowledge, or local subgraph")

    return {
        "mode": mode,
        "scope": {"domain": domain, "selection": selection},
    }


class Step0SelectScopePlugin:
    """Validate config-only human input without producing a domain Artifact."""

    def run(self, context: StageContext) -> StageResult:
        try:
            validate_step0_options(context.options)
        except ValueError as exc:
            return StageResult(
                "failed",
                findings=[Finding("STEP0_SELECTION_INVALID", "error", str(exc))],
            )
        return StageResult("succeeded", metadata={"validated": True})

