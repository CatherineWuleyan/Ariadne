from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Mapping

from ..models import ArtifactRef
from ..plugins import ViewAdapter, instantiate
from ..store import RunStore, atomic_write_bytes, atomic_write_json, canonical_json_bytes, sha256_bytes
from .model import ViewDocument


class ArtifactListViewAdapter:
    version = "1"

    def project(
        self,
        store: RunStore,
        artifacts: list[ArtifactRef],
        options: Mapping[str, Any] | None = None,
    ) -> ViewDocument:
        nodes = [
            {
                "id": artifact.artifact_id,
                "label": artifact.kind,
                "kind": "artifact",
                "layer": "artifacts",
                "min_granularity": "overview",
                "summary": artifact.path,
                "details": artifact.to_dict(),
            }
            for artifact in artifacts
        ]
        documents = [
            {
                "id": artifact.artifact_id,
                "title": artifact.kind,
                "text": f"{artifact.path} {artifact.sha256}",
                "tags": [artifact.kind, artifact.producer_stage],
                "refs": [artifact.artifact_id],
                "metadata": {},
            }
            for artifact in artifacts
        ]
        return ViewDocument(
            title="Run artifacts",
            source_artifacts=[artifact.artifact_id for artifact in artifacts],
            nodes=nodes,
            edges=[],
            search_documents=documents,
            layers=[{"id": "artifacts", "label": "Artifacts", "default_visible": True}],
            metadata={},
        )


def _render_force_html(view: ViewDocument) -> bytes:
    """Render the spring-electrical (force-directed) domain-graph viewer.

    ``metadata.renderer == "force"`` selects this template.  It carries no
    third-party dependency: the layout is the Fruchterman-Reingold
    spring-electrical model -- the same forces ``d3-force`` exposes as
    ``forceManyBody`` (repulsion between unconnected nodes), ``forceLink``
    (attraction along edges), ``forceCenter`` and ``forceCollide``.  Node radius
    scales with degree.
    """
    template_path = Path(__file__).with_name("force_viewer.html")
    template = template_path.read_text(encoding="utf-8")
    payload = json.dumps(view.to_dict(), ensure_ascii=False, sort_keys=True).replace(
        "</script", "<\\/script"
    )
    return template.replace("__VIEW_DATA__", payload).encode("utf-8")


def _render_html(view: ViewDocument) -> bytes:
    if str((view.metadata or {}).get("renderer") or "") == "force":
        return _render_force_html(view)
    template_path = Path(__file__).with_name("viewer.html")
    template = template_path.read_text(encoding="utf-8")
    dagre_bytes = base64.b64decode(
        template_path.with_name("dagre-3.1.1.min.js.b64").read_text(encoding="ascii"),
        validate=False,
    )
    expected_dagre_sha256 = "3152d214941a5df3a3d4c079dfa338c3cd7a6c0d4c1b4c3a2fdb6bba6f6facf9"
    if sha256_bytes(dagre_bytes) != expected_dagre_sha256:
        raise ValueError("vendored @dagrejs/dagre 3.1.1 failed integrity validation")
    dagre_license = template_path.with_name("dagre-3.1.1.LICENSE.txt").read_text(encoding="utf-8")
    dagre_source = dagre_bytes.decode("utf-8").replace(
        "//# sourceMappingURL=dagre.min.js.map", ""
    )
    payload = json.dumps(view.to_dict(), ensure_ascii=False, sort_keys=True).replace(
        "</script", "<\\/script"
    )
    rendered = template.replace(
        "__DAGRE_SCRIPT__",
        f"/* @license\n{dagre_license}\n*/\n{dagre_source}",
    ).replace("__VIEW_DATA__", payload)
    return rendered.encode("utf-8")


def project_run(
    run_dir: Path | str,
    *,
    adapter_spec: str | None = None,
    options: Mapping[str, Any] | None = None,
    write_viewer: bool = True,
) -> ViewDocument:
    store = RunStore(run_dir)
    run = store.load_run()
    artifacts = store.load_artifacts()
    selected_spec = adapter_spec or run.config.get("view_adapter")
    if selected_spec is None:
        adapter: ViewAdapter = ArtifactListViewAdapter()
        adapter_name = "pipeline_harness.view.projector:ArtifactListViewAdapter"
    else:
        if not isinstance(selected_spec, str):
            raise ValueError("view_adapter must be a plugin spec string")
        candidate = instantiate(selected_spec)
        if not isinstance(candidate, ViewAdapter):
            raise TypeError(f"{selected_spec} does not implement ViewAdapter")
        adapter = candidate
        adapter_name = selected_spec

    view = adapter.project(store, artifacts, options or {})
    if not isinstance(view, ViewDocument):
        raise TypeError(f"{adapter_name} returned {type(view).__name__}, not ViewDocument")
    view.validate()
    artifact_map = store.artifact_map()
    missing = [item for item in view.source_artifacts if item not in artifact_map]
    if missing:
        raise ValueError(f"view has unknown source artifacts: {', '.join(missing)}")

    view_dir = store.run_dir / "views"
    view_payload = view.to_dict()
    view_bytes = canonical_json_bytes(view_payload)
    atomic_write_bytes(view_dir / "view_model.json", view_bytes)
    if write_viewer:
        atomic_write_bytes(view_dir / "viewer.html", _render_html(view))
    manifest = {
        "adapter": adapter_name,
        "adapter_version": str(getattr(adapter, "version", "unknown")),
        "view_model": "views/view_model.json",
        "viewer": "views/viewer.html" if write_viewer else None,
        "view_sha256": sha256_bytes(view_bytes),
        "source_artifacts": [
            {
                "artifact_id": artifact_id,
                "sha256": artifact_map[artifact_id].sha256,
            }
            for artifact_id in view.source_artifacts
        ],
    }
    atomic_write_json(view_dir / "manifest.json", manifest)
    store.append_event(
        "view_projected",
        {"adapter": adapter_name, "source_artifacts": view.source_artifacts},
    )
    return view
