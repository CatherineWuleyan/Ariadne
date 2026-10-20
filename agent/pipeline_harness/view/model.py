from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..models import JSONDict


GRANULARITIES = {"overview": 0, "standard": 1}


def _object_list(data: Mapping[str, Any], key: str) -> list[JSONDict]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{key} must be a list of objects")
    return [dict(item) for item in value]


def _required_text(item: Mapping[str, Any], key: str, collection: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{collection}.{key} must be a non-empty string")
    return value


@dataclass(frozen=True)
class ViewDocument:
    title: str
    source_artifacts: list[str]
    nodes: list[JSONDict]
    edges: list[JSONDict]
    search_documents: list[JSONDict]
    layers: list[JSONDict]
    metadata: JSONDict = field(default_factory=dict)
    stages: list[JSONDict] = field(default_factory=list)
    source_targets: list[JSONDict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.title, str) or not self.title:
            raise ValueError("view title must be a non-empty string")
        if not isinstance(self.source_artifacts, list) or not all(
            isinstance(item, str) and item for item in self.source_artifacts
        ):
            raise ValueError("source_artifacts must be a list of non-empty strings")
        if not isinstance(self.metadata, dict):
            raise ValueError("view metadata must be an object")
        if not all(isinstance(item, dict) for item in self.stages):
            raise ValueError("view stages must be objects")
        if not all(isinstance(item, dict) for item in self.source_targets):
            raise ValueError("view source_targets must be objects")

        node_ids: set[str] = set()
        for node in self.nodes:
            node_id = _required_text(node, "id", "nodes")
            _required_text(node, "label", "nodes")
            if node_id in node_ids:
                raise ValueError(f"duplicate view node ID: {node_id}")
            node_ids.add(node_id)
            granularity = node.get("min_granularity", "overview")
            if granularity not in GRANULARITIES:
                raise ValueError(f"invalid node granularity: {granularity}")
            visible_at = node.get("visible_at")
            if visible_at is not None and (
                not isinstance(visible_at, list)
                or not visible_at
                or any(item not in GRANULARITIES for item in visible_at)
            ):
                raise ValueError(f"invalid node visible_at: {visible_at}")

        edge_ids: set[str] = set()
        for edge in self.edges:
            edge_id = _required_text(edge, "id", "edges")
            source = _required_text(edge, "source", "edges")
            target = _required_text(edge, "target", "edges")
            if edge_id in edge_ids:
                raise ValueError(f"duplicate view edge ID: {edge_id}")
            edge_ids.add(edge_id)
            if source not in node_ids or target not in node_ids:
                raise ValueError(f"edge {edge_id} has a missing endpoint")
            granularity = edge.get("min_granularity", "overview")
            if granularity not in GRANULARITIES:
                raise ValueError(f"invalid edge granularity: {granularity}")
            visible_at = edge.get("visible_at")
            if visible_at is not None and (
                not isinstance(visible_at, list)
                or not visible_at
                or any(item not in GRANULARITIES for item in visible_at)
            ):
                raise ValueError(f"invalid edge visible_at: {visible_at}")

        layer_ids: set[str] = set()
        for layer in self.layers:
            layer_id = _required_text(layer, "id", "layers")
            if layer_id in layer_ids:
                raise ValueError(f"duplicate view layer ID: {layer_id}")
            layer_ids.add(layer_id)

        for document in self.search_documents:
            _required_text(document, "id", "search_documents")
            title = document.get("title", "")
            text = document.get("text", "")
            if not isinstance(title, str) or not isinstance(text, str):
                raise ValueError("search document title and text must be strings")
            tags = document.get("tags", [])
            refs = document.get("refs", [])
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                raise ValueError("search document tags must be strings")
            if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
                raise ValueError("search document refs must be strings")
            missing_refs = [ref for ref in refs if ref not in node_ids]
            if missing_refs:
                raise ValueError(
                    f"search document {document['id']} has missing refs: {', '.join(missing_refs)}"
                )

    def to_dict(self) -> JSONDict:
        return {
            "title": self.title,
            "source_artifacts": list(self.source_artifacts),
            "nodes": self.nodes,
            "edges": self.edges,
            "search_documents": self.search_documents,
            "layers": self.layers,
            "metadata": self.metadata,
            "stages": self.stages,
            "source_targets": self.source_targets,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ViewDocument":
        title = data.get("title")
        if not isinstance(title, str):
            raise ValueError("view title must be a string")
        source_artifacts = data.get("source_artifacts", [])
        if not isinstance(source_artifacts, list):
            raise ValueError("source_artifacts must be a list")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("view metadata must be an object")
        return cls(
            title=title,
            source_artifacts=list(source_artifacts),
            nodes=_object_list(data, "nodes"),
            edges=_object_list(data, "edges"),
            search_documents=_object_list(data, "search_documents"),
            layers=_object_list(data, "layers"),
            metadata=dict(metadata),
            stages=_object_list(data, "stages"),
            source_targets=_object_list(data, "source_targets"),
        )


def normalize_search_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def search_view(view: ViewDocument, query: str) -> list[JSONDict]:
    normalized = normalize_search_text(query)
    if not normalized:
        return []
    results: list[tuple[int, JSONDict]] = []
    for document in view.search_documents:
        document_id = str(document["id"])
        title = str(document.get("title", ""))
        text = str(document.get("text", ""))
        tags = [str(tag) for tag in document.get("tags", [])]
        haystack = normalize_search_text(" ".join([document_id, title, text, *tags]))
        normalized_id = normalize_search_text(document_id)
        if normalized == normalized_id:
            rank = 0
        elif normalized_id.startswith(normalized):
            rank = 1
        elif normalized in haystack:
            rank = 2
        else:
            continue
        results.append(
            (
                rank,
                {
                    "id": document_id,
                    "title": title,
                    "text": text,
                    "tags": tags,
                    "refs": list(document.get("refs", [])),
                    "metadata": dict(document.get("metadata", {})),
                },
            )
        )
    results.sort(key=lambda item: (item[0], normalize_search_text(item[1]["title"]), item[1]["id"]))
    return [item for _, item in results]
