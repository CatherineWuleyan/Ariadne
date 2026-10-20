#!/usr/bin/env python3
"""Retrofit the base view with edge roles and the Step 3 links Step 4 drops.

Two problems this fixes for an already-merged graph:

1. Step 3 recognises ``special_case`` / ``reproduces`` / ``bounds``, but
   ``Gaia`` strategies only accept deduction/abduction/analogy/infer, so Step 4
   skips those weakpoints.  They never reach the view.  This script adds them
   as *typed display links* (not Gaia strategies) so the graph no longer hides
   the relations Step 3 actually judged.
2. The final view collapsed every reasoning edge to ``infer``.  This script
   attaches the audit's ``reasoning_type`` / ``chainable`` to every base edge.

The output is a view model plus an edge-role list; the answer layer and viewer
read both.  No Gaia compile is involved.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
for _extra in (_REPO, _REPO / "agent", _REPO / "pipelines" / "single_paper", _REPO / "pipelines" / "merge"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

DEFAULT_BASE = Path("outputs/aircraft/versions/03-reviewed-handcrafted-2026-10-14/source_snapshot/domain_view_model.json")
DEFAULT_STEP3 = Path(
    "outputs/aircraft/versions/02-native-311-2026-10-13/"
    "artifacts/blobs/artifact_42958528016f4cbe972f8675b327ee44.json"
)
DEFAULT_ROLES = Path("outputs/aircraft/final/edge_roles.json")
DEFAULT_OUT_VIEW = Path("outputs/aircraft/final/domain_view_typed.json")
DEFAULT_OUT_ROLES = Path("outputs/aircraft/final/edge_roles_typed.json")
# Step 4 cannot compile these into Gaia strategies; keep them as typed links.
DISPLAY_ONLY_TYPES = {"special_case", "reproduces", "bounds"}


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--view", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--step3", type=Path, default=DEFAULT_STEP3)
    parser.add_argument("--roles", type=Path, default=DEFAULT_ROLES)
    parser.add_argument("--out-view", type=Path, default=DEFAULT_OUT_VIEW)
    parser.add_argument("--out-roles", type=Path, default=DEFAULT_OUT_ROLES)
    args = parser.parse_args()

    view = copy.deepcopy(read_json(args.view))
    nodes = {str(node.get("id")): node for node in view.get("nodes") or []}
    edges = view.setdefault("edges", [])
    edge_index = {
        (str(edge.get("source")), str(edge.get("target")), str(edge.get("semantic_type"))): edge
        for edge in edges
    }
    roles_payload = read_json(args.roles)
    role_list = roles_payload.get("edges") if isinstance(roles_payload.get("edges"), list) else roles_payload
    roles = {str(item.get("edge_id")): dict(item) for item in role_list if isinstance(item, Mapping)}
    # Attach roles to existing edges.
    for edge in edges:
        role = roles.get(str(edge.get("id")))
        if role is None:
            role = {
                "edge_id": str(edge.get("id")),
                "reasoning_type": "infer",
                "residual_reason": "unclear",
                "chainable": False,
                "reason": "no edge-role audit record; conservative residual infer",
            }
            roles[str(edge.get("id"))] = role
        edge["reasoning_type"] = role.get("reasoning_type")
        edge["residual_reason"] = role.get("residual_reason")
        edge["chainable"] = bool(role.get("chainable"))
        edge["role_reason"] = role.get("reason")
        details = dict(edge.get("details") or {})
        details.update({
            "reasoning_type": role.get("reasoning_type"),
            "residual_reason": role.get("residual_reason"),
            "chainable": bool(role.get("chainable")),
            "role_reason": role.get("reason"),
        })
        edge["details"] = details

    # Add Step 3 links that Step 4 could not compile as Gaia strategies.
    step3 = read_json(args.step3)
    added = []
    for weakpoint in step3.get("weakpoints") or []:
        payload = weakpoint.get("payload") or {}
        reasoning_type = str(payload.get("reasoning_type") or "")
        if reasoning_type not in DISPLAY_ONLY_TYPES:
            continue
        expression = str(payload.get("expression") or "")
        for evidence in payload.get("evidence_claim_ids") or []:
            for target in payload.get("target_claim_id") or []:
                source = f"knowledge:{evidence}"
                target_id = f"knowledge:{target}"
                if source not in nodes or target_id not in nodes:
                    continue
                key = (source, target_id, reasoning_type)
                if key in edge_index:
                    continue
                edge_id = f"typed:{reasoning_type}:{evidence}:{target}"
                edge = {
                    "id": edge_id,
                    "source": source,
                    "target": target_id,
                    "label": reasoning_type,
                    "semantic_type": reasoning_type,
                    "edge_class": "reasoning",
                    "layer": "weakpoints",
                    "step": 4,
                    "visible_at": ["overview", "standard"],
                    "min_granularity": "overview",
                    "reasoning_type": reasoning_type,
                    "residual_reason": None,
                    "chainable": reasoning_type == "deduction",
                    "role_reason": expression,
                    "details": {
                        "source": evidence,
                        "target": target,
                        "relation": reasoning_type,
                        "reasoning_type": reasoning_type,
                        "residual_reason": None,
                        "chainable": reasoning_type == "deduction",
                        "role_reason": expression,
                        "step3_weakpoint_id": weakpoint.get("id"),
                    },
                }
                edges.append(edge)
                edge_index[key] = edge
                roles[edge_id] = {
                    "edge_id": edge_id,
                    "reasoning_type": reasoning_type,
                    "residual_reason": None,
                    "chainable": reasoning_type == "deduction",
                    "reason": expression or f"Step 3 {reasoning_type} link (display-only; not a Gaia strategy)",
                }
                added.append(edge)

    role_list = sorted(roles.values(), key=lambda item: str(item.get("edge_id")))
    counts = Counter(str(item.get("reasoning_type") or "unknown") for item in role_list)
    metadata = dict(view.get("metadata") or {})
    metadata.update({
        "edge_role_counts": dict(sorted(counts.items())),
        "chainable_edge_count": sum(1 for item in role_list if item.get("chainable")),
        "typed_display_edge_count": len(added),
        "typed_display_edge_types": dict(sorted(Counter(edge["semantic_type"] for edge in added).items())),
    })
    view["metadata"] = metadata
    args.out_view.parent.mkdir(parents=True, exist_ok=True)
    args.out_view.write_text(json.dumps(view, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    args.out_roles.write_text(json.dumps({
        "schema": "edge_role_audit",
        "version": "1",
        "purpose": "base view edge roles + display-only Step 3 links (special_case/reproduces/bounds)",
        "edge_count": len(role_list),
        "type_counts": dict(sorted(counts.items())),
        "chainable_types": ["deduction"],
        "edges": role_list,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({
        "view": str(args.out_view),
        "roles": str(args.out_roles),
        "nodes": len(view.get("nodes") or []),
        "edges": len(edges),
        "added_display_edges": len(added),
        "edge_role_counts": dict(sorted(counts.items())),
        "chainable_edge_count": metadata["chainable_edge_count"],
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
