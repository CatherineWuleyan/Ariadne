from __future__ import annotations

import json
from typing import Any, Mapping

from .models import ArtifactRef, Finding
from .plugins import ArtifactDraft, StageContext, StageResult
from .store import RunStore, atomic_write_json


SYNTHETIC_PIPELINE: dict[str, Any] = {
    "pipeline_id": "synthetic",
    "version": "1",
    "stages": [
        {
            "name": "make-values",
            "plugin": "pipeline_harness.synthetic:MakeValuesPlugin",
            "options": {"count": 3},
        },
        {
            "name": "make-graph",
            "plugin": "pipeline_harness.synthetic:MakeGraphPlugin",
            "options": {},
        },
    ],
    "view_adapter": "pipeline_harness.synthetic:SyntheticViewAdapter",
}

SYNTHETIC_FAILURE_PIPELINE: dict[str, Any] = {
    **SYNTHETIC_PIPELINE,
    "pipeline_id": "synthetic-failure",
    "stages": [
        SYNTHETIC_PIPELINE["stages"][0],
        {
            "name": "make-graph",
            "plugin": "pipeline_harness.synthetic:MakeGraphPlugin",
            "options": {"fail_once": True},
        },
    ],
}


class MakeValuesPlugin:
    def run(self, context: StageContext) -> StageResult:
        required_kind = context.options.get("require_input_kind")
        if required_kind is not None:
            try:
                required_input = context.require_one(str(required_kind))
                context.artifact_path(required_input).read_bytes()
            except ValueError as exc:
                return StageResult(
                    status="failed",
                    findings=[
                        Finding(
                            code="SYNTHETIC_EXTERNAL_INPUT_INVALID",
                            severity="error",
                            message=str(exc),
                        )
                    ],
                )
        count = context.options.get("count", 3)
        if not isinstance(count, int) or count < 1:
            return StageResult(
                status="failed",
                findings=[
                    Finding(
                        code="SYNTHETIC_INVALID_COUNT",
                        severity="error",
                        message="synthetic count must be a positive integer",
                    )
                ],
            )
        output = context.work_dir / "values.json"
        atomic_write_json(
            output,
            {
                "fixture_kind": "synthetic_contract_test",
                "values": [f"VALUE_{index + 1}" for index in range(count)],
            },
        )
        return StageResult(
            status="succeeded",
            artifacts=[
                ArtifactDraft(
                    path=output,
                    kind="synthetic.values",
                    media_type="application/json",
                )
            ],
        )


class MakeGraphPlugin:
    def run(self, context: StageContext) -> StageResult:
        required_kind = context.options.get("require_input_kind")
        if required_kind is not None:
            try:
                required_input = context.require_one(str(required_kind))
                context.artifact_path(required_input).read_bytes()
            except ValueError as exc:
                return StageResult(
                    status="failed",
                    findings=[
                        Finding(
                            code="SYNTHETIC_EXTERNAL_INPUT_INVALID",
                            severity="error",
                            message=str(exc),
                        )
                    ],
                )
        if context.options.get("wait_once") is True and context.attempt == 1:
            waiting_output = context.work_dir / "waiting.json"
            atomic_write_json(
                waiting_output,
                {"fixture_kind": "synthetic_waiting_audit", "attempt": context.attempt},
            )
            return StageResult(
                status="waiting",
                artifacts=[
                    ArtifactDraft(
                        path=waiting_output,
                        kind="synthetic.waiting",
                        media_type="application/json",
                    )
                ],
                metadata={"reason": "synthetic_wait_once"},
            )
        if context.options.get("fail_once") is True and context.attempt == 1:
            diagnostic_output = context.work_dir / "failure.json"
            atomic_write_json(
                diagnostic_output,
                {"fixture_kind": "synthetic_failure_diagnostic", "attempt": context.attempt},
            )
            return StageResult(
                status="failed",
                artifacts=[
                    ArtifactDraft(
                        path=diagnostic_output,
                        kind="synthetic.failure",
                        media_type="application/json",
                    )
                ],
                findings=[
                    Finding(
                        code="SYNTHETIC_FAIL_ONCE",
                        severity="error",
                        message="intentional first-attempt failure for resume testing",
                    )
                ],
            )
        values_ref = context.latest("synthetic.values")
        if values_ref is None:
            return StageResult(
                status="failed",
                findings=[
                    Finding(
                        code="SYNTHETIC_INPUT_MISSING",
                        severity="error",
                        message="make-graph requires a synthetic.values artifact",
                    )
                ],
            )
        with context.artifact_path(values_ref).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        values = payload.get("values", [])
        if not isinstance(values, list):
            raise ValueError("synthetic values payload is invalid")

        nodes = []
        search_documents = []
        for index, value in enumerate(values):
            node_id = f"SYNTH_NODE_{index + 1:03d}"
            granularity = "overview" if index == 0 else "standard"
            nodes.append(
                {
                    "id": node_id,
                    "label": str(value),
                    "kind": "synthetic",
                    "layer": "values",
                    "min_granularity": granularity,
                    "summary": f"Synthetic value {index + 1}",
                    "details": {"ordinal": index + 1, "fixture": True},
                }
            )
            search_documents.append(
                {
                    "id": node_id,
                    "title": str(value),
                    "text": f"Synthetic searchable item {value} 合成条目 {index + 1}",
                    "tags": ["synthetic", granularity],
                    "refs": [node_id],
                    "metadata": {},
                }
            )
        edges = [
            {
                "id": f"SYNTH_EDGE_{index:03d}",
                "source": nodes[index - 1]["id"],
                "target": nodes[index]["id"],
                "label": "next",
                "layer": "links",
                "min_granularity": "standard",
                "details": {},
            }
            for index in range(1, len(nodes))
        ]
        output = context.work_dir / "graph.json"
        atomic_write_json(
            output,
            {
                "fixture_kind": "synthetic_contract_test",
                "nodes": nodes,
                "edges": edges,
                "search_documents": search_documents,
            },
        )
        return StageResult(
            status="succeeded",
            artifacts=[
                ArtifactDraft(
                    path=output,
                    kind="synthetic.graph",
                    media_type="application/json",
                )
            ],
        )


class SyntheticViewAdapter:
    version = "1"

    def project(
        self,
        store: RunStore,
        artifacts: list[ArtifactRef],
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        from .view.model import ViewDocument

        graph_ref = next(
            (artifact for artifact in reversed(artifacts) if artifact.kind == "synthetic.graph"),
            None,
        )
        if graph_ref is None:
            return ViewDocument(
                title="Synthetic pipeline",
                source_artifacts=[],
                nodes=[],
                edges=[],
                search_documents=[],
                layers=[],
                metadata={"empty_reason": "synthetic.graph has not been produced"},
            )
        with store.artifact_path(graph_ref).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return ViewDocument.from_dict(
            {
                "title": "Synthetic pipeline",
                "source_artifacts": [graph_ref.artifact_id],
                "nodes": payload.get("nodes", []),
                "edges": payload.get("edges", []),
                "search_documents": payload.get("search_documents", []),
                "layers": [
                    {"id": "values", "label": "Values", "default_visible": True},
                    {"id": "links", "label": "Links", "default_visible": True},
                ],
                "metadata": {"fixture_kind": "synthetic_contract_test"},
            }
        )


BUILTIN_PIPELINES = {
    "synthetic": SYNTHETIC_PIPELINE,
    "synthetic-failure": SYNTHETIC_FAILURE_PIPELINE,
}

# Kept here for the CLI's existing built-in registry boundary.
from .domain.workflow import AUTOMATED_FORMALIZATION_PIPELINE

BUILTIN_PIPELINES["automated-paper-formalization"] = AUTOMATED_FORMALIZATION_PIPELINE
