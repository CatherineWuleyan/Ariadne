"""Merge viewer: compose the input Paper Packages with the integration delta.

The integration Package references Paper knowledge by external QID and adds only
its own cross-paper relations plus candidate K.  The projection splits into three
viewer layers so the merge delta can be isolated:

* ``claims``     — Paper claims that the merge did NOT touch (context).
* ``operators``  — package-internal reasoning edges (each paper's own graph).
* ``weakpoints`` — the merge delta: candidate K, the Paper claims its new
  relations touch, and those new cross-paper relations.

Hide ``claims`` and ``operators`` to see only the delta; hide ``weakpoints`` to
see the untouched Paper graph.  (The shared viewer only recognises the layer ids
``claims``/``operators``/``weakpoints``/``strategies``/``registry``, so the delta
layer reuses ``weakpoints``.)

Compiler-internal helper / interface nodes (``__*``, ``*_result``, AltExp,
``step4_weakpoint_*``, ``helper_relation_*``) are plumbing and never rendered.

Labels follow the frozen concise scheme, so the projection is readable without a
post-processing pass.  The legend is emitted as ``metadata.label_scheme``:

===========  ==========================================================
``K<n>·L<m>``  domain conclusion of the summary tree at level ``m``
``C<n>``       conclusion owned by an input Package (cluster context)
``A<n>``       summarized observation premise / assertion
``O<n>``       observation claim imported from a paper
``P<n>``       paper claim
===========  ==========================================================

The counters are stable for a fixed artifact set because nodes are emitted in a
deterministic order.  Nothing is lost by shortening: every node keeps its full
``qid`` and its pre-shortening label in ``details``.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Mapping

from pipeline_harness.models import ArtifactRef, JSONDict
from pipeline_harness.store import RunStore
from pipeline_harness.view.model import ViewDocument

from .relations import reconcile_operator_types, reconcile_rendered_arms

_INTEGRATION_STAGE = "step4_formalize_integration"
_STEP3_KIND = "integration.step3_proposals"
_STEP = 4
_DELTA_LAYER = "weakpoints"
_CONTEXT_LAYER = "claims"
_PAPER_EDGE_LAYER = "operators"
_HELPER_RE = re.compile(
    r"(^|::)__|helper[_-]?relation|operator[_-]?result|disjunction[_-]?result|"
    r"alternative[_-]?explanation|step4_weakpoint_",
    re.I,
)
# The compiled IR carries no category metadata, so the local-id convention is the
# observation signal: `claim_O*` are Step 2-discovered observations and `claim_E*`
# imported experimental claims (the single-paper pipeline treats experimental
# claims as first-class observations).
_OBSERVATION_ID_RE = re.compile(r"^claim_(?:O|E)(?=$|\d|_)")
# The concise scheme: one letter per node kind, numbered per kind.
LABEL_LETTERS: dict[str, str] = {
    "domain_conclusion": "K",
    "package_conclusion": "C",
    "premise": "A",
    "observation": "O",
    "paper_claim": "P",
}
LABEL_LEGEND: dict[str, str] = {
    "K": "domain conclusion of the summary tree (·L<m> is its summary level)",
    "C": "conclusion owned by an input Package (cluster context)",
    "A": "summarized observation premise / assertion",
    "O": "observation claim imported from a paper",
    "P": "paper claim",
}


def _is_helper(knowledge_id: str) -> bool:
    return bool(_HELPER_RE.search(str(knowledge_id)))


def _is_observation_qid(knowledge_id: str) -> bool:
    local = str(knowledge_id).split("::")[-1]
    return bool(_OBSERVATION_ID_RE.match(local))


def _node_kind_for_paper_claim(knowledge_id: str, provenance: list[str]) -> str:
    """Classify a referenced paper claim as `O` (observation) or `P` (claim).

    The compiled IR carries no category metadata, so the local-id convention
    (`claim_O*` / `claim_E*`) is the primary signal.  A claim the integration
    re-exports keeps a ``package_provenance`` naming *its own* Package: when that
    Package is an integration namespace, the record is an integration-owned
    conclusion or observation premise, so it stays an observation even if the
    local id lost the `O`.
    """
    if _is_observation_qid(knowledge_id):
        return "observation"
    if provenance and all(str(item).startswith("integration:") for item in provenance):
        return "observation"
    return "paper_claim"


def _is_k_conclusion(node: JSONDict) -> bool:
    """A domain conclusion node: `candidate_K_*` backing a `K<n>` label."""
    details = node.get("details") if isinstance(node.get("details"), Mapping) else {}
    qid = str(details.get("qid") or node.get("id") or "")
    return "candidate_K" in qid


def _read_json(store: RunStore, ref: ArtifactRef) -> JSONDict:
    with store.artifact_path(ref).open("r", encoding="utf-8") as handle:
        return json.load(handle)


class _LabelScheme:
    """Per-kind running counters that turn a node kind into `K3·L2` / `P17`.

    The `K` counter is reserved for the top-level domain conclusions of the
    summary tree; a conclusion owned by an input Package is `C`, so the central
    graph shows a handful of `K` bubbles instead of one per cluster.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self.by_kind: dict[str, int] = {}

    def label(self, kind: str) -> str:
        """Reserve the next number for a kind without emitting a node."""
        letter = LABEL_LETTERS.get(kind, "P")
        self._counters[letter] = self._counters.get(letter, 0) + 1
        self.by_kind[letter] = self.by_kind.get(letter, 0) + 1
        return f"{letter}{self._counters[letter]}"

    def next(self, kind: str, *, level: Any = None) -> str:
        base = self.label(kind)
        suffix = f"·L{level}" if base.startswith("K") and isinstance(level, int) and level > 0 else ""
        return f"{base}{suffix}"

    def legend(self) -> JSONDict:
        return {**LABEL_LEGEND, "by_kind": dict(sorted(self.by_kind.items()))}


class MergeViewAdapter:
    version = "6"  # v6 labels every node with the concise K/C/A/O/P scheme

    def project(
        self,
        store: RunStore,
        artifacts: list[ArtifactRef],
        options: Mapping[str, Any] | None = None,
    ) -> ViewDocument:
        # 1. Collect the public Paper knowledge (helpers excluded).
        paper_payloads: list[JSONDict] = []
        known: dict[str, JSONDict] = {}
        for ref in artifacts:
            if ref.kind != "gaia.ir" or ref.producer_stage != "input":
                continue
            payload = _read_json(store, ref)
            paper_payloads.append(payload)
            for knowledge in payload.get("knowledges") or []:
                if not isinstance(knowledge, Mapping):
                    continue
                qid, content = knowledge.get("id"), knowledge.get("content")
                if not isinstance(qid, str) or not qid or _is_helper(qid):
                    continue
                known[qid] = {"content": content or "", "type": knowledge.get("type", "claim"),
                              "provenance": list((knowledge.get("metadata") or {}).get("package_provenance") or [])
                              if isinstance(knowledge.get("metadata"), Mapping) else []}

        # 2. Pre-read the integration delta so its touched claims can be routed
        #    to the delta layer (the viewer filters nodes by layer).
        integration_doc: JSONDict | None = None
        for ref in artifacts:
            if ref.kind == "formalization" and ref.producer_stage == _INTEGRATION_STAGE:
                integration_doc = _read_json(store, ref)
        compiled_integration_ref = None
        if integration_doc is None:
            # Step 6 (and a fork from Step 5) receives only the compiler's
            # checkpoint, not Step 4's authoring snapshot. The compiled IR is
            # sufficient to recover the same nodes and named strategies.
            compiled_refs = [ref for ref in artifacts if ref.kind == "gaia.ir"
                             and ref.producer_stage == "step5_compile_gaia_ir"]
            if len(compiled_refs) > 1:
                raise ValueError("question graph requires one compiled integration")
            if compiled_refs:
                compiled_integration_ref = compiled_refs[0]
                compiled = _read_json(store, compiled_integration_ref)
                public = [item for item in compiled.get("knowledges", [])
                          if not _is_helper(item["id"])
                          and (item.get("metadata") or {}).get("visibility") != "formal_internal"]
                bindings = {item["id"]: (item.get("metadata") or {}).get("source_knowledge_id", item["id"])
                            for item in public}
                integration_doc = {
                    "knowledges": {
                        bindings[item["id"]]: {
                            "type": item["type"], "content": {"canonical": item.get("content")},
                            "metadata": dict(item.get("metadata") or {}),
                        } for item in public
                    },
                    "graph": {
                        "operators": [
                            {**item, "type": item.get("operator") or item.get("type"),
                             "variables": [bindings.get(qid, qid) for qid in item.get("variables", [])]}
                            for item in compiled.get("operators", [])
                        ],
                        "strategies": [
                            {**item, "premises": [bindings.get(qid, qid) for qid in item.get("premises", [])],
                             "conclusion": bindings.get(item.get("conclusion"), item.get("conclusion"))}
                            for item in compiled.get("strategies", [])
                        ],
                    },
                }
        # Step 3's proposals retain the reasoning type/expression of each
        # cross-paper weakpoint.  Step 4 can compile only the Gaia strategy
        # types; special_case/reproduces/bounds are kept as typed display links
        # instead of being silently dropped.
        step3_weakpoints: list[Mapping[str, Any]] = []
        for ref in artifacts:
            if ref.kind != _STEP3_KIND:
                continue
            payload = _read_json(store, ref)
            step3_weakpoints.extend(
                item for item in payload.get("weakpoints") or [] if isinstance(item, Mapping)
            )
        weakpoint_by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
        for weakpoint in step3_weakpoints:
            payload = weakpoint.get("payload") or {}
            for evidence in payload.get("evidence_claim_ids") or []:
                for target in payload.get("target_claim_id") or []:
                    weakpoint_by_pair[(str(evidence), str(target))] = payload
        delta_qids: set[str] = set()
        k_ids: list[str] = []
        if integration_doc is not None:
            graph = integration_doc.get("graph", {})
            for operator in graph.get("operators", []):
                delta_qids.update(str(v) for v in operator.get("variables") or [])
                if isinstance(operator.get("conclusion"), str):
                    delta_qids.add(operator["conclusion"])
            for strategy in graph.get("strategies", []):
                delta_qids.update(str(p) for p in strategy.get("premises") or [])
                if isinstance(strategy.get("conclusion"), str):
                    delta_qids.add(strategy["conclusion"])
            for knowledge_id in (integration_doc.get("knowledges") or {}):
                if isinstance(knowledge_id, str) and knowledge_id not in known and not _is_helper(knowledge_id):
                    k_ids.append(knowledge_id)

        nodes: list[JSONDict] = []
        edges: list[JSONDict] = []
        search_documents: list[JSONDict] = []
        source_artifacts: list[str] = []
        if compiled_integration_ref is not None:
            source_artifacts.append(compiled_integration_ref.artifact_id)
        emitted: set[str] = set()
        scheme = _LabelScheme()
        input_packages = {
            f"{payload.get('namespace')}:{payload.get('package_name')}" for payload in paper_payloads
        }

        def add_node(node_id: str, node_kind: str, content: str, kind: str, layer: str,
                     qid: str, *, original: str = "", level: Any = None,
                     metadata: Mapping[str, Any] | None = None) -> str:
            if node_id in emitted:
                return node_id
            emitted.add(node_id)
            label = scheme.next(node_kind, level=level)
            nodes.append({
                "id": node_id,
                "entity_id": label,
                "label": label,
                "display_label": label,
                "display_meta": kind,
                "kind": kind,
                "layer": layer,
                "step": _STEP,
                "visible_at": ["overview", "standard"],
                "min_granularity": "overview",
                "summary": content,
                "source_anchor_ids": [],
                "details": {"id": qid, "qid": qid, "type": kind, "content": content,
                            "original_label": original or qid,
                            "metadata": dict(metadata or {}),
                            "integration_owned": layer == _DELTA_LAYER and qid not in known},
            })
            search_documents.append({
                "id": f"search:{qid}", "title": label, "text": content,
                "tags": [kind, layer], "refs": [node_id], "metadata": {"step": _STEP},
            })
            return node_id

        def knowledge_node(knowledge_id: str, content: str, kind: str) -> str | None:
            if knowledge_id not in known:
                return None
            record = known[knowledge_id]
            layer = _DELTA_LAYER if knowledge_id in delta_qids else _CONTEXT_LAYER
            provenance = [str(item) for item in record.get("provenance") or []]
            node_kind = _node_kind_for_paper_claim(knowledge_id, provenance)
            return add_node(f"knowledge:{knowledge_id}", node_kind,
                            content, kind, layer, knowledge_id,
                            original=knowledge_id.split("::")[-1],
                            metadata={"package_provenance": provenance})

        def relation_edge(source: str, target: str, label: str, layer: str,
                          *, reasoning_type: str | None = None,
                          expression: str = "") -> None:
            if f"knowledge:{source}" not in emitted or f"knowledge:{target}" not in emitted:
                return  # endpoint is a helper / unknown node: drop the edge
            kind = reasoning_type or label
            chainable = kind == "deduction"
            edges.append({
                "id": f"edge:{len(edges)}:{source}:{target}:{label}:{layer}",
                "source": f"knowledge:{source}",
                "target": f"knowledge:{target}",
                "label": label,
                "layer": layer,
                "edge_class": "reasoning",
                "semantic_type": label,
                "reasoning_type": kind,
                "residual_reason": None,
                "chainable": chainable,
                "role_reason": expression,
                "reasoning_state": "confirmed",
                "step": _STEP,
                "visible_at": ["overview", "standard"],
                "min_granularity": "overview",
                "details": {"source": source, "target": target, "relation": label,
                            "reasoning_type": kind, "chainable": chainable,
                            "role_reason": expression},
            })

        # 3. Paper claims + package-internal reasoning.
        for ref in artifacts:
            if ref.kind != "gaia.ir" or ref.producer_stage != "input":
                continue
            source_artifacts.append(ref.artifact_id)
            payload = _read_json(store, ref)
            package_name = payload.get("package_name")
            for qid, record in known.items():
                if f":{package_name}::" not in qid:
                    continue
                knowledge_node(qid, record["content"], record["type"])
            for operator in payload.get("operators") or []:
                variables = [str(v) for v in operator.get("variables") or []]
                op_type = str(operator.get("operator") or operator.get("type") or "")
                if op_type in {"equivalence", "implication"} and len(variables) >= 2:
                    relation_edge(variables[0], variables[1], op_type, _PAPER_EDGE_LAYER)
            for strategy in payload.get("strategies") or []:
                conclusion = strategy.get("conclusion")
                premises = [str(p) for p in strategy.get("premises") or []]
                if not isinstance(conclusion, str) or not premises:
                    continue
                strategy_type = str(strategy.get("type") or "infer")
                for premise in premises:
                    relation_edge(premise, conclusion, strategy_type, _PAPER_EDGE_LAYER,
                                  reasoning_type=strategy_type)

        # 4. Integration delta: candidate nodes + new cross-paper edges.
        type_conflicts: list[JSONDict] = []
        arm_conflicts: list[JSONDict] = []
        if integration_doc is not None:
            for ref in artifacts:
                if ref.kind == "formalization" and ref.producer_stage == _INTEGRATION_STAGE:
                    source_artifacts.append(ref.artifact_id)
            for knowledge_id in k_ids:
                knowledge = (integration_doc.get("knowledges") or {}).get(knowledge_id) or {}
                content = (knowledge.get("content") or {}).get("canonical", "") if isinstance(knowledge.get("content"), Mapping) else ""
                metadata = knowledge.get("metadata") if isinstance(knowledge.get("metadata"), Mapping) else {}
                level = metadata.get("conclusion_level")
                # `candidate_A_*` is the summarized observation premise of an
                # abduction star, `candidate_K_*` a conclusion.  Only a
                # conclusion the summary tree tagged with a level is a domain
                # conclusion (`K<n>·L<m>`); an untagged one is the conclusion an
                # input Package (for example a cluster run) already owned, so it
                # stays context (`C<n>`) instead of posing as a domain node.
                if knowledge_id.split("::")[-1].startswith("candidate_A_"):
                    node_kind = "premise"
                elif isinstance(level, int) and level > 0:
                    node_kind = "domain_conclusion"
                else:
                    node_kind = "package_conclusion"
                add_node(f"knowledge:{knowledge_id}", node_kind, content,
                         knowledge.get("type", "claim"), _DELTA_LAYER, knowledge_id,
                         original=knowledge_id.split("::")[-1], level=level, metadata=metadata)
            # An operand set the gate typed more than once has no decided relation,
            # so it is not drawn at all.  This also protects a stale artifact that
            # was produced before Step 3/4 reconciled the types: rendering it must
            # not turn two conflicting verdicts into two parallel edges with
            # mutually exclusive `semantic_type` values.
            integration_operators, type_conflicts = reconcile_operator_types(
                integration_doc.get("graph", {}).get("operators", []))
            arms: list[tuple[str, str, str]] = []
            for operator in integration_operators:
                variables = [str(v) for v in operator.get("variables") or []]
                op_type = str(operator.get("type") or "")
                if len(variables) < 2 or not op_type:
                    continue  # negation has a single operand: no pairwise edge
                # Every operator type is projected (a bare equivalence/implication
                # filter left conjunction/contradiction operands with no edge at
                # all, so they looked floating).  A k-ary operator is drawn as a
                # star from its first operand, the only shape this viewer has.
                for operand in variables[1:]:
                    arms.append((variables[0], operand, op_type))
            # The star rendering collapses a k-ary operand set onto pairwise arms,
            # so two *different* operand sets can land on one drawn pair with
            # mutually exclusive labels; that pair is dropped for the same reason.
            arms, arm_conflicts = reconcile_rendered_arms(arms)
            for source, target, label in arms:
                relation_edge(source, target, label, _DELTA_LAYER)
            for strategy in integration_doc.get("graph", {}).get("strategies", []):
                conclusion = strategy.get("conclusion")
                premises = [str(p) for p in strategy.get("premises") or []]
                if not isinstance(conclusion, str) or not premises:
                    continue
                strategy_type = str(strategy.get("type") or "infer")
                for premise in premises:
                    relation_edge(premise, conclusion, strategy_type, _DELTA_LAYER,
                                  reasoning_type=strategy_type)
            # Display-only Step 3 types that Gaia cannot compile as strategies.
            seen_pairs = {
                (edge["source"].replace("knowledge:", ""), edge["target"].replace("knowledge:", ""),
                 edge.get("semantic_type"))
                for edge in edges
            }
            for weakpoint in step3_weakpoints:
                payload = weakpoint.get("payload") or {}
                reasoning_type = str(payload.get("reasoning_type") or "")
                if reasoning_type not in {"special_case", "reproduces", "bounds"}:
                    continue
                expression = str(payload.get("expression") or "")
                for evidence in payload.get("evidence_claim_ids") or []:
                    for target in payload.get("target_claim_id") or []:
                        key = (str(evidence), str(target), reasoning_type)
                        if key in seen_pairs:
                            continue
                        relation_edge(str(evidence), str(target), reasoning_type, _DELTA_LAYER,
                                      reasoning_type=reasoning_type, expression=expression)
                        seen_pairs.add(key)

        # Use Step 3's own type/expression for compiled integration edges.  This
        # is what keeps an `infer` from being treated as a support step merely
        # because Step 4's strategy schema has no finer type.
        for edge in edges:
            if edge.get("layer") != _DELTA_LAYER:
                continue
            source = edge["source"].replace("knowledge:", "")
            target = edge["target"].replace("knowledge:", "")
            payload = weakpoint_by_pair.get((source, target))
            if payload is None:
                continue
            reasoning_type = str(payload.get("reasoning_type") or edge.get("reasoning_type") or "infer")
            residual_reason = payload.get("residual_reason")
            chainable = bool(payload.get("chainable")) or (
                reasoning_type == "deduction" and str(payload.get("residual_reason") or "") == ""
            )
            edge["reasoning_type"] = reasoning_type
            edge["residual_reason"] = residual_reason
            edge["chainable"] = chainable
            edge["role_reason"] = str(payload.get("expression") or "")
            details = dict(edge.get("details") or {})
            details.update({
                "reasoning_type": reasoning_type,
                "residual_reason": residual_reason,
                "chainable": chainable,
                "role_reason": edge["role_reason"],
            })
            edge["details"] = details

        # 5. Drop isolated K conclusions.
        # A `candidate_K_*` conclusion that no edge touches is either context the
        # integration never used or a conclusion nothing linked to; projecting it
        # only scatters floating bubbles over the graph.
        connected = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
        dropped = {
            node["id"] for node in nodes
            if node["id"] not in connected and _is_k_conclusion(node)
        }
        if dropped:
            nodes = [node for node in nodes if node["id"] not in dropped]
            search_documents = [
                doc for doc in search_documents
                if not any(ref in dropped for ref in doc.get("refs") or [])
            ]

        return ViewDocument(
            title="Domain graph · Papers + Integration",
            source_artifacts=source_artifacts,
            nodes=nodes,
            edges=edges,
            search_documents=search_documents,
            layers=[
                {"id": _CONTEXT_LAYER, "label": "Paper knowledge (context)", "default_visible": True},
                {"id": _PAPER_EDGE_LAYER, "label": "Paper reasoning", "default_visible": True},
                {"id": _DELTA_LAYER, "label": "★ Integration delta", "default_visible": True},
            ],
            metadata={
                "latest_step": _STEP,
                "merge": True,
                "reasoning_type_counts": dict(sorted(Counter(
                    str(edge.get("reasoning_type") or "unknown") for edge in edges
                ).items())),
                "chainable_edge_count": sum(1 for edge in edges if edge.get("chainable")),
                "delta_node_count": sum(1 for node in nodes if node["layer"] == _DELTA_LAYER),
                "integration_edge_count": sum(1 for edge in edges if edge["layer"] == _DELTA_LAYER),
                "unused_context_conclusions_dropped": len(dropped),
                "operator_type_conflict_count": len(type_conflicts),
                "operator_type_conflicts": type_conflicts[:20],
                "rendered_edge_conflict_count": len(arm_conflicts),
                "rendered_edge_conflicts": arm_conflicts[:20],
                "label_scheme": scheme.legend(),
            },
            stages=[{"number": _STEP, "name": "integration", "validation_status": "passed"}],
        )


__all__ = ["MergeViewAdapter"]
