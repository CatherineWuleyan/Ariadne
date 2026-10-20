"""Read-only importers for the first real-input integration stage.

Importers receive only frozen ArtifactRefs from the input manifest.  They never
walk the workspace and they never call LKM (that boundary is intentional).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .plugins import ArtifactDraft, StageContext, StageResult

IMPORTER_VERSION = "1"


@dataclass(frozen=True)
class InputSpec:
    kind: str
    required: bool
    media_types: tuple[str, ...]
    version: str
    fields: tuple[str, ...]


INPUT_SPECS = (
    InputSpec("source.paper_text", True, ("text/markdown",), "1", ("text",)),
    InputSpec("source.clean_claims", True, ("application/json",), "1", ("assertions", "relations")),
    InputSpec("source.lkm_coarse_graph", True, ("application/json",), "1", ("nodes", "edges")),
    InputSpec("source.original_figure", False, ("image/jpeg", "image/png", "application/pdf"), "1", ()),
)

INPUT_BUNDLE_KIND = "input.bundle"


class ImportErrorDetail(ValueError):
    pass


def _load(ref, context: StageContext) -> Any:
    path = context.artifact_path(ref)
    if ref.media_type == "application/json":
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return path.read_text(encoding="utf-8")


def _validate(kind: str, value: Any) -> None:
    if kind == "source.paper_text":
        if not isinstance(value, str) or not value.strip():
            raise ImportErrorDetail("paper text must be a non-empty UTF-8 string")
        return
    if not isinstance(value, dict):
        raise ImportErrorDetail(f"{kind} must contain a JSON object")
    required = next(spec.fields for spec in INPUT_SPECS if spec.kind == kind)
    if kind == "source.lkm_coarse_graph":
        data = value.get("data")
        papers = data.get("papers") if isinstance(data, dict) else None
        if not isinstance(papers, list) or not papers:
            raise ImportErrorDetail("source.lkm_coarse_graph must contain a non-empty data.papers list")
        invalid = [index for index, paper in enumerate(papers) if not isinstance(paper, dict) or not isinstance(paper.get("graph"), dict) or not all(isinstance(paper["graph"].get(key), list) for key in ("nodes", "edges"))]
        if invalid:
            raise ImportErrorDetail(f"source.lkm_coarse_graph papers must contain graph.nodes and graph.edges lists; invalid indexes: {invalid}")
        return
    missing = [field for field in required if field not in value]
    if missing:
        raise ImportErrorDetail(f"{kind} is missing fields: {', '.join(missing)}")
    if kind == "source.clean_claims" and not all(isinstance(value[k], list) for k in required):
        raise ImportErrorDetail("clean claims assertions and relations must be lists")


class RealInputImporter:
    """StagePlugin that imports only kinds explicitly supplied by the manifest."""

    def run(self, context: StageContext) -> StageResult:
        inputs: dict[str, list[Any]] = {}
        for ref in context.inputs:
            inputs.setdefault(ref.kind, []).append(ref)
        bundle: dict[str, Any] = {
            "schema_version": IMPORTER_VERSION,
            "sources": {},
            "figures": [],
        }
        imported: list[dict[str, Any]] = []
        for spec in INPUT_SPECS:
            refs = inputs.get(spec.kind, [])
            if not refs:
                if spec.required:
                    return StageResult("failed", metadata={"missing_kind": spec.kind})
                continue
            for index, ref in enumerate(refs, 1):
                if ref.media_type not in spec.media_types:
                    return StageResult("failed", metadata={"kind": spec.kind, "media_type": ref.media_type})
                if spec.kind == "source.original_figure":
                    bundle["figures"].append({
                        "artifact_id": ref.artifact_id,
                        "sha256": ref.sha256,
                        "media_type": ref.media_type,
                        "figure": ref.metadata.get("figure", ref.metadata.get("source_filename")),
                        "sequence": ref.metadata.get("sequence"),
                    })
                    imported.append({"kind": spec.kind, "artifact_id": ref.artifact_id, "version": spec.version})
                    continue
                try:
                    value = _load(ref, context)
                    _validate(spec.kind, value)
                except (OSError, json.JSONDecodeError, ImportErrorDetail) as exc:
                    return StageResult("failed", metadata={"kind": spec.kind, "error": str(exc)})
                bundle["sources"].setdefault(spec.kind, []).append({
                    "artifact_id": ref.artifact_id,
                    "sha256": ref.sha256,
                    "media_type": ref.media_type,
                    "version": spec.version,
                    "value": value,
                })
                imported.append({"kind": spec.kind, "artifact_id": ref.artifact_id, "version": spec.version})
        output = context.work_dir / "source_input_bundle.json"
        output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return StageResult(
            "succeeded",
            [ArtifactDraft(output, INPUT_BUNDLE_KIND, "application/json", {
                "importer_version": IMPORTER_VERSION,
                "logical_name": output.name,
                "source_kinds": sorted(bundle["sources"]),
                "figure_count": len(bundle["figures"]),
            })],
            metadata={"importer_version": IMPORTER_VERSION, "imported": imported},
        )


class LKMToolPlugin(Protocol):
    """Future boundary: raw tool response and normalized result must be persisted."""
    def run_lkm(self, inputs: Mapping[str, Any]) -> Mapping[str, Any]: ...


REAL_INPUT_PIPELINE = {
    "pipeline_id": "gaia-real-inputs",
    "version": "phase2-step1",
    "stages": [{"name": "import-real-inputs", "plugin": "pipeline_harness.real_inputs:RealInputImporter"}],
}
