#!/usr/bin/env python3
"""Export every multi-step chain that ends at an answer root to Markdown.

Two kinds of chains are exported, separately labelled:

* ``same-paper``: direct-support claims of one root grouped by paper, ordered by
  claim number.  This is a reading sequence, not a logical inference.
* ``graph-path``: the answer layer's ``exploratory_paths`` (old infer-based
  graph reachability), annotated with the audited edge type of each step.

Chains without an intermediate node (claim -> root) are not included.  The
Markdown includes one Mermaid overview per root plus a content table per chain.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

DEFAULT_ANSWER = Path("outputs/aircraft/final/answer_layer.json")
DEFAULT_VIEW = Path("outputs/aircraft/final/domain_view_question.json")
DEFAULT_OUT = Path("outputs/aircraft/final/ALL_CHAINS_TO_ROOTS.md")

ROLE_LABELS = {
    "deduction": "deduces", "special_case": "special case", "reproduces": "reproduces",
    "bounds": "bounds", "abduction": "explains", "analogy": "analogy",
    "infer": "infer", "same_paper": "same paper", "conjunction": "conjunction",
    "support": "answer support",
}
RESIDUAL_LABELS = {
    "precondition": "requires", "enabling": "enables", "instance": "instance",
    "unclear": "related",
}


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def clean(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("|", "\\|")
    return text[:limit] + ("…" if len(text) > limit else "")


def paper_of(node_id: str) -> str:
    match = re.match(r"^knowledge:papers:(paper_[^:]+)::", str(node_id))
    return match.group(1) if match else "?"


def claim_number(code: str) -> int:
    match = re.match(r"^P(\d+)", str(code))
    return int(match.group(1)) if match else 9999


def role_of(edge: Mapping[str, Any] | None, fallback: str = "infer") -> tuple[str, str]:
    if edge is None:
        return fallback, ROLE_LABELS.get(fallback, fallback)
    if edge.get("layer") == "answer":
        return "support", "answer support"
    rtype = str(edge.get("reasoning_type") or "infer")
    label = ROLE_LABELS.get(rtype, rtype)
    if rtype == "infer" and edge.get("residual_reason") in RESIDUAL_LABELS:
        label = RESIDUAL_LABELS[str(edge["residual_reason"])]
    return rtype, label


def same_paper_chains(root: Mapping[str, Any], nodes: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = {}
    for claim_id in root.get("support_ids") or []:
        node = nodes.get(str(claim_id))
        if not node:
            continue
        paper = str(node.get("paper") or paper_of(str(claim_id)))
        groups.setdefault(paper, []).append(str(claim_id))
    chains = []
    for paper, ids in groups.items():
        if len(ids) < 2:
            continue
        ids.sort(key=lambda item: claim_number(str(
            nodes[item].get("code") or nodes[item].get("display_label") or ""
        )))
        chains.append({
            "kind": "same-paper",
            "path": [*ids, str(root["id"])],
            "paper": paper,
            "depth": len(ids) + 1,
            "edge_ids": [None] * len(ids),
        })
    return chains


def graph_edge_for(
    source: str, target: str,
    edge_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    return edge_lookup.get((source, target))


def build_mermaid(
    root: Mapping[str, Any],
    chains: list[Mapping[str, Any]],
    nodes: Mapping[str, Mapping[str, Any]],
    edge_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> str:
    node_ids: dict[str, str] = {}
    def nid(node_id: str) -> str:
        if node_id not in node_ids:
            node_ids[node_id] = "n" + str(len(node_ids))
        return node_ids[node_id]
    edges: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for chain in chains:
        path = [str(item) for item in chain.get("path") or []]
        for index in range(len(path) - 1):
            source, target = path[index], path[index + 1]
            if chain["kind"] == "same-paper":
                label = "same paper"
            else:
                edge = graph_edge_for(source, target, edge_lookup)
                _, label = role_of(edge)
            key = (source, target, label)
            if key in seen:
                continue
            seen.add(key)
            edges.append(key)
    # Materialise every node before emitting node definitions; otherwise the
    # edge loop creates bare ids after the definition block and Mermaid renders
    # n0/n1 instead of claim codes.
    for chain in chains:
        for node_id in chain.get("path") or []:
            nid(str(node_id))
    lines = ["```mermaid", "flowchart LR"]
    for node_id in node_ids:
        node = nodes.get(node_id, {})
        code = str(node.get("code") or node.get("display_label") or node_id)
        if str(node_id) in {str(root["id"])}:
            lines.append(f'  {nid(node_id)}["{root["code"]}"]')
        else:
            lines.append(f'  {nid(node_id)}["{code}"]')
    for source, target, label in edges:
        lines.append(f'  {nid(source)} -->|"{label}"| {nid(target)}')
    lines.append("```")
    return "\n".join(lines)


def path_table(
    chain: Mapping[str, Any],
    root: Mapping[str, Any],
    nodes: Mapping[str, Mapping[str, Any]],
    edge_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> str:
    path = [str(item) for item in chain.get("path") or []]
    rows = []
    for index, node_id in enumerate(path):
        node = nodes.get(node_id, {})
        code = str(node.get("code") or node.get("display_label") or node_id)
        content = clean(node.get("summary"), 220)
        if index == len(path) - 1:
            relation = "**answer support**"
        elif chain["kind"] == "same-paper":
            relation = "**same paper** · direct-support reading order"
        else:
            edge = graph_edge_for(path[index], path[index + 1], edge_lookup)
            rtype, label = role_of(edge)
            reason = clean((edge or {}).get("role_reason"), 150)
            relation = f"**{label}**" + (f" · {reason}" if reason else "")
        rows.append(f"| {index + 1} | {code} | {content} | {relation} |")
    return "\n".join([
        "| # | Claim | 内容摘录 | 与下一步的关系 |",
        "| ---: | --- | --- | --- |",
        *rows,
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answer-layer", type=Path, default=DEFAULT_ANSWER)
    parser.add_argument("--view", type=Path, default=DEFAULT_VIEW)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    answer = read_json(args.answer_layer)
    view = read_json(args.view)
    nodes = {str(node["id"]): node for node in view.get("nodes") or []}
    edge_lookup: dict[tuple[str, str], Mapping[str, Any]] = {}
    for edge in view.get("edges") or []:
        key = (str(edge.get("source")), str(edge.get("target")))
        if key not in edge_lookup or edge.get("semantic_type") == "infer":
            edge_lookup[key] = edge

    sections = []
    summary_rows = []
    total_same = total_graph = 0
    for root in answer.get("roots") or []:
        same = same_paper_chains(root, nodes)
        graph = [dict(item, kind="graph-path") for item in root.get("exploratory_paths") or []]
        graph = [item for item in graph if int(item.get("depth") or 0) >= 3]
        total_same += len(same)
        total_graph += len(graph)
        all_chains = same + graph
        summary_rows.append(
            f"| {root['code']} | {len(same)} | {len(graph)} | {len(all_chains)} |"
        )
        lines = [f"## {root['code']} · {root.get('title_zh') or root.get('title_en')}", ""]
        lines.append(f"> {root.get('statement', '')}")
        lines.append("")
        if not all_chains:
            lines.append("_没有带中间节点的链。_")
            lines.append("")
            sections.append("\n".join(lines))
            continue
        lines.append("### Mermaid 总图")
        lines.append("")
        lines.append(build_mermaid(root, all_chains, nodes, edge_lookup))
        lines.append("")
        lines.append("### 逐链内容")
        lines.append("")
        for index, chain in enumerate(all_chains, 1):
            kind = "same-paper 证据序列" if chain["kind"] == "same-paper" else "graph path（exploratory）"
            codes = [
                str(nodes.get(str(item), {}).get("code") or nodes.get(str(item), {}).get("display_label") or item)
                for item in chain.get("path") or []
            ]
            lines.append(f"#### {root['code']}-{index:02d} · {kind} · depth {chain.get('depth')}")
            lines.append("")
            lines.append("`" + " → ".join(codes) + "`")
            lines.append("")
            if chain["kind"] == "same-paper":
                lines.append("同论文直接支持 claim，按 claim 编号排序；这是阅读顺序，不是逻辑蕴含。")
            else:
                lines.append("旧 infer/analogy 图路径；边类型来自人工审计，**不是 typed support chain**。")
            lines.append("")
            lines.append(path_table(chain, root, nodes, edge_lookup))
            lines.append("")
        sections.append("\n".join(lines))

    header = [
        "# 指向 R1–R5 的所有多步推理链",
        "",
        "日期：2026-10-18",
        "",
        "## 说明",
        "",
        "- 只收录**至少有一个中间节点**的链；`claim → root` 的直接支持不列为“链”。",
        "- `same-paper`：同一篇论文的直接支持 claim，按 claim 编号排列；这是阅读顺序，不是逻辑推理。",
        "- `graph path`：`answer_layer.exploratory_paths` 中的旧 infer 图路径；边类型来自人工审计，仍不是 typed support chain。",
        "- `analogy` / `infer` 是关系类型，不是 support；本文件不把它们升级为 deduction。",
        "- Mermaid 只画代码节点和边类型；具体内容在下方表格。",
        "",
        "## 汇总",
        "",
        "| Root | same-paper | graph path | 合计 |",
        "| --- | ---: | ---: | ---: |",
        *summary_rows,
        f"| **合计** | **{total_same}** | **{total_graph}** | **{total_same + total_graph}** |",
        "",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(header) + "\n" + "\n".join(sections), encoding="utf-8")
    print(json.dumps({
        "out": str(args.out), "same_paper": total_same, "graph_paths": total_graph,
        "total": total_same + total_graph,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
