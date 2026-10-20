"""Cross-paper domain graph viewer: only the relations *between* papers.

This projection is deliberately narrower than :mod:`pipeline_merge.view`.  It
carries **no domain-conclusion layer**:

* every node is a claim that already exists in a frozen Paper Package, with the
  claim text as that package produced it (no summarized premise ``A``, no
  cluster conclusion ``C``, no domain conclusion ``K``);
* every edge is an operator or weakpoint that Step 3 judged between claims from
  **different** packages, which is the only shape ``_validate_operator`` accepts
  for operators.

Why the conclusion layer is dropped here
----------------------------------------
The summary/conclusion synthesis is a different product.  ``_observation_summary_prompt``
requires the synthesized premise to "name no dataset, model, method or metric,
and contain no number", and the conclusion tree then builds ``K`` on top of those
premises.  The result is a graph whose headline claims are strictly wider than
their support.  A cross-paper relation graph that keeps the original claim text
does not have that problem, so this adapter refuses to render the generated
nodes even when a stale artifact still contains them.

Node granularity
----------------
Claims that take part in at least one cross-paper relation are ``overview``
(shown by default).  Claims that take part in none are ``standard``: they stay in
the model and in the search index, but a default view does not fill the canvas
with isolated nodes.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from pipeline_harness.models import ArtifactRef, JSONDict
from pipeline_harness.store import RunStore
from pipeline_harness.view.model import ViewDocument

from .claim_hygiene import apply_hygiene, quantity_conflict
from .relations import reconcile_operator_types, reconcile_rendered_arms
from .step3 import _HELPER_ID_RE

_PROPOSAL_KIND = "integration.step3_proposals"
_POOL_KIND = "cross_paper.pool"
_LAYER = "cross_paper"

#: Deterministic Chinese names for the relation vocabulary.  The judge emits a
#: closed set of types, so this is a lookup rather than a translation task.
RELATION_ZH = {
    "deduction": "演绎",
    "abduction": "溯因",
    "analogy": "类比",
    "infer": "归纳",
    "induction": "归纳",
    "conjunction": "合取",
    "disjunction": "析取",
    "equivalence": "等价",
    "contradiction": "矛盾",
    "negation": "否定",
    "reproduces": "独立复现",
    "bounds": "给出边界",
    "special_case": "特例",
}

#: Relation families drawn differently by the viewer.
RELATION_FAMILY = {
    "deduction": "entail",
    "infer": "entail",
    "induction": "entail",
    "abduction": "explain",
    "analogy": "analogy",
    "conjunction": "joint",
    "disjunction": "joint",
    "equivalence": "identity",
    "contradiction": "conflict",
    "negation": "conflict",
    "reproduces": "entail",
    "bounds": "joint",
    "special_case": "identity",
}


#: A node whose text is a derivation rather than a proposition.  Measured on the
#: 7-paper graph: 18 of 165 nodes are formula blocks with a parameter glossary
#: ("... where l_0 = ..., B_0 = ..., Phi_0 = ..."), the longest 1,277 characters
#: with 82 math fragments.  Such a node states several things at once, so no
#: relation between it and another claim can be checked, and it dominates the
#: canvas.  They stay in the model (and in search) but are hidden by default.
_MATH = re.compile(r"\$[^$]*\$|\\[A-Za-z]+")


#: A claim that reports a measurement rather than asserting a general conclusion.
#: Used only to pick the node's id letter, so a coarse marker is enough: a
#: measurement instrument/verb together with a value.
_MEASURE = re.compile(
    r"\b(?:measured|measurement|we (?:find|found|observe|observed|obtain|obtained)|"
    r"resistivity|magnetoresistance|magnetization|torque|heat capacity|specific heat|"
    r"conductance|spectroscop|diffraction|Raman|EDS|STM|ARPES|transport|"
    r"experimental(?:ly)?|data)\b",
    re.I,
)
_NUMBER = re.compile(r"\d")


#: 内联参数定义从句的锚点。实测 500 节点里 85 个带这种尾巴，是"非原子"的主因：
#: "... become Gaussian-localized: <formula>, with width l_B=..., where l=1..N labels
#: layers, B_0 = ... is the characteristic field, Phi_0 = ... is the flux quantum ..."
#: 断言本身和一串定义被塞进同一个节点，于是这个节点既不可判定、也最占画面。
#: 锚点取**最后**一个 where/here 从句：句首的 "with ... and center ..., where ..." 里，
#: 真正的参数字典在 where 之后，取第一个会把断言中段误划进定义。
_GLOSSARY_ANCHORS = (
    re.compile(r",?\s+(?:where|here)\s+", re.I),
    re.compile(r",?\s+with\s+", re.I),
)
#: 一条定义至少要有"符号 + 是/等于"的结构，否则不算定义。
_DEF_LIKE = re.compile(r"(\bis\b|\bare\b|\bdenotes\b|\blabels\b|\bdefined\b|≡|=)", re.I)


def _split_outside_math(text: str) -> list[str]:
    """Split a glossary tail into definitions, never cutting inside ``$...$``.

    A cut is only taken when the text after it still looks like a definition, so
    a formula containing a comma is not torn in half.
    """
    parts, buf, in_math, i = [], "", False, 0
    while i < len(text):
        ch = text[i]
        if ch == "$":
            in_math = not in_math
        if not in_math:
            cut = 0
            if ch == ";":
                cut = 1
            elif text.startswith(", and ", i):
                cut = 6
            elif text.startswith(", ", i):
                lookahead = text[i + 2:i + 70]
                if _DEF_LIKE.search(lookahead):
                    cut = 2
            if cut:
                parts.append(buf)
                buf = ""
                i += cut
                continue
        buf += ch
        i += 1
    parts.append(buf)
    return [part.strip(" ,.;:") for part in parts if part.strip(" ,.;:")]


def split_definitions(text: str) -> tuple[str, list[str]]:
    """Separate an assertion from an inlined parameter glossary.

    Returns ``(body, definitions)``.  A definition is background, not a premise:
    it fixes what a symbol means, it does not support the assertion.  Splitting
    therefore makes both halves self-contained -- the body becomes one readable
    assertion, each definition becomes one readable definition -- and nothing is
    lost from the node's meaning.
    """
    for anchor in _GLOSSARY_ANCHORS:
        for match in reversed(list(anchor.finditer(text))):
            body = text[:match.start()].strip()
            tail = text[match.end():].strip()
            if len(body) < 80 or len(tail) < 20:
                continue
            defs = [d for d in _split_outside_math(tail) if 12 <= len(d) <= 320]
            # 至少两条、且多数条目确实像定义，才认这是参数字典
            if len(defs) < 2 or len(defs) > 8:
                continue
            if sum(1 for d in defs if _DEF_LIKE.search(d)) < max(2, len(defs) - 1):
                continue
            return body, defs
    return text, []


def node_kind(text: str) -> str:
    """Return the single-letter id class: ``O`` observation, ``T`` derivation, ``C`` claim."""
    if is_derivation(text):
        return "T"
    return "O" if (_MEASURE.search(text) and _NUMBER.search(text)) else "C"


def is_derivation(text: str) -> bool:
    """True when a node is a formula/derivation block, not an atomic claim."""
    math = len(_MATH.findall(text))
    return len(text) > 600 or (math > 12 and len(text) > 350)


def _read_json(store: RunStore, ref: ArtifactRef) -> JSONDict:
    with store.artifact_path(ref).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _claims(store: RunStore, artifacts: list[ArtifactRef]) -> dict[str, JSONDict]:
    """Mirror ``step3._load_records`` so the operator qids resolve here too."""
    records: dict[str, JSONDict] = {}
    for ref in artifacts:
        if ref.kind != "gaia.ir":
            continue
        payload = _read_json(store, ref)
        namespace = str(payload.get("namespace") or "")
        package_name = str(payload.get("package_name") or "")
        package = f"{namespace}:{package_name}" if namespace and package_name else package_name or namespace
        for item in payload.get("knowledges") or []:
            if not isinstance(item, Mapping):
                continue
            qid, content = item.get("id"), item.get("content")
            if not isinstance(qid, str) or not qid or not isinstance(content, str) or not content.strip():
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
            source_knowledge_id = str(metadata.get("source_knowledge_id") or "")
            if (metadata.get("helper_visibility") == "formal_internal"
                    or metadata.get("visibility") == "strategy_interface"
                    or metadata.get("generated_kind") == "interface_claim"
                    or "step4_weakpoint_relation" in source_knowledge_id
                    or _HELPER_ID_RE.search(qid)):
                continue
            records[qid] = {
                "qid": qid,
                "package": package,
                "content": content,
                "type": item.get("type", "claim"),
                "metadata": dict(metadata),
                "source_anchor_ids": list(item.get("source_anchor_ids") or []),
            }
    # Same hygiene the judge saw, so the projection cannot show a fragment, a
    # leaked pipeline pointer, or one proposition under two ids.
    cleaned, _merges = apply_hygiene(records)
    return cleaned


def _target_question() -> JSONDict:
    """The frozen question the graph exists to answer.

    Without a declared question there is no criterion for whether the graph is
    adequate: reasonableness is only defined relative to a question.  Read from
    ``GAIA_CROSS_PAPER_QUESTION`` or the default repo-relative path.
    """
    candidates = []
    if os.environ.get("GAIA_CROSS_PAPER_QUESTION"):
        candidates.append(Path(os.environ["GAIA_CROSS_PAPER_QUESTION"]))
    candidates.append(Path(__file__).resolve().parents[3] / "outputs" / "physics_domain" / "target_question.json")
    for path in candidates:
        try:
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("question_zh"):
                    return payload
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _materials() -> JSONDict:
    """Declared per-paper material and inversion symmetry.

    This matters for the graph's soundness: the orbital-FFLO papers study bulk
    2H-NbSe2/NbS2, which is centrosymmetric (P6_3/mmc) and therefore cannot host
    bulk Ising protection at all, while the Ising claims are made for the
    non-centrosymmetric 4H_a / 3R polytypes.  A "contradiction" between the two
    camps is then a cross-symmetry-class comparison, not a same-material one.
    """
    candidates = []
    if os.environ.get("GAIA_CROSS_PAPER_MATERIALS"):
        candidates.append(Path(os.environ["GAIA_CROSS_PAPER_MATERIALS"]))
    candidates.append(Path(__file__).resolve().parents[3] / "outputs" / "physics_domain" / "materials.json")
    for path in candidates:
        try:
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("papers"):
                    return payload
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _theory_layer() -> JSONDict:
    """Curated theory layer: axioms, the master equation, channel theorems.

    Read from ``GAIA_CROSS_PAPER_THEORY`` or the default repo-relative path.  It
    is declared knowledge rather than run output, so it deliberately sits outside
    the frozen run inputs: the theory structure is edited by hand and re-projected
    without re-running the (expensive) merge.
    """
    candidates = []
    if os.environ.get("GAIA_CROSS_PAPER_THEORY"):
        candidates.append(Path(os.environ["GAIA_CROSS_PAPER_THEORY"]))
    candidates.append(Path(__file__).resolve().parents[3] / "outputs" / "physics_domain" / "theory_layer.json")
    for path in candidates:
        try:
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("nodes"):
                    return payload
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _pool_annotations(store: RunStore, artifacts: list[ArtifactRef]) -> dict[str, JSONDict]:
    for ref in artifacts:
        if ref.kind == _POOL_KIND:
            payload = _read_json(store, ref)
            if isinstance(payload, dict):
                return payload
    return {}


def _packages_of(records: Mapping[str, JSONDict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for qid, record in records.items():
        out.setdefault(str(record["package"]), []).append(qid)
    return out


def _edges_from(
    operators: list[JSONDict],
    weakpoints: list[JSONDict],
) -> tuple[list[tuple[str, str, str, JSONDict]], list[JSONDict]]:
    """One drawn arm per operand pair, tagged with the operator that asserts it.

    A k-ary operator is drawn as every pair of its operands rather than as a star
    from an arbitrary "first" operand: the star makes the first operand look like
    a hub the gate never asserted, and it silently drops the other pairs.
    """
    arms: list[tuple[str, str, str, JSONDict]] = []
    for operator in operators:
        variables = [str(v) for v in operator.get("variables") or []]
        op_type = str(operator.get("type") or "")
        if len(variables) < 2 or not op_type:
            continue
        detail = {
            "relation": op_type,
            "operator_id": str(operator.get("id") or ""),
            "arity": len(variables),
            "expression": operator.get("expression"),
            "operands": variables,
            # 算子的 variables 是"共同参与这条推理的若干断言"，边是它们两两之间的
            # 对称关系，没有前提/结论之分 —— 所以推理链不能沿它定向遍历。
            "directed": False,
        }
        for index, left in enumerate(variables):
            for right in variables[index + 1:]:
                arms.append((left, right, op_type, detail))
    for weakpoint in weakpoints:
        payload = weakpoint.get("payload") if isinstance(weakpoint.get("payload"), Mapping) else {}
        targets = [str(t) for t in payload.get("target_claim_id") or []]
        evidences = [str(e) for e in payload.get("evidence_claim_ids") or []]
        reasoning = str(payload.get("reasoning_type") or "infer")
        if not targets:
            continue
        detail = {
            "relation": reasoning,
            "operator_id": str(weakpoint.get("id") or ""),
            "arity": len(evidences) + len(targets),
            "expression": payload.get("expression"),
            "directed": True,
        }
        for evidence in evidences:
            for target in targets:
                arms.append((evidence, target, reasoning, detail))
    return arms


class CrossPaperViewAdapter:
    """Project the cross-paper relation set as a standalone domain graph."""

    version = "1"

    def project(
        self,
        store: RunStore,
        artifacts: list[ArtifactRef],
        options: Mapping[str, Any] | None = None,
    ) -> ViewDocument:
        options = dict(options or {})
        include_isolated = options.get("include_isolated", True)
        records = _claims(store, artifacts)
        pool = _pool_annotations(store, artifacts)
        papers = pool.get("papers") if isinstance(pool.get("papers"), dict) else {}
        translations = pool.get("translations") if isinstance(pool.get("translations"), dict) else {}

        proposals: JSONDict = {}
        for ref in artifacts:
            if ref.kind == _PROPOSAL_KIND:
                proposals = _read_json(store, ref)
        operators = [item for item in proposals.get("operators") or [] if isinstance(item, Mapping)]
        weakpoints = [item for item in proposals.get("weakpoints") or [] if isinstance(item, Mapping)]
        operators, _type_conflicts = reconcile_operator_types(operators)

        arms = [
            (source, target, label, detail)
            for source, target, label, detail in _edges_from(operators, weakpoints)
            if source in records and target in records
            # The relation gate already requires two packages for operators; a
            # weakpoint is re-checked here because it is the shape that can still
            # arrive from a stale artifact.
            and records[source]["package"] != records[target]["package"]
        ]
        # A relation between two claims that measure different quantities of the
        # same symbol (in-plane vs perpendicular critical field, onset vs
        # zero-resistance critical temperature) reads as a real inference but its
        # premise is about the other measurement.  Such an arm is dropped and
        # reported rather than drawn.
        quantity_drops: list[JSONDict] = []
        kept_arms = []
        for source, target, label, detail in arms:
            conflict = quantity_conflict(records[source]["content"], records[target]["content"])
            if conflict is None:
                kept_arms.append((source, target, label, detail))
            else:
                base, left_mods, right_mods = conflict
                quantity_drops.append({
                    "source": source, "target": target, "relation": label,
                    "base": base, "source_kind": sorted(left_mods), "target_kind": sorted(right_mods),
                })
        arms = kept_arms

        drawn, arm_conflicts = reconcile_rendered_arms(
            [(source, target, label) for source, target, label, _ in arms])
        keep = {(source, target, label) for source, target, label in drawn}
        arms = [arm for arm in arms if (arm[0], arm[1], arm[2]) in keep]

        degree: dict[str, int] = {}
        neighbours: dict[str, set[str]] = {}
        for source, target, _label, _detail in arms:
            degree[source] = degree.get(source, 0) + 1
            degree[target] = degree.get(target, 0) + 1
            neighbours.setdefault(source, set()).add(target)
            neighbours.setdefault(target, set()).add(source)

        def package_meta(package: str) -> JSONDict:
            entry = papers.get(package)
            return dict(entry) if isinstance(entry, Mapping) else {}

        # 唯一编号 <类型字母><论文号>-<序号>，例如 O1-1 = 第 1 篇的第 1 条实验观察。
        # 论文号按 pool 的顺序（即 results 文件顺序）固定下来，图里和图外一致。
        # 论文号取 pool 注解里的顺序（driver 按 arXiv 升序写入），而不是包名哈希：
        # 编号要能在图外用同一张表解释清楚。
        packages_in_pool = [key for key in papers if isinstance(key, str)]
        present = {str(record["package"]) for record in records.values()}
        ordered = [package for package in packages_in_pool if package in present]
        ordered += [package for package in sorted(present) if package not in ordered]
        paper_order = {package: index + 1 for index, package in enumerate(ordered)}
        counters: dict[tuple[int, str], int] = {}
        codes: dict[str, str] = {}
        for qid in sorted(records):
            record = records[qid]
            kind = node_kind(str(record["content"]))
            paper_index = paper_order[str(record["package"])]
            key = (paper_index, kind)
            counters[key] = counters.get(key, 0) + 1
            codes[qid] = f"{kind}{paper_index}-{counters[key]}"

        # 先把内联的参数定义从句拆出来：断言节点只留断言，每条定义单独成节点。
        # 定义是 background 而不是 premise —— 它固定符号含义，不支撑断言 —— 所以
        # 两者之间画的是"定义"边，与推理边分开。
        bodies: dict[str, str] = {}
        definition_map: dict[str, list[str]] = {}
        for qid in sorted(records):
            body, defs = split_definitions(str(records[qid]["content"]))
            bodies[qid] = body
            if defs:
                definition_map[qid] = defs
        # (合成 id, 原 claim qid, 编号, 定义文本, 论文号) —— 原 qid 必须单独留着：
        # qid 形如 "papers:paper_X::claim_N"，按 "::" 取 [0] 得到的是包名而不是 claim。
        definition_nodes: list[tuple[str, str, str, str, int]] = []
        for qid in sorted(definition_map):
            package = str(records[qid]["package"])
            index = paper_order[package]
            for offset, text in enumerate(definition_map[qid], start=1):
                counters[(index, "D")] = counters.get((index, "D"), 0) + 1
                code = f"D{index}-{counters[(index, 'D')]}"
                definition_nodes.append((f"{qid}::def{offset}", qid, code, text, index))

        # 每篇的体系与反演对称性：中心对称的块体 2H 在对称性上不可能有 Ising 保护，
        # 而 Ising 主张是针对非中心对称的 4H_a / 3R 多型提出的。标注出来之后，
        # 两端体系不同的推理边才不会被误读成同一材料内的矛盾。
        _mat_table = (_materials().get("papers") or {})
        package_material: dict[str, JSONDict] = {}
        for package in ordered:
            arxiv = str((papers.get(package) or {}).get("arxiv") or "")
            package_material[package] = dict(_mat_table.get(arxiv) or {})

        nodes: list[JSONDict] = []
        for qid in sorted(records):
            record = records[qid]
            package = str(record["package"])
            meta = package_meta(package)
            translation = translations.get(qid) if isinstance(translations, Mapping) else None
            # 拆出的定义若在翻译缓存里没有对应条目，就保留英文原文，绝不丢内容
            if definition_map.get(qid) and isinstance(translation, Mapping):
                translation = dict(translation)
            translation = translation if isinstance(translation, Mapping) else {}
            connected = degree.get(qid, 0) > 0
            body_text = bodies.get(qid, str(record["content"]))
            node_class = "derivation" if is_derivation(body_text) else "claim"
            code = codes[qid]
            # 推导块默认不显示：它不可判定，且最占画面。可见性与孤立节点分开，
            # 因为"没连上"和"不是命题"是两件不同的事。
            visible_by_default = connected and node_class == "claim"
            zh_label = str(translation.get("zh_label") or "").strip()
            zh_summary = str(translation.get("zh_summary") or "").strip()
            # Fall back to the claim's own text, never the paper title: a
            # paper-title fallback made every node of one paper carry the same
            # label, so the canvas showed 34 copies of one string and no node was
            # identifiable before the translation cache existed.
            # 节点显示编号；中文短标签与全文放到详情面板，工具提示里也带上编号，
            # 这样图上一个节点可以被唯一指认（"O1-1 那条"），而不是靠一长串中译。
            label = code
            summary = zh_summary or str(record["content"])
            nodes.append({
                "id": f"knowledge:{qid}",
                "entity_id": qid,
                "label": label,
                "kind": "claim",
                "layer": _LAYER,
                "summary": summary,
                "min_granularity": "overview" if visible_by_default else "standard",
                "visible_at": ["overview", "standard"] if visible_by_default else ["standard"],
                "source_anchor_ids": list(record.get("source_anchor_ids") or []),
                "details": {
                    "qid": qid,
                    "paper": package,
                    "paper_arxiv": meta.get("arxiv"),
                    "paper_title": meta.get("en_title"),
                    "paper_title_zh": meta.get("zh_title"),
                    "paper_role": meta.get("role"),
                    "english": body_text,
                    "definitions": definition_map.get(qid, []),
                    "full_english": record["content"],
                    "degree": degree.get(qid, 0),
                    "cross_paper_neighbours": len(neighbours.get(qid, ())),
                    "anchor_count": len(record.get("source_anchor_ids") or []),
                    "connected": connected,
                    "node_class": node_class,
                    "code": code,
                    "label_zh": zh_label or str(record["content"])[:40],
                    "paper_index": paper_order[str(package)],
                    "system": str(package_material.get(str(package), {}).get("system") or ""),
                    "symmetry": str(package_material.get(str(package), {}).get("symmetry") or ""),
                    "ising_allowed": package_material.get(str(package), {}).get("ising_allowed"),
                },
            })

        # ---- 理论层：公理 / 主方程 / 通道定理 / 合成判据 ----------------
        theory = _theory_layer()
        # 公理身份由结构决定，不由 theory_layer.json 手写声明：在推导 DAG 里
        # "没有任何推导指向它"就是根（公理）。方向约定是"前提 → 结论"，
        # 所以公理 = 入度为 0；若把箭头反过来读（"依赖 → 依据"），
        # 同一条规则表现为"没有出边"。两种说法是一回事。
        _t_nodes = [str(i.get("id")) for i in (theory.get("nodes") or []) if isinstance(i, Mapping) and i.get("id")]
        _t_edges = [(str(a), str(b)) for a, b in (theory.get("derivations") or [])
                    if str(a) in _t_nodes and str(b) in _t_nodes]
        theory_in = {tid: 0 for tid in _t_nodes}
        theory_out = {tid: 0 for tid in _t_nodes}
        for left, right in _t_edges:
            theory_out[left] += 1
            theory_in[right] += 1
        theory_role = {
            tid: ("axiom" if theory_in[tid] == 0 and theory_out[tid] > 0
                  else "terminal" if theory_out[tid] == 0
                  else "derived")
            for tid in _t_nodes
        }
        theory_ids: set[str] = set()
        # 引用可以是图上 code，也可以是跨运行稳定的 qid。理论层应当用 qid：
        # code 是按论文顺序在每次运行里现生成的，核心图与全量图的编号体系不同，
        # 用 code 引用会在换一个运行后静默指到别的节点。
        code_to_node: dict[str, str] = {}
        for node in nodes:
            code_to_node[str(node["details"].get("code"))] = node["id"]
            code_to_node[str(node["details"].get("qid"))] = node["id"]
        for item in theory.get("nodes") or []:
            if not isinstance(item, Mapping) or not item.get("id"):
                continue
            tid = str(item["id"])
            theory_ids.add(tid)
            nodes.append({
                "id": f"theory:{tid}",
                "entity_id": tid,
                "label": tid,
                "kind": "theory",
                "layer": _LAYER,
                "summary": str(item.get("zh") or item.get("en") or ""),
                "min_granularity": "overview",
                "visible_at": ["overview", "standard"],
                "source_anchor_ids": [],
                "details": {
                    "qid": f"theory:{tid}", "code": tid, "node_class": "theory",
                    "label_zh": str(item.get("label_zh") or tid),
                    "english": str(item.get("en") or ""),
                    "theory_kind": theory_role.get(tid) or str(item.get("kind") or "theorem"),
                    "declared_kind": str(item.get("kind") or "theorem"),
                    "theory_in_degree": theory_in.get(tid, 0),
                    "theory_out_degree": theory_out.get(tid, 0),
                    "condition": str((theory.get("special_cases") or {}).get(tid) or ""),
                    "is_basic": bool(item.get("is_basic")),
                    "degree": 0, "connected": True, "paper": "theory",
                    "paper_index": None,
                },
            })

        for synthetic_id, qid, code, text, index in definition_nodes:
            package = str(records[qid]["package"])
            # 定义没有自己的翻译条目：翻译缓存按原 claim 的 qid 索引，取它会拿到
            # **断言**的中译，于是定义节点会显示成它所属断言的中文，属内容错误。
            # 没有独立译文时保留定义原文。
            zh = (translations.get(synthetic_id) or {}).get("zh_summary") if isinstance(
                translations.get(synthetic_id), Mapping) else None
            nodes.append({
                "id": f"knowledge:{synthetic_id}",
                "entity_id": synthetic_id,
                "label": code,
                "kind": "definition",
                "layer": _LAYER,
                "summary": zh or text,
                "min_granularity": "standard",
                "visible_at": ["standard"],
                "source_anchor_ids": [],
                "details": {
                    "qid": synthetic_id, "paper": package, "code": code,
                    "node_class": "definition",
                    "label_zh": str(zh or text)[:40],
                    "english": text,
                    "degree": 0,
                    "connected": False,
                    "paper_index": index,
                    "defined_for": sorted(k for k, v in definition_map.items() if text in v),
                },
            })

        edges: list[JSONDict] = []
        seen: set[tuple[str, str, str]] = set()
        for source, target, label, detail in sorted(
                arms, key=lambda arm: (arm[0], arm[1], arm[2], str(arm[3].get("operator_id")))):
            key = (source, target, label)
            if key in seen:
                continue
            seen.add(key)
            source_package = str(records[source]["package"])
            target_package = str(records[target]["package"])
            edges.append({
                "id": f"cross:{source}:{target}:{label}",
                "source": f"knowledge:{source}",
                "target": f"knowledge:{target}",
                "label": RELATION_ZH.get(label, label),
                "semantic_type": label,
                "edge_class": "reasoning",
                "family": RELATION_FAMILY.get(label, "other"),
                "layer": _LAYER,
                "min_granularity": "overview",
                "visible_at": ["overview", "standard"],
                "details": {
                    **detail,
                    "relation_zh": RELATION_ZH.get(label, label),
                    "source_paper": source_package,
                    "target_paper": target_package,
                    "cross_paper": source_package != target_package,
                },
            })

        # 理论→理论：推导边。若目标被 special_cases 标注了适用条件，这条边是"特例"
        # 而不是一般推导 —— 通式在每个几何极限下退化成不同公式，两者是限定关系而非竞争。
        special = theory.get("special_cases") or {}
        for left, right in theory.get("derivations") or []:
            left, right = str(left), str(right)
            if left not in theory_ids or right not in theory_ids:
                continue
            condition = str(special.get(right) or "")
            is_case = bool(condition)
            edges.append({
                "id": f"derives:{left}:{right}",
                "source": f"theory:{left}", "target": f"theory:{right}",
                "label": "特例" if is_case else "推导",
                "semantic_type": "special_case" if is_case else "derives",
                "edge_class": "reasoning",
                "family": "special_case" if is_case else "derivation", "layer": _LAYER,
                "min_granularity": "overview", "visible_at": ["overview", "standard"],
                "details": {"relation": "special_case" if is_case else "derives",
                            "relation_zh": "特例" if is_case else "推导", "arity": 2,
                            "directed": True, "condition": condition,
                            "source_paper": "theory", "target_paper": "theory",
                            "cross_paper": False},
            })
        # 证据→理论：支撑边（用图中现有节点的 code 指认）
        missing_support: list[str] = []
        for tid, codes in (theory.get("support") or {}).items():
            tid = str(tid)
            if tid not in theory_ids:
                continue
            for code in codes or []:
                target = code_to_node.get(str(code))
                if target is None:
                    # 用 qid 引用时图上没有对应节点，同样留痕而不是静默丢弃
                    missing_support.append(str(code))
                    continue
                edges.append({
                    "id": f"supports:{code}:{tid}",
                    "source": target, "target": f"theory:{tid}",
                    "label": "支撑", "semantic_type": "supports", "edge_class": "reasoning",
                    "family": "support", "layer": _LAYER,
                    "min_granularity": "overview", "visible_at": ["overview", "standard"],
                    "details": {"relation": "supports", "relation_zh": "支撑", "arity": 2,
                                "directed": True,
                                "source_paper": "evidence", "target_paper": "theory",
                                "cross_paper": False},
                })

        for qid in sorted(definition_map):
            for offset, text in enumerate(definition_map[qid], start=1):
                edges.append({
                    "id": f"defines:{qid}:{offset}",
                    "source": f"knowledge:{qid}",
                    "target": f"knowledge:{qid}::def{offset}",
                    "label": "定义",
                    "semantic_type": "defines",
                    "edge_class": "background",
                    "family": "definition",
                    "layer": _LAYER,
                    "min_granularity": "standard",
                    "visible_at": ["standard"],
                    "details": {"relation": "defines", "relation_zh": "定义",
                                "arity": 2, "source_paper": str(records[qid]["package"]),
                                "target_paper": str(records[qid]["package"]), "cross_paper": False},
                })

        if not include_isolated:
            connected_ids = {node["id"] for node in nodes if node["details"]["connected"]}
            nodes = [node for node in nodes if node["id"] in connected_ids]
            edges = [edge for edge in edges
                     if edge["source"] in connected_ids and edge["target"] in connected_ids]

        # 统一给推理边标注"两端体系是否不同"。跨体系的关系不是错，但它和
        # "同一材料内的矛盾"是两种强度完全不同的主张，图上必须能分辨。
        _node_system = {node["id"]: str(node["details"].get("system") or "") for node in nodes}
        # 有物理意义的分类不是体系字符串（每篇都不同），而是"该体系是否允许 Ising 保护"：
        # 非中心对称可有、块体中心对称不可有、moiré/模型另论。
        def _sym_class(value: object) -> str:
            if value is True:
                return "Ising 可有（非中心对称）"
            if value is False:
                return "Ising 不可有（块体中心对称）"
            return "另论（moiré / 模型 / 待确认）"

        _node_sym = {node["id"]: _sym_class(node["details"].get("ising_allowed")) for node in nodes}
        cross_material = 0
        cross_symmetry = 0
        for edge in edges:
            if edge["semantic_type"] in {"defines", "derives", "supports"}:
                continue
            a = _node_system.get(edge["source"], "")
            b = _node_system.get(edge["target"], "")
            sa = _node_sym.get(edge["source"], "")
            sb = _node_sym.get(edge["target"], "")
            differs = bool(a) and bool(b) and a != b
            sym_differs = bool(sa) and bool(sb) and sa != sb
            edge["details"]["cross_material"] = differs
            edge["details"]["cross_symmetry_class"] = sym_differs
            if differs:
                cross_material += 1
            if sym_differs:
                cross_symmetry += 1

        relation_counts: dict[str, int] = {}
        for edge in edges:
            relation_counts[edge["semantic_type"]] = relation_counts.get(edge["semantic_type"], 0) + 1
        pair_papers: set[tuple[str, str]] = set()
        for edge in edges:
            pair_papers.add(tuple(sorted((edge["details"]["source_paper"], edge["details"]["target_paper"]))))

        search_documents = [
            {
                "id": f"search:{node['id']}",
                "title": node["label"],
                "text": " ".join(filter(None, [
                    node["label"], str(node["details"].get("label_zh") or ""),
                    node["summary"], str(node["details"].get("english") or ""),
                    str(node["details"].get("qid") or ""), str(node["details"].get("paper") or ""),
                ])),
                "tags": ["claim", _LAYER, "cross-paper" if node["details"]["connected"] else "isolated"],
                "refs": [node["id"]],
                "metadata": {"degree": node["details"]["degree"]},
            }
            for node in nodes
        ]

        return ViewDocument(
            title="跨论文推理关系图 · 2D Ising 超导面内临界场",
            source_artifacts=[ref.artifact_id for ref in artifacts],
            nodes=nodes,
            edges=edges,
            search_documents=search_documents,
            layers=[
                {"id": _LAYER, "label": "跨论文推理关系", "default_visible": True},
            ],
            metadata={
                "renderer": "force",
                "claim_count": len(records),
                "connected_claim_count": sum(1 for node in nodes if node["details"]["connected"]),
                # 口径说明：定义节点既不是断言也没有推理边，不能算进"孤立 claim"，
                # 定义边也不是跨论文关系，不能算进 cross_paper_edge_count。
                "isolated_claim_count": sum(
                    1 for node in nodes
                    if not node["details"]["connected"] and node["details"]["node_class"] != "definition"),
                "definition_edge_count": sum(1 for edge in edges if edge["semantic_type"] == "defines"),
                "derivation_node_count": sum(1 for node in nodes if node["details"]["node_class"] == "derivation"),
                "id_scheme": "<kind><paper>-<n>, kind = O observation / C claim / T derivation",
                "paper_index": {
                    str(index): {
                        "package": package,
                        "arxiv": (papers.get(package) or {}).get("arxiv"),
                        "zh_title": (papers.get(package) or {}).get("zh_title"),
                    }
                    for package, index in sorted(paper_order.items(), key=lambda kv: kv[1])
                },
                "cross_material_edge_count": cross_material,
                "cross_symmetry_edge_count": cross_symmetry,
                "contradiction_cross_symmetry_count": sum(
                    1 for edge in edges
                    if edge["semantic_type"] == "contradiction"
                    and edge["details"].get("cross_symmetry_class")),
                "target_question": str(_target_question().get("question_zh") or ""),
                "target_gold": str((_target_question().get("gold") or {}).get("statement_zh") or ""),
                "target_status": str(_target_question().get("status") or ""),
                "theory_node_count": len(theory_ids),
                "theory_axioms": sorted(t for t, r in theory_role.items() if r == "axiom"),
                "theory_terminals": sorted(t for t, r in theory_role.items() if r == "terminal"),
                "theory_roles": theory_role,
                "theory_support_missing": missing_support,
                "split_definition_count": sum(len(v) for v in definition_map.values()),
                "split_body_count": len(definition_map),
                # 只统计证据层节点：理论层编号 TH0-TH5 也以 T 开头，不排除会把 T 类虚增。
                "id_kind_counts": {
                    kind: sum(1 for node in nodes
                              if node["details"].get("node_class") != "theory"
                              and str(node["details"]["code"]).startswith(kind))
                    for kind in ("O", "C", "T", "D")
                },
                "proposition_node_count": sum(1 for node in nodes if node["details"]["node_class"] == "claim"),
                # 三种边分开计数：跨论文推理边 / 内部定义边 / 理论层的推导与支撑边。
                # 混在一起会让"跨论文关系有多少条"这个数虚高。
                "cross_paper_edge_count": sum(
                    1 for edge in edges
                    if edge["semantic_type"] not in {"defines", "derives", "supports"}),
                "theory_derivation_edge_count": sum(1 for edge in edges if edge["semantic_type"] == "derives"),
                # 只数理论层内部的限定边：merge 本身也会产出跨论文的"特例"边，
                # 不加这个约束会把两类完全不同的关系混成一个数。
                "theory_special_case_count": sum(
                    1 for edge in edges
                    if edge["semantic_type"] == "special_case" and edge["id"].startswith("derives:")),
                "theory_support_edge_count": sum(1 for edge in edges if edge["semantic_type"] == "supports"),
                "cross_paper_paper_pair_count": len(pair_papers),
                "relation_counts": relation_counts,
                "relation_counts_zh": {RELATION_ZH.get(k, k): v for k, v in relation_counts.items()},
                "dropped_operator_type_conflicts": len(_type_conflicts),
                "dropped_rendered_arm_conflicts": len(arm_conflicts),
                "dropped_quantity_conflicts": len(quantity_drops),
                "quantity_conflicts": quantity_drops[:20],
                "package_count": len({str(record["package"]) for record in records.values()}),
                "conclusion_layer": "absent_by_design",
                "relation_legend": RELATION_ZH,
            },
            stages=[{"number": 3, "name": "cross_paper_relations", "validation_status": "passed"}],
        )


__all__ = ["CrossPaperViewAdapter", "RELATION_ZH", "RELATION_FAMILY"]
