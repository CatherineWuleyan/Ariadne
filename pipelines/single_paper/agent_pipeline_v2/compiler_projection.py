"""Local V2-to-legacy authoring projection for an official Gaia compiler boundary."""
from __future__ import annotations

import copy
from typing import Any, Mapping

from .authoring import validate


def project_for_official_compiler(document: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude non-graph candidates and preserve relation hyperedges as workflow metadata."""
    validate(document)
    if any("conclusion" not in operator for operator in document["graph"]["operators"]):
        raise ValueError(
            "Authoring relation declarations omit Gaia's required conclusion helper; "
            "an explicitly reviewed compiler projection must generate it before official compilation."
        )
    if any(operator.get("background") or ("background" in operator and operator["type"] == "implication")
           for operator in document["graph"]["operators"]):
        raise ValueError(
            "Historical authoring Operators use conditional background notes or direct entailment; "
            "official Gaia Operator has no background and its implication is formula-valued. "
            "An explicitly reviewed compiler lowering is required; conditions must not be dropped."
        )
    knowledges = document["knowledges"]
    graph_nodes = list(document["graph"]["nodes"])
    # Notes remain outside graph.nodes, but must cross the compiler boundary
    # together with the strategies that reference them.
    for strategy in document["graph"].get("strategies", []):
        for key in strategy["background"]:
            if key not in graph_nodes:
                graph_nodes.append(key)
    legacy_knowledge = [{
        "id": knowledge_id,
        "type": "claim" if knowledges[knowledge_id]["type"] == "observation_claim" else knowledges[knowledge_id]["type"],
        "content": dict(knowledges[knowledge_id]["content"]) if knowledges[knowledge_id]["content"] is not None else None,
        "self_contained": True,
        "origin": "extracted",
        "visibility": "public",
        "source_anchor_ids": list(knowledges[knowledge_id].get("source_anchor_ids", [])),
        "external_ids": [],
        "epistemic": {"prior_status": "unset" if knowledges[knowledge_id]["type"] in {"claim", "observation_claim"} else "not_applicable", "prior_ref": None},
    } for knowledge_id in graph_nodes]
    return {
        "schema_version": document["schema_version"], "revision": dict(document["revision"]), "package": dict(document["package"]),
        "graph": {"knowledges": legacy_knowledge, "operators": copy.deepcopy(document["graph"]["operators"]),
                  "strategies": copy.deepcopy(document["graph"].get("strategies", [])), "composes": list(document["graph"]["composes"])},
        "workflow": dict(document["workflow"]),
    }
