"""Connectivity repair and auditing shared by every graph projection.

A projected graph must not contain isolated nodes.  Two classes of node are
structurally allowed to be isolated in the underlying Knowledge registry but
must never survive into a rendered graph:

* helper nodes (``layer == "helpers"``) — layout plumbing such as the terminal
  ``__equivalence_result_<digest>`` that Gaia's formalizer mints for an
  abduction clause.  Nothing consumes it, so it has no incoming and no
  outgoing edge.
* operators and strategies that lost every endpoint — a ``^``/contradiction
  operator whose operand or conclusion was filtered out is a "断头" node.

All zero-degree nodes are hidden from the rendered projection. Nodes that belong
to a disconnected component but have at least one live edge are retained.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..models import JSONDict
from .model import ViewDocument

_HELPER_LAYER = "helpers"
_REASONING_LAYERS = frozenset({"operators", "strategies", "weakpoints"})


def _is_helper(node: Mapping[str, Any]) -> bool:
    return str(node.get("layer") or "") == _HELPER_LAYER


def _is_removable(node: Mapping[str, Any]) -> bool:
    """All zero-degree nodes are prunable from the rendered projection."""

    return True


def prune_orphan_nodes(document: ViewDocument) -> tuple[ViewDocument, JSONDict]:
    """Return a connectivity-clean copy of ``document`` plus a repair report.

    The pass is transitive: removing an orphan deletes its incident edges,
    which can in turn orphan another node. It repeats until a fixed point.
    """

    nodes: list[JSONDict] = [dict(node) for node in document.nodes]
    edges: list[JSONDict] = [dict(edge) for edge in document.edges]
    removed_nodes: list[str] = []
    removed_helper_nodes: list[str] = []
    removed_edges: list[str] = []

    while True:
        node_ids = {str(node["id"]) for node in nodes}
        degree: dict[str, int] = {node_id: 0 for node_id in node_ids}
        live_edges: list[JSONDict] = []
        for edge in edges:
            source, target = str(edge["source"]), str(edge["target"])
            if source not in node_ids or target not in node_ids:
                removed_edges.append(str(edge["id"]))
                continue
            degree[source] += 1
            degree[target] += 1
            live_edges.append(edge)
        edges = live_edges

        dropped = sorted(
            str(node["id"])
            for node in nodes
            if degree.get(str(node["id"]), 0) == 0 and _is_removable(node)
        )
        if not dropped:
            break
        dropped_set = set(dropped)
        removed_nodes.extend(dropped)
        removed_helper_nodes.extend(
            node_id for node_id in dropped
            if _is_helper(next(node for node in nodes if str(node["id"]) == node_id))
        )
        nodes = [node for node in nodes if str(node["id"]) not in dropped_set]

    node_ids = {str(node["id"]) for node in nodes}
    degree = {node_id: 0 for node_id in node_ids}
    for edge in edges:
        degree[str(edge["source"])] += 1
        degree[str(edge["target"])] += 1

    standalone: list[str] = []

    search_documents = [
        {**entry, "refs": [ref for ref in entry.get("refs", []) if ref in node_ids]}
        for entry in document.search_documents
    ]
    search_documents = [entry for entry in search_documents if entry["refs"]]

    report: JSONDict = {
        "removed_helper_nodes": len(removed_helper_nodes),
        "removed_helper_node_ids": removed_helper_nodes,
        "removed_edges": len(removed_edges),
        "standalone_claims": len(standalone),
        "standalone_claim_ids": standalone,
    }
    repaired = ViewDocument(
        title=document.title,
        source_artifacts=list(document.source_artifacts),
        nodes=nodes,
        edges=edges,
        search_documents=search_documents,
        layers=list(document.layers),
        metadata={**document.metadata, "connectivity": report},
        stages=list(document.stages),
        source_targets=list(document.source_targets),
    )
    return repaired, report


def _components(nodes: list[JSONDict], edges: list[JSONDict]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = {str(node["id"]): set() for node in nodes}
    for edge in edges:
        source, target = str(edge["source"]), str(edge["target"])
        if source in adjacency and target in adjacency and source != target:
            adjacency[source].add(target)
            adjacency[target].add(source)
    seen: set[str] = set()
    components: list[list[str]] = []
    for node in nodes:
        node_id = str(node["id"])
        if node_id in seen:
            continue
        stack = [node_id]
        seen.add(node_id)
        members = [node_id]
        while stack:
            current = stack.pop()
            for neighbour in adjacency[current]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    members.append(neighbour)
                    stack.append(neighbour)
        components.append(members)
    components.sort(key=len, reverse=True)
    return components


def _step_matches(item: Mapping[str, Any], step: int | None) -> bool:
    """An absent step is unknown, not a mismatch: never silently drop the edge."""

    if step is None:
        return True
    value = item.get("step")
    if value is None:
        return True
    try:
        return int(value) == int(step)
    except (TypeError, ValueError):
        return True


def audit_view_connectivity(
    document: ViewDocument, *, step: int | None = None, layers: frozenset[str] = frozenset({"operators"}),
) -> JSONDict:
    """Measure how fragmented one step of a projected graph is.

    ``layers`` selects the edge layers that constitute "the reasoning graph"
    for the measurement; edges in other layers are ignored, which mirrors what
    the viewer actually draws in standard mode.
    """

    nodes = [node for node in document.nodes if _step_matches(node, step)]
    node_ids = {str(node["id"]) for node in nodes}
    edges = [
        edge
        for edge in document.edges
        if _step_matches(edge, step)
        and str(edge.get("layer") or "") in layers
        and str(edge["source"]) in node_ids
        and str(edge["target"]) in node_ids
    ]

    degree: dict[str, int] = {node_id: 0 for node_id in node_ids}
    for edge in edges:
        degree[str(edge["source"])] += 1
        degree[str(edge["target"])] += 1

    isolated = [
        node for node in nodes if degree.get(str(node["id"]), 0) == 0
    ]
    helper_ids = [str(node["id"]) for node in isolated if _is_helper(node)]
    operator_ids = [
        str(node["id"])
        for node in isolated
        if str(node.get("layer") or "") in _REASONING_LAYERS
    ]
    claim_ids = [
        str(node["id"])
        for node in isolated
        if str(node.get("layer") or "") not in _REASONING_LAYERS and not _is_helper(node)
    ]

    live_nodes = [node for node in nodes if degree.get(str(node["id"]), 0) > 0]
    live_ids = {str(node["id"]) for node in live_nodes}
    live_edges = [edge for edge in edges if str(edge["source"]) in live_ids]
    components = _components(live_nodes, live_edges)
    largest = len(components[0]) if components else 0
    return {
        "step": step,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "isolated_node_count": len(isolated),
        "isolated_helper_count": len(helper_ids),
        "isolated_helper_ids": helper_ids,
        "dangling_operator_count": len(operator_ids),
        "dangling_operator_ids": operator_ids,
        "standalone_claim_count": len(claim_ids),
        "standalone_claim_ids": claim_ids,
        "component_count": len(components),
        "largest_component_size": largest,
        "largest_component_ratio": round(largest / len(live_nodes), 4) if live_nodes else 0.0,
        "component_sizes": [len(component) for component in components[:20]],
    }


__all__ = ["prune_orphan_nodes", "audit_view_connectivity"]
