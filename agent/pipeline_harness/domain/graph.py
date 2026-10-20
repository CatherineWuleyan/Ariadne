"""Connectivity audit for the V2 authoring formalization graph.

The official Gaia compiler already proves that the *referential* graph is
closed (every operator variable, operator conclusion, strategy premise and
strategy conclusion resolves to a Knowledge node).  It does not prove that the
*reasoning* graph is connected.

A single-paper reasoning graph is expected to be one connected structure rooted
in the paper's own conclusions.  When an article's claims are extracted without
linking them to any relation, or when a claim is only ever named by a Step-3
weakpoint (which authors no Step-4 reasoning edge), that claim ends up floating
in its own component.  This module measures that condition so the pipeline can
report it instead of silently emitting a fragmented IR.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..models import JSONDict

# A formalization graph this small is not expected to be one component yet.
MIN_CONNECTED_NODES = 2


def _knowledge_id(value: Any) -> str:
    return str(value)


def reasoning_graph(
    graph: Mapping[str, Any],
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Return the graph node IDs, reasoning links and an undirected adjacency map.

    Every operator or strategy relates its inputs to its conclusion.  An
    authoring operator often has no explicit ``conclusion`` (its variables are
    related directly), and the compiler mints a helper conclusion for it; both
    shapes must connect their operands instead of silently contributing no
    edge at all.
    """

    nodes = [_knowledge_id(node) for node in graph.get("nodes") or []]
    node_set = set(nodes)
    adjacency: dict[str, list[str]] = {node: [] for node in nodes}
    links: list[str] = []

    def connect(left: str, right: str) -> None:
        if left not in node_set or right not in node_set or left == right:
            return
        adjacency[left].append(right)
        adjacency[right].append(left)
        links.append(f"{left}~{right}")

    def relate(inputs: Iterable[Any], conclusion: Any) -> None:
        operands = [_knowledge_id(value) for value in inputs if _knowledge_id(value) in node_set]
        target = _knowledge_id(conclusion) if conclusion is not None else None
        if target is not None and target not in node_set:
            target = None
        if target is not None:
            operands.append(target)
        for index, left in enumerate(operands):
            for right in operands[index + 1:]:
                connect(left, right)

    for operator in graph.get("operators") or []:
        relate(operator.get("variables") or [], operator.get("conclusion"))
    for strategy in graph.get("strategies") or []:
        relate(strategy.get("premises") or [], strategy.get("conclusion"))
    return nodes, links, adjacency


def _components(nodes: Iterable[str], adjacency: Mapping[str, list[str]]) -> list[list[str]]:
    seen: set[str] = set()
    components: list[list[str]] = []
    for node in nodes:
        if node in seen:
            continue
        stack = [node]
        seen.add(node)
        members = [node]
        while stack:
            current = stack.pop()
            for neighbour in adjacency.get(current, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    members.append(neighbour)
                    stack.append(neighbour)
        components.append(members)
    components.sort(key=len, reverse=True)
    return components


def audit_formalization_connectivity(document: Mapping[str, Any]) -> JSONDict:
    """Measure connectivity of the formalization Graph that produces the IR."""

    graph = document.get("graph") or {}
    nodes, edge_ids, adjacency = reasoning_graph(graph)
    components = _components(nodes, adjacency)
    touched = {node for node in nodes if adjacency.get(node)}
    floating = [node for node in nodes if node not in touched]
    largest = len(components[0]) if components else 0
    return {
        "node_count": len(nodes),
        "reasoning_edge_count": len(edge_ids),
        "touched_node_count": len(touched),
        "floating_node_count": len(floating),
        "floating_node_ids": floating,
        "component_count": len(components),
        "component_sizes": [len(component) for component in components[:20]],
        "largest_component_size": largest,
        "largest_component_ratio": round(largest / len(nodes), 4) if nodes else 1.0,
        "connected": len(components) <= 1,
        "healthy": len(nodes) < MIN_CONNECTED_NODES or not floating,
    }


__all__ = [
    "MIN_CONNECTED_NODES",
    "audit_formalization_connectivity",
    "reasoning_graph",
]
