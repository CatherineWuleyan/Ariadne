"""Read-only V2 projection; only graph members are rendered as graph nodes."""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from gaia.engine.ir.formalize import formalize_named_strategy

from pipeline_harness.models import ArtifactRef, JSONDict
from pipeline_harness.store import RunStore
from pipeline_harness.view.connectivity import prune_orphan_nodes
from pipeline_harness.view.model import ViewDocument

from .authoring import validate, weakpoint_target_ids


_INLINE_RELATIONS = {"implication", "equivalence"}


def _is_internal_helper(knowledge_id: str, knowledge: Mapping[str, Any]) -> bool:
    """Compiler-generated helper claims are layout plumbing, never public graph nodes."""
    value = str(knowledge_id)
    return bool(
        knowledge.get("metadata", {}).get("derived_ast_helper") is True
        or re.search(
            r"(^|::)__|helper[_-]?relation|operator[_-]?result|disjunction[_-]?result|"
            r"alternative[_-]?explanation|step4_weakpoint_",
            value,
            re.I,
        )
    )


# Authoring and the compiler both name helper claims by their canonical
# expression, e.g. ``conjunction(claim_O03,claim_O04)`` (Step 4) or
# ``all_true(...)`` / ``not_both_true(...)`` (compiler).  Operand Knowledge IDs
# are recoverable from that string, which is the only place a synthesized
# helper records what it is made of.
_HELPER_EXPRESSION = re.compile(
    r"^\s*(all_true|any_true|not_both_true|same_truth|opposite_truth|implies|"
    r"conjunction|disjunction|negation|equivalence|complement|implication)"
    r"\s*\((.+)\)\s*$",
    re.I,
)


def _helper_operands(content: Any, known_ids: set[str]) -> tuple[str, list[str]]:
    """Recover (operator_kind, operand Knowledge IDs) from a helper expression."""
    if not isinstance(content, str):
        return "", []
    match = _HELPER_EXPRESSION.match(content)
    if match is None:
        return "", []
    kind = match.group(1).lower()
    operands = [part.strip() for part in match.group(2).split(",")]
    return kind, [operand for operand in operands if operand in known_ids]


def _flatten_helper_operands(
    helper_expressions: Mapping[str, Any], known_ids: set[str]
) -> dict[str, tuple[str, list[str]]]:
    """Resolve synthesized helper claims to (kind, non-helper operand Knowledge IDs).

    Step 4 records a synthesized operand only as an expression string, e.g.
    ``conjunction(claim_O03,claim_O04)`` for the first operand of a
    contradiction, and folds long conjunctions into nested helpers
    (``conjunction(helper_relation_1_1,claim_16)``).  Returning non-helper
    operands keeps the viewer's helper contraction (``compactFormalGraph``,
    which rewires incoming x outgoing) independent of the order in which it
    happens to visit hidden nodes.
    """
    def resolve(entity: str, seen: frozenset[str]) -> tuple[str, list[str]]:
        if entity in seen:
            return "", []
        kind, operands = _helper_operands(helper_expressions.get(entity), known_ids)
        resolved: list[str] = []
        for operand in operands:
            if operand in helper_expressions:
                _, nested = resolve(operand, seen | {entity})
                resolved.extend(nested)
            else:
                resolved.append(operand)
        return kind, resolved

    return {entity: resolve(entity, frozenset()) for entity in helper_expressions}


def _display_claim_label(knowledge_id: str, article: str | None = None) -> str:
    """Keep stable IDs internally while making paper claim labels readable."""
    if "::" in knowledge_id:
        package, claim = knowledge_id.split("::", 1)
        article = package.split(":", 1)[-1]
        return f"{article}-{claim}"
    return f"{article}-{knowledge_id}" if article else knowledge_id


class V2FormalizationViewAdapter:
    version = "4"

    def project(self, store: RunStore, artifacts: list[ArtifactRef], options: Mapping[str, Any] | None = None) -> ViewDocument:
        formalizations = [ref for ref in artifacts if ref.kind == "formalization"]
        if not formalizations:
            return ViewDocument("Agent Pipeline V2", [], [], [], [], [{"id": "claims", "label": "Claims"}], {"latest_step": 1})
        latest_by_step: dict[int, ArtifactRef] = {}
        for ref in formalizations:
            latest_by_step[int(ref.metadata.get("step", 0))] = ref
        validation_by_step: dict[int, ArtifactRef] = {}
        for validation_ref in (ref for ref in artifacts if ref.kind == "formalization.validation"):
            validation_by_step[int(validation_ref.metadata.get("step", 0))] = validation_ref
        artifact_by_id = {item.artifact_id: item for item in artifacts}
        nodes: list[JSONDict] = []
        edges: list[JSONDict] = []
        search_documents: list[JSONDict] = []
        source_targets: list[JSONDict] = []
        stages: list[JSONDict] = []
        latest_document: JSONDict | None = None
        for step, ref in sorted(latest_by_step.items()):
            with store.artifact_path(ref).open("r", encoding="utf-8") as handle:
                document = json.load(handle)
            validate(document)
            latest_document = document
            article_name = str(document.get("package", {}).get("name") or "")
            graph_nodes = set(document["graph"]["nodes"])
            internal_helper_ids = {
                str(operator["conclusion"])
                for operator in document["graph"]["operators"]
                if isinstance(operator.get("conclusion"), str)
                and operator.get("metadata", {}).get("derived_ast_helper") is True
            }
            visible_ids = list(document["graph"]["nodes"])
            if step == 2:
                visible_ids.extend(
                    knowledge_id
                    for knowledge_id, knowledge in document["knowledges"].items()
                    if knowledge["type"] == "observation_proposal" and knowledge_id not in graph_nodes
                )
            visible_set = set(visible_ids)
            for knowledge_id in visible_ids:
                knowledge = document["knowledges"][knowledge_id]
                content = knowledge["content"]
                summary = content["canonical"] if content is not None else "Missing Knowledge content"
                node_id = f"step:{step}:knowledge:{knowledge_id}"
                # A name-based helper pattern must never hide an explicit graph
                # member: real Step 4 summary claims are named
                # ``claim_step4_weakpoint_*`` and hiding them silently drops the
                # abduction target edge.  Only derived-AST operator conclusions
                # stay internal among graph members.
                is_internal_helper = (
                    knowledge_id in internal_helper_ids
                    or (knowledge_id not in graph_nodes and _is_internal_helper(knowledge_id, knowledge))
                )
                nodes.append({
                    "id": node_id,
                    "entity_id": knowledge_id,
                    "label": _display_claim_label(knowledge_id, article_name),
                    "display_label": _display_claim_label(knowledge_id, article_name),
                    "display_meta": "formal internal helper" if is_internal_helper else knowledge["type"],
                    "kind": knowledge["type"],
                    "layer": "helpers" if is_internal_helper else "claims",
                    "step": step,
                    "visible_at": ["standard"] if is_internal_helper else ["overview", "standard"],
                    "min_granularity": "standard" if is_internal_helper else "overview",
                    "summary": summary,
                    "source_anchor_ids": knowledge.get("source_anchor_ids", []),
                    "details": {
                        **knowledge,
                        "graph_member": knowledge_id in graph_nodes,
                        "visibility": "formal_internal" if is_internal_helper else "public",
                        "derived_ast_helper": is_internal_helper,
                    },
                })
                if not is_internal_helper:
                    search_documents.append({
                        "id": f"search:{step}:{knowledge_id}",
                        "title": knowledge_id,
                        "text": summary,
                        "tags": [knowledge["type"], f"step-{step}"],
                        "refs": [node_id],
                        "metadata": {"step": step},
                    })
            for link in document["workflow"]["non_reasoning_links"]:
                if any(source not in visible_set for source in link["sources"]) or link["target"] not in visible_set:
                    continue
                relation_node_id = f"step:{step}:relation:{link['id']}"
                nodes.append({
                    "id": relation_node_id,
                    "entity_id": link["id"],
                    "label": link["id"],
                    "display_label": "关系",
                    "display_meta": "仅供显示",
                    "kind": "relation_display",
                    "layer": "registry",
                    "step": step,
                    "visible_at": ["overview", "standard"],
                    "min_granularity": "overview",
                    "summary": link.get("metadata", {}).get("relation", {}).get("expression", "Imported relation"),
                    "source_anchor_ids": [],
                    "details": {**link, "display_only": True},
                })
                for source in link["sources"]:
                    edges.append({
                        "id": f"link:{step}:{link['id']}:{source}:source",
                        "semantic_id": link["id"],
                        "source": f"step:{step}:knowledge:{source}",
                        "target": relation_node_id,
                        "label": link["link_type"],
                        "layer": "registry",
                        "edge_class": "non_reasoning",
                        "semantic_type": link["link_type"],
                        "step": step,
                        "visible_at": ["overview", "standard"],
                        "min_granularity": "overview",
                        "details": {**link, "display_only": True},
                    })
                edges.append({
                    "id": f"link:{step}:{link['id']}:target",
                    "semantic_id": link["id"],
                    "source": relation_node_id,
                    "target": f"step:{step}:knowledge:{link['target']}",
                    "label": link["link_type"],
                    "layer": "registry",
                    "edge_class": "non_reasoning",
                    "semantic_type": link["link_type"],
                    "step": step,
                    "visible_at": ["overview", "standard"],
                    "min_granularity": "overview",
                    "details": {**link, "display_only": True},
                })
            for weakpoint in document["workflow"]["weakpoints"]:
                payload = weakpoint["payload"]
                weakpoint_id = str(weakpoint["id"])
                targets = weakpoint_target_ids(payload)
                for target in targets:
                    visible_weakpoint_id = weakpoint_id if len(targets) == 1 else f"{weakpoint_id}__target_{target}"
                    visible_weakpoint = {
                        **weakpoint,
                        "payload": {**payload, "target_claim_id": target},
                    }
                    node_id = f"step:{step}:weakpoint:{visible_weakpoint_id}"
                    nodes.append({
                        "id": node_id, "entity_id": visible_weakpoint_id, "label": visible_weakpoint_id,
                        "display_label": "Weakpoint", "display_meta": "evidence / example",
                        "kind": "weakpoint", "layer": "weakpoints", "step": step,
                        "visible_at": ["overview", "standard"], "min_granularity": "overview",
                        "summary": payload["expression"], "source_anchor_ids": payload["evidence_anchor_ids"],
                        "details": visible_weakpoint,
                    })
                    for source in payload["evidence_claim_ids"]:
                        edges.append({
                            "id": f"weakpoint:{step}:{visible_weakpoint_id}:{source}:evidence", "semantic_id": weakpoint_id,
                            "source": f"step:{step}:knowledge:{source}", "target": node_id, "label": "evidence",
                            "layer": "weakpoints", "edge_class": "non_reasoning", "semantic_type": "weakpoint",
                            "step": step, "visible_at": ["overview", "standard"], "min_granularity": "overview",
                            "details": visible_weakpoint,
                        })
                    edges.append({
                        "id": f"weakpoint:{step}:{visible_weakpoint_id}:{target}:target", "semantic_id": weakpoint_id,
                        "source": node_id, "target": f"step:{step}:knowledge:{target}", "label": "target",
                        "layer": "weakpoints", "edge_class": "non_reasoning", "semantic_type": "weakpoint",
                        "step": step, "visible_at": ["overview", "standard"], "min_granularity": "overview",
                        "details": visible_weakpoint,
                    })
            for operator in document["graph"]["operators"]:
                # Binary conjunction helpers are compiler plumbing.  Showing
                # each nested helper as a separate caret makes a ternary
                # relation look duplicated; the weakpoint projection below
                # carries the complete source set and target instead.
                if operator.get("metadata", {}).get("derived_ast_helper") is True:
                    continue
                operator_id = str(operator["id"])
                if operator["type"] in _INLINE_RELATIONS:
                    left, right = operator["variables"]
                    edges.append({
                        "id": f"operator-edge:{step}:{operator_id}", "entity_id": operator_id,
                        "semantic_id": operator_id,
                        "source": f"step:{step}:knowledge:{left}",
                        "target": f"step:{step}:knowledge:{right}",
                        "label": operator["type"], "layer": "operators", "edge_class": "reasoning",
                        "semantic_type": operator["type"], "step": step,
                        "visible_at": ["standard"], "min_granularity": "standard", "details": operator,
                    })
                    continue
                node_id = f"step:{step}:operator:{operator_id}"
                nodes.append({
                    "id": node_id, "entity_id": operator_id, "label": operator["type"],
                    "display_label": operator["type"], "display_meta": "operator",
                    "kind": "operator", "layer": "operators", "step": step,
                    "visible_at": ["standard"], "min_granularity": "standard",
                    "summary": operator.get("metadata", {}).get("expression", operator["type"]),
                    "source_anchor_ids": [], "details": operator,
                })
                for source in operator["variables"]:
                    edges.append({
                        "id": f"operator:{step}:{operator_id}:{source}:input", "semantic_id": operator_id,
                        "source": f"step:{step}:knowledge:{source}", "target": node_id, "label": "input",
                        "layer": "operators", "edge_class": "reasoning", "semantic_type": operator["type"],
                        "step": step, "visible_at": ["standard"], "min_granularity": "standard", "details": operator,
                    })
                conclusion = operator.get("conclusion")
                if isinstance(conclusion, str):
                    edges.append({
                        "id": f"operator:{step}:{operator_id}:output", "semantic_id": operator_id,
                        "source": node_id, "target": f"step:{step}:knowledge:{conclusion}", "label": "output",
                        "layer": "operators", "edge_class": "reasoning", "semantic_type": operator["type"],
                        "step": step, "visible_at": ["standard"], "min_granularity": "standard", "details": operator,
                    })
            alternative_count = 0
            # The compiled snapshot may retain an AltExp premise as a
            # qualified Knowledge ID even when its named strategy is lowered
            # into operators. Ensure every such interface claim has a visible
            # placeholder node in the standard projection.
            for knowledge_id, knowledge in document["knowledges"].items():
                if "AltExp" not in str(knowledge_id) or f"step:{step}:knowledge:{knowledge_id}" in {node["id"] for node in nodes}:
                    continue
                nodes.append({
                    "id": f"step:{step}:knowledge:{knowledge_id}", "entity_id": "AltExp",
                    "label": "AltExp", "display_label": "AltExp",
                    "display_meta": "official alternative interface",
                    "kind": "alternative_placeholder", "layer": "claims", "step": step,
                    "visible_at": ["standard"], "min_granularity": "standard",
                    "summary": "Unknown alternative explanation (content=null)",
                    "source_anchor_ids": [], "fold_group": None,
                    "details": {"id": knowledge_id, "label": "AltExp", "type": "claim",
                                "content": None, "derived_for_view": True,
                                "visibility": "public"},
                })
            for strategy in document["graph"].get("strategies", []):
                strategy_id = strategy["strategy_id"]
                # Strategies are an authoring abstraction only. The viewer always
                # renders their lowered Operator form; no Strategy block is kept.
                # Gaia's named formalizer does not accept infer: infer is the
                # intentionally weak, evidence-backed edge emitted by Step 4
                # for relations whose stronger reasoning family is unknown.
                # Render it directly (the legacy sentinel remains readable).
                if strategy.get("type") in {"infer", "__legacy_infer_disabled__"}:
                    node_id = f"step:{step}:strategy:{strategy_id}"
                    nodes.append({
                        "id": node_id, "entity_id": strategy_id, "label": "infer",
                        "display_label": "infer", "display_meta": "strategy",
                        "kind": "strategy", "layer": "strategies", "step": step,
                        "visible_at": ["overview", "standard"], "min_granularity": "overview",
                        "summary": "infer", "source_anchor_ids": [], "details": strategy,
                    })
                    for source in strategy["premises"]:
                        edges.append({
                            "id": f"strategy:{step}:{strategy_id}:{source}:input", "semantic_id": strategy_id,
                            "source": f"step:{step}:knowledge:{source}", "target": node_id, "label": "premise",
                            "layer": "strategies", "edge_class": "reasoning", "semantic_type": "infer",
                            "step": step, "visible_at": ["overview", "standard"], "min_granularity": "overview", "details": strategy,
                        })
                    edges.append({
                        "id": f"strategy:{step}:{strategy_id}:output", "semantic_id": strategy_id,
                        "source": node_id, "target": f"step:{step}:knowledge:{strategy['conclusion']}", "label": "conclusion",
                        "layer": "strategies", "edge_class": "reasoning", "semantic_type": "infer",
                        "step": step, "visible_at": ["overview", "standard"], "min_granularity": "overview", "details": strategy,
                    })
                    search_ref = node_id
                else:
                    # AltExp is an interface premise (not a generated helper),
                    # so Gaia's formalizer leaves it in operator.variables.
                    # Materialize it explicitly for the viewer as a public
                    # red dashed placeholder node.
                    for premise in strategy["premises"]:
                        if "AltExp" in str(premise) and f"step:{step}:knowledge:{premise}" not in {node["id"] for node in nodes}:
                            nodes.append({
                                "id": f"step:{step}:knowledge:{premise}", "entity_id": "AltExp",
                                "label": "AltExp", "display_label": "AltExp",
                                "display_meta": "official alternative interface",
                                "kind": "alternative_placeholder", "layer": "claims", "step": step,
                                "visible_at": ["standard"], "min_granularity": "standard",
                                "summary": "Unknown alternative explanation (content=null)",
                                "source_anchor_ids": [], "fold_group": strategy_id,
                                "details": {"id": premise, "label": "AltExp", "type": "claim",
                                            "content": None, "derived_for_view": True,
                                            "strategy_id": strategy_id, "visibility": "public"},
                            })
                    lowered = formalize_named_strategy(
                        scope=strategy["scope"], type_=strategy["type"], premises=list(strategy["premises"]),
                        conclusion=strategy["conclusion"], namespace="viewer", package_name="display",
                        background=list(strategy["background"]),
                    )
                    for helper in lowered.knowledges:
                        helper_id = str(helper.id)
                        helper_metadata = dict(helper.metadata or {})
                        is_alternative = helper_metadata.get("interface_role") == "alternative_explanation"
                        if is_alternative:
                            alternative_count += 1
                        display_label = "AltExp" if is_alternative else (helper.label or helper_id)
                        display_entity_id = (
                            "AltExp" if alternative_count == 1 else f"AltExp_{alternative_count}"
                        ) if is_alternative else helper_id
                        nodes.append({
                            "id": f"step:{step}:knowledge:{helper_id}", "entity_id": display_entity_id,
                            "label": display_label, "display_label": display_label,
                            "display_meta": "official alternative interface" if is_alternative else "formal internal helper",
                            "kind": "alternative_placeholder" if is_alternative else "claim",
                            "layer": "claims" if is_alternative else "helpers", "step": step,
                            "visible_at": ["standard"], "min_granularity": "standard", "summary": str(helper.content or ""),
                            "source_anchor_ids": [], "fold_group": strategy_id,
                            "details": {
                                **helper.model_dump(mode="json"), "derived_for_view": True,
                                "strategy_id": strategy_id,
                                "visibility": "public" if is_alternative else "formal_internal",
                            },
                        })
                    formal_operators = list(lowered.strategy.formal_expr.operators)
                    # A clause whose helper conclusion is consumed by another
                    # operator must keep a real node so that operator is not
                    # left "headless".  A terminal clause (its helper conclusion
                    # feeds nothing) can be projected as a single double-headed
                    # arrow edge instead of an equivalence/implication node, so
                    # the viewer shows a plain bidirectional arrow rather than
                    # an equivalence symbol.
                    consumed_conclusions = {
                        str(operator.conclusion)
                        for operator in formal_operators
                        if operator.conclusion is not None
                    } & {
                        str(variable)
                        for operator in formal_operators
                        for variable in operator.variables
                    }
                    strategy_refs: list[str] = []
                    for index, operator in enumerate(formal_operators, 1):
                        operator_id = f"{strategy_id}:operator:{index}"
                        operator_type = operator.operator.value
                        operator_node_id = f"step:{step}:operator:{operator_id}"
                        details = {
                            "id": operator_id, "type": operator_type, "variables": list(operator.variables),
                            "conclusion": operator.conclusion,
                            "metadata": {"strategy_id": strategy_id, "background": list(strategy["background"]),
                                         "formalization_template": strategy["type"], "derived_for_view": True},
                        }
                        if (operator_type in _INLINE_RELATIONS and len(operator.variables) == 2
                                and (operator.conclusion is None
                                     or str(operator.conclusion) not in consumed_conclusions)):
                            left, right = (str(variable) for variable in operator.variables)
                            edges.append({
                                "id": f"formal:{step}:{operator_id}", "entity_id": operator_id,
                                "semantic_id": strategy_id,
                                "source": f"step:{step}:knowledge:{left}",
                                "target": f"step:{step}:knowledge:{right}",
                                "label": operator_type, "layer": "operators", "edge_class": "reasoning",
                                "semantic_type": operator_type, "step": step,
                                "visible_at": ["standard"], "min_granularity": "standard",
                                "fold_group": strategy_id, "details": details,
                            })
                            continue
                        nodes.append({
                            "id": operator_node_id, "entity_id": operator_id, "label": operator_type,
                            "display_label": operator_type, "display_meta": "operator", "kind": "operator",
                            "layer": "operators", "step": step, "visible_at": ["standard"],
                            "min_granularity": "standard", "summary": operator_type, "source_anchor_ids": [],
                            "fold_group": strategy_id, "details": details,
                        })
                        strategy_refs.append(operator_node_id)
                        for source in operator.variables:
                            edges.append({
                                "id": f"formal:{step}:{operator_id}:{source}:input", "semantic_id": strategy_id,
                                "source": f"step:{step}:knowledge:{source}", "target": operator_node_id, "label": "input",
                                "layer": "operators", "edge_class": "reasoning", "semantic_type": operator_type,
                                "step": step, "visible_at": ["standard"], "min_granularity": "standard",
                                "fold_group": strategy_id, "details": details,
                            })
                        edges.append({
                            "id": f"formal:{step}:{operator_id}:output", "semantic_id": strategy_id,
                            "source": operator_node_id, "target": f"step:{step}:knowledge:{operator.conclusion}", "label": "output",
                            "layer": "operators", "edge_class": "reasoning", "semantic_type": operator_type,
                            "step": step, "visible_at": ["standard"], "min_granularity": "standard",
                            "fold_group": strategy_id, "details": details,
                        })
                    search_ref = (
                        strategy_refs[0] if strategy_refs
                        else f"step:{step}:knowledge:{strategy['conclusion']}"
                    )
                search_documents.append({
                    "id": f"search:{step}:{strategy_id}", "title": strategy_id,
                    "text": " ".join([strategy["type"], *strategy["premises"], strategy["conclusion"],
                                      *(document["knowledges"][key]["content"]["canonical"] for key in strategy["background"])]),
                    "tags": ["strategy", strategy["type"], f"step-{step}"], "refs": [search_ref], "metadata": {"step": step},
                })
            for anchor in document["workflow"]["source_anchors"]:
                source = artifact_by_id.get(anchor["artifact_id"])
                source_targets.append({
                    "anchor_id": anchor["anchor_id"],
                    "step": step,
                    "artifact_id": anchor["artifact_id"],
                    "artifact_path": source.path if source else None,
                    "href": f"../{source.path}" if source else None,
                    "locator": anchor["locator"],
                    "source_kind": anchor["source_kind"],
                    "relevance": anchor.get("relevance"),
                })
            validation_ref = validation_by_step.get(step)
            stages.append({
                "number": step,
                "name": ref.metadata.get("step_name", f"step_{step}"),
                "artifact_id": ref.artifact_id,
                "revision_id": document["revision"]["revision_id"],
                "validation_status": validation_ref.metadata.get("validation_status", "unknown") if validation_ref else "unknown",
                "validation_errors": validation_ref.metadata.get("error_count", 0) if validation_ref else 0,
                "validation_warnings": validation_ref.metadata.get("warning_count", 0) if validation_ref else 0,
            })
            # A synthesized helper Knowledge node that no edge derives records
            # its operands only in its content expression.  Step 4 uses one as
            # the first operand of a contradiction -- ``([claim_O03] 和
            # [claim_O04]) 与 [claim_6] 不可同时成立`` becomes the operand
            # ``helper_relation_step2_2_1`` with content
            # ``conjunction(claim_O03,claim_O04)`` and no incoming edge.
            # The viewer hides helper nodes and rewires incoming x outgoing
            # (compactFormalGraph in viewer.html), so a helper with no incoming
            # edge loses its only outgoing edge with nothing replacing it and
            # the contradiction renders with a missing operand.  Recover the
            # operands as input edges so the contraction bridges them through.
            #
            # Authoring folds long conjunctions into nested helpers
            # (``conjunction(helper_relation_1_1,claim_16)``), so resolve the
            # chain down to non-helper Knowledge IDs.  Leaving helper-to-helper
            # edges would make the viewer's contraction depend on the order it
            # happens to visit hidden nodes in.
            derived_targets = {edge["target"] for edge in edges}
            step_knowledge_ids = {
                node["entity_id"]
                for node in nodes
                if node.get("step") == step and f"step:{step}:knowledge:" in node["id"]
            }
            helper_expressions: dict[str, str] = {}
            for node in nodes:
                if node.get("step") != step or node.get("layer") != "helpers":
                    continue
                content = (node.get("details") or {}).get("content")
                helper_expressions[node["entity_id"]] = (
                    content.get("canonical") if isinstance(content, dict) else content
                )
            flattened = _flatten_helper_operands(helper_expressions, step_knowledge_ids)

            for node in nodes:
                if node.get("step") != step or node.get("layer") != "helpers":
                    continue
                if node["id"] in derived_targets:
                    continue
                details = node.get("details") or {}
                operator_kind, operands = flattened.get(node["entity_id"], ("", []))
                for operand in operands:
                    edges.append({
                        "id": f"helper:{step}:{node['entity_id']}:{operand}:input",
                        "semantic_id": node["entity_id"],
                        "source": f"step:{step}:knowledge:{operand}",
                        "target": node["id"], "label": "input",
                        "layer": "operators", "edge_class": "reasoning",
                        "semantic_type": operator_kind or "conjunction",
                        "step": step, "visible_at": ["standard"], "min_granularity": "standard",
                        "details": details,
                    })
        # Reconnect each Step 3 weakpoint to the Step 4 operator group(s) that
        # formalize the same public interface. This is derived Viewer
        # provenance only; it is never written back to formalization.json.
        group_entities: dict[str, set[str]] = {}
        group_types: dict[str, str] = {}
        for node in nodes:
            if node.get("step") != 4 or not node.get("fold_group"):
                continue
            group = str(node["fold_group"])
            if node.get("kind") != "operator":
                group_entities.setdefault(group, set()).add(str(node.get("entity_id", "")))
            else:
                template = node.get("details", {}).get("metadata", {}).get("formalization_template")
                if isinstance(template, str):
                    group_types[group] = template
        node_by_id = {str(node["id"]): node for node in nodes}
        for edge in edges:
            if edge.get("step") != 4 or edge.get("layer") != "operators":
                continue
            edge_groups = edge.get("fold_groups") or ([edge["fold_group"]] if edge.get("fold_group") else [])
            for group_value in edge_groups:
                group = str(group_value)
                entities = group_entities.setdefault(group, set())
                for endpoint in (edge.get("source"), edge.get("target")):
                    endpoint_node = node_by_id.get(str(endpoint))
                    if endpoint_node is not None and endpoint_node.get("kind") != "operator":
                        entities.add(str(endpoint_node.get("entity_id", "")))

        groups = list(group_entities)
        neighbors = {group: set() for group in groups}
        for index, left in enumerate(groups):
            for right in groups[index + 1:]:
                if group_entities[left] & group_entities[right]:
                    neighbors[left].add(right)
                    neighbors[right].add(left)
        components: list[set[str]] = []
        unseen = set(groups)
        while unseen:
            seed = unseen.pop()
            component = {seed}
            frontier = [seed]
            while frontier:
                current = frontier.pop()
                fresh = neighbors[current] & unseen
                unseen -= fresh
                component |= fresh
                frontier.extend(fresh)
            components.append(component)

        for node in nodes:
            if node.get("step") != 3 or node.get("kind") != "weakpoint":
                continue
            payload = node.get("details", {}).get("payload", {})
            interface = {
                *map(str, payload.get("evidence_claim_ids", [])),
                *map(str, weakpoint_target_ids(payload)),
            }
            reasoning_type = payload.get("reasoning_type")
            direct = [
                {group}
                for group, entities in group_entities.items()
                if interface <= entities and (reasoning_type is None or group_types.get(group) == reasoning_type)
            ]
            if direct:
                smallest = min(len(group_entities[next(iter(match))] - interface) for match in direct)
                matches = [
                    match
                    for match in direct
                    if len(group_entities[next(iter(match))] - interface) == smallest
                ]
            else:
                matches = [
                    component
                    for component in components
                    if interface <= set().union(*(group_entities[group] for group in component))
                    and (reasoning_type is None or reasoning_type in {group_types.get(group) for group in component})
                ]
            if len(matches) == 1:
                node["details"] = {**node["details"], "expanded_strategy_ids": sorted(matches[0])}

        # Compiled snapshots can expose qualified AltExp IDs as ordinary claim
        # nodes. Reclassify them at the final projection boundary so the
        # viewer uses the dedicated red dashed placeholder shape.
        for node in nodes:
            if "AltExp" in str(node.get("entity_id", "")) or "AltExp" in str(node.get("id", "")):
                node["kind"] = "alternative_placeholder"
                node["layer"] = "claims"
                node["entity_id"] = "AltExp"
                node["label"] = node["display_label"] = "AltExp"
                node["summary"] = "Unknown alternative explanation (content=null)"
        assert latest_document is not None
        latest_step = max(latest_by_step)
        # Terminal formalizer helpers (for example the unused
        # __equivalence_result_<digest> of an abduction clause) and operators
        # that lost every operand must not leak isolated nodes into the graph.
        view, _connectivity = prune_orphan_nodes(ViewDocument(
            f"Agent Pipeline V2 · Step {latest_step}",
            [ref.artifact_id for _, ref in sorted(latest_by_step.items())],
            nodes,
            edges,
            search_documents,
            [
                {"id": "claims", "label": "Formal nodes"},
                {"id": "registry", "label": "Imported relations"},
                {"id": "weakpoints", "label": "Weakpoints"},
                {"id": "operators", "label": "Operators"},
                {"id": "strategies", "label": "Infer strategies"},
            ],
            {
                "latest_step": latest_step,
                "knowledge_count": len(latest_document["knowledges"]),
                "graph_node_count": len(latest_document["graph"]["nodes"]),
            },
            stages,
            source_targets,
        ))
        return view
