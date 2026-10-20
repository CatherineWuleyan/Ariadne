from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter

from ..models import JSONDict
from ..plugins import StageContext, StageResult
from .runtime import (
    emit_snapshot,
    imported_value,
    inherit_snapshot,
    invoke_optional_tool,
    knowledge,
    new_snapshot,
    slug,
    source_anchor,
)


_TAG_PATTERN = re.compile(r"^\s*\[#([A-Za-z0-9_.-]+)\]")
_IMAGE_PATTERN = re.compile(r"!\[[^]]*\]\(([^)]+)\)")
_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]{2,}")
_STOP_TOKENS = frozenset({"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was", "were", "with"})
_EXPRESSION_TOKEN_PATTERN = re.compile(r"\s*(\[([^\]]+)\]|不可同时成立|例子或证据|推出|等价|矛盾|且|和|与|或|非|\(|\))")


def _relation_reference_ids(relation: JSONDict) -> list[str]:
    """Return the claim identifiers named by either supported relation shape."""
    values = relation.get("related_assertions", relation.get("connects", []))
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def _expression_tokens(expression: str, references: dict[str, str]) -> list[tuple[str, str | None]]:
    """Tokenize the fixed relation language; reject all free-form syntax."""
    tokens: list[tuple[str, str | None]] = []
    position = 0
    while position < len(expression):
        match = _EXPRESSION_TOKEN_PATTERN.match(expression, position)
        if match is None:
            if expression[position:].strip():
                raise ValueError(f"unsupported relation expression near {expression[position:]!r}")
            break
        token = match.group(1)
        position = match.end()
        if token.startswith("["):
            reference = match.group(2).strip()
            claim_id = references.get(reference)
            if claim_id is None:
                raise ValueError(f"relation expression references unknown claim [{reference}]")
            tokens.append(("claim", claim_id))
            continue
        tokens.append(({"且": "and", "和": "and", "与": "and", "或": "or", "非": "not", "等价": "equivalence", "矛盾": "contradiction", "不可同时成立": "contradiction", "推出": "implies", "(": "(", ")": ")"}[token], None))
    return tokens


def _parse_expression(tokens: list[tuple[str, str | None]]) -> JSONDict:
    """Parse only the documented deterministic expression grammar into a small AST."""
    index = 0

    def parse_primary() -> JSONDict:
        nonlocal index
        if index >= len(tokens):
            raise ValueError("relation expression ends unexpectedly")
        kind, value = tokens[index]
        if kind == "claim":
            index += 1
            return {"kind": "claim", "id": value}
        if kind == "not":
            index += 1
            return {"kind": "negation", "operand": parse_primary()}
        if kind == "(":
            index += 1
            value = parse_binary(0)
            if index >= len(tokens) or tokens[index][0] != ")":
                raise ValueError("relation expression has unbalanced parentheses")
            index += 1
            return value
        raise ValueError(f"expected claim, 非, or (, found {kind}")

    precedence = {"and": 30, "or": 20, "equivalence": 10, "contradiction": 10, "implies": 5}
    operator_types = {"and": "conjunction", "or": "disjunction", "equivalence": "equivalence", "contradiction": "contradiction", "implies": "implies"}

    def parse_binary(minimum: int) -> JSONDict:
        nonlocal index
        left = parse_primary()
        while index < len(tokens):
            token, _ = tokens[index]
            priority = precedence.get(token)
            if priority is None or priority < minimum:
                break
            index += 1
            right = parse_binary(priority + 1)
            left = {"kind": operator_types[token], "left": left, "right": right}
        return left

    result = parse_binary(0)
    if index != len(tokens):
        raise ValueError("relation expression has trailing tokens")
    return result


def _ast_claim_ids(node: JSONDict) -> list[str]:
    if node["kind"] == "claim":
        return [node["id"]]
    if node["kind"] == "negation":
        return _ast_claim_ids(node["operand"])
    return [*_ast_claim_ids(node["left"]), *_ast_claim_ids(node["right"])]


def _paper_paragraphs(lines: list[str]) -> list[JSONDict]:
    """Return tagged paragraphs, preserving source lines for provenance."""
    paragraphs: list[JSONDict] = []
    buffer: list[str] = []
    start_line: int | None = None
    tag: str | None = None
    used_tags: set[str] = set()

    def flush(end_line: int) -> None:
        nonlocal buffer, start_line, tag
        if start_line is None or not buffer:
            buffer, start_line, tag = [], None, None
            return
        paragraph_tag = tag or f"p{len(paragraphs) + 1:04d}"
        if paragraph_tag in used_tags:
            raise ValueError(f"paper text contains duplicate anchor label: {paragraph_tag}")
        used_tags.add(paragraph_tag)
        text = "\n".join(buffer).strip()
        paragraphs.append({
            "tag": paragraph_tag,
            "anchor_id": f"anchor_paragraph_{paragraph_tag}",
            "start_line": start_line,
            "end_line": end_line,
            "text": text,
        })
        buffer, start_line, tag = [], None, None

    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            flush(line_number - 1)
            continue
        match = _TAG_PATTERN.match(line)
        if match and buffer:
            flush(line_number - 1)
        if start_line is None:
            start_line = line_number
        if match:
            tag = match.group(1)
            line = line[match.end():]
        buffer.append(line)
    flush(len(lines))
    return paragraphs


def _tokens(value: str) -> list[str]:
    return [token.lower() for token in _TOKEN_PATTERN.findall(value) if token.lower() not in _STOP_TOKENS]


def _cosine(left: list[str], right: list[str]) -> float:
    left_counts, right_counts = Counter(left), Counter(right)
    overlap = sum(left_counts[token] * right_counts[token] for token in left_counts.keys() & right_counts.keys())
    if not overlap:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left_counts.values()))
    right_norm = math.sqrt(sum(value * value for value in right_counts.values()))
    return overlap / (left_norm * right_norm)


def _best_paragraph_anchor(content: str, paragraphs: list[JSONDict]) -> JSONDict | None:
    query = _tokens(content)
    if not query:
        return None
    scored = [(_cosine(query, _tokens(paragraph["text"])), index, paragraph) for index, paragraph in enumerate(paragraphs)]
    score, _, paragraph = max(scored, default=(0.0, 0, None), key=lambda value: (value[0], -value[1]))
    return paragraph if score > 0 else None


def _figure_number(value: str) -> str | None:
    match = re.search(r"(?:fig(?:ure)?[_. -]*)?(\d+[A-Za-z]?)", value, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _figure_aliases(label: str) -> set[str]:
    aliases = {re.sub(r"[^a-z0-9]", "", label.lower())}
    number = _figure_number(label)
    if number:
        aliases.update({f"fig{number}", f"figure{number}"})
    return {value for value in aliases if value}


def _experiment_candidates(context, paragraphs: list[JSONDict], anchors: dict[object, JSONDict]) -> list[JSONDict]:
    bundles = context.find_all("input.bundle")
    if bundles:
        with context.artifact_path(bundles[-1]).open("r", encoding="utf-8") as handle:
            figures = json.load(handle).get("figures", [])
    else:
        figures = [{
            "artifact_id": ref.artifact_id,
            "media_type": ref.media_type,
            "figure": ref.metadata.get("figure", ref.metadata.get("source_filename")),
            "sequence": ref.metadata.get("sequence"),
        } for ref in context.find_all("source.original_figure")]
    if not isinstance(figures, list):
        raise ValueError("input.bundle figures must be a list")
    figure_anchors: dict[str, list[str]] = {}
    for anchor_id, anchor in anchors.items():
        if isinstance(anchor_id, str) and anchor.get("source_kind") == "paper.figure" and isinstance(anchor.get("artifact_id"), str):
            figure_anchors.setdefault(anchor["artifact_id"], []).append(anchor_id)
    candidates: list[JSONDict] = []
    for figure in figures:
        if not isinstance(figure, dict) or not isinstance(figure.get("artifact_id"), str):
            continue
        label = str(figure.get("figure") or "")
        aliases = _figure_aliases(label)
        matches = [index for index, paragraph in enumerate(paragraphs) if any(alias in re.sub(r"[^a-z0-9]", "", paragraph["text"].lower()) for alias in aliases)]
        if not matches:
            continue
        context_indexes = sorted({neighbor for index in matches for neighbor in range(max(0, index - 1), min(len(paragraphs), index + 2))})
        candidates.append({
            "figure": {
                "artifact_id": figure["artifact_id"],
                "media_type": figure.get("media_type"),
                "label": label or None,
                "source_anchor_ids": sorted(figure_anchors.get(figure["artifact_id"], [])),
            },
            "paragraphs": [{
                "anchor_id": paragraphs[index]["anchor_id"],
                "tag": paragraphs[index]["tag"],
                "text": paragraphs[index]["text"],
            } for index in context_indexes],
        })
    return candidates


class Step1ExtractEvidencePlugin:
    def run(self, context: StageContext) -> StageResult:
        claims_ref, claims = imported_value(context, "claims.cleaned")
        graph_ref, graph = imported_value(context, "reasoning_graph.lkm_coarse")
        paper_ref, paper_text = imported_value(context, "paper.text")
        if not isinstance(claims, dict) or not isinstance(claims.get("assertions"), list) or not isinstance(claims.get("relations"), list):
            raise ValueError("claims.cleaned has invalid assertions/relations")
        papers = graph.get("data", {}).get("papers") if isinstance(graph, dict) else None
        if not isinstance(papers, list):
            raise ValueError("reasoning_graph.lkm_coarse has invalid data.papers")
        payload = new_snapshot(context)
        knowledge_items: list[JSONDict] = payload["knowledge"]
        anchors: list[JSONDict] = payload["source_anchors"]
        links: list[JSONDict] = payload["non_reasoning_links"]
        source_to_knowledge: dict[str, str] = {}

        paper_lines = paper_text.splitlines()
        anchors.append({
            "anchor_id": "anchor_paper_text",
            "artifact_id": paper_ref.metadata.get("source_artifact_id", paper_ref.artifact_id),
            "source_kind": "paper.text",
            "locator": {"type": "markdown_span", "start_line": 1, "end_line": len(paper_lines)},
            "relevance": "provenance",
        })
        bundle_ref = context.require_one("input.bundle")
        with context.artifact_path(bundle_ref).open("r", encoding="utf-8") as handle:
            figures = json.load(handle).get("figures", [])
        if not isinstance(figures, list):
            raise ValueError("input.bundle figures must be a list")
        labelled_figure_artifact_ids: set[str] = set()
        figure_by_number: dict[str, list[JSONDict]] = {}
        for figure in figures:
            label = figure.get("figure") if isinstance(figure, dict) else None
            match = re.search(r"(?:fig(?:ure)?\s*)?(\d+)", label or "", re.IGNORECASE)
            if match:
                figure_by_number.setdefault(match.group(1), []).append(figure)

        paragraphs = _paper_paragraphs(paper_lines)
        for paragraph in paragraphs:
            anchors.append({
                "anchor_id": paragraph["anchor_id"],
                "artifact_id": paper_ref.metadata.get("source_artifact_id", paper_ref.artifact_id),
                "source_kind": "paper.text",
                "locator": {"type": "markdown_span", "start_line": paragraph["start_line"], "end_line": paragraph["end_line"]},
                "relevance": "paragraph",
            })
            image_match = _IMAGE_PATTERN.search(paragraph["text"])
            image_number = _figure_number(image_match.group(1)) if image_match is not None else None
            candidates = figure_by_number.get(image_number, []) if image_number else []
            if image_match is None or len(candidates) != 1:
                continue
            figure = candidates[0]
            labelled_figure_artifact_ids.add(figure["artifact_id"])
            figure_label = figure.get("figure") or f"Figure {image_number}"
            anchors.append({
                "anchor_id": f"anchor_label_{paragraph['tag']}", "artifact_id": figure["artifact_id"],
                "source_kind": "paper.figure",
                "locator": {"type": "figure_region", "figure": figure_label, "bbox": [0, 0, 1, 1]},
                "relevance": "labelled_figure",
            })

        for figure_index, figure in enumerate(figures, 1):
            if not isinstance(figure, dict) or not isinstance(figure.get("artifact_id"), str):
                raise ValueError(f"input.bundle figure {figure_index} is invalid")
            figure_label = figure.get("figure")
            if not isinstance(figure_label, str) or not figure_label:
                figure_label = f"Figure {figure_index}"
            if figure["artifact_id"] in labelled_figure_artifact_ids:
                continue
            anchors.append({
                "anchor_id": slug(figure.get("sequence", figure_index), "anchor_figure"),
                "artifact_id": figure["artifact_id"],
                "source_kind": "paper.figure",
                "locator": {"type": "figure_region", "figure": figure_label, "bbox": [0, 0, 1, 1]},
                "relevance": "provenance",
            })

        for index, assertion in enumerate(claims["assertions"]):
            if not isinstance(assertion, dict):
                continue
            external_id = assertion.get("id", f"assertion_{index}")
            knowledge_id = slug(external_id, "claim")
            anchor_id = slug(external_id, "anchor_clean")
            anchors.append(source_anchor(anchor_id, claims_ref.metadata.get("source_artifact_id", claims_ref.artifact_id), "claims.cleaned", f"/assertions/{index}"))
            text_en = assertion.get("text_en")
            if not isinstance(text_en, str) or not text_en.strip():
                raise ValueError(f"A assertion {external_id} requires non-empty text_en")
            item = knowledge(
                knowledge_id, text_en, anchor_ids=[anchor_id],
            )
            item["content"]["en"] = text_en
            knowledge_items.append(item)
            source_to_knowledge[str(external_id)] = knowledge_id
            source_to_knowledge[str(assertion.get("number", index + 1))] = knowledge_id

        permitted_node_kinds = {"weak_point"}
        for paper_index, paper_item in enumerate(papers):
            graph_value = paper_item.get("graph", {}) if isinstance(paper_item, dict) else {}
            for node_index, node in enumerate(graph_value.get("nodes", [])):
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("id", f"paper_{paper_index}_node_{node_index}"))
                node_kind = str(node.get("kind", node.get("type", "lkm_node")))
                if node_kind not in permitted_node_kinds:
                    continue
                knowledge_id = slug(node_id, "lkm")
                anchor_id = slug(f"{paper_index}_{node_index}", "anchor_lkm")
                anchors.append(source_anchor(anchor_id, graph_ref.metadata.get("source_artifact_id", graph_ref.artifact_id), "reasoning_graph.lkm_coarse", f"/data/papers/{paper_index}/graph/nodes/{node_index}"))
                node_content = str(node.get("content", ""))
                if node_kind == "reasoning_steps" and not node_content.strip():
                    steps = node.get("steps", [])
                    if isinstance(steps, list):
                        node_content = "\n".join(
                            step["reasoning"]
                            for step in steps
                            if isinstance(step, dict) and isinstance(step.get("reasoning"), str)
                        )
                if not node_content.strip():
                    raise ValueError(f"{node_kind} node {node_id} requires non-empty content")
                item = knowledge(knowledge_id, node_content, anchor_ids=[anchor_id])
                knowledge_items.append(item)

        for relation_index, relation in enumerate(claims["relations"]):
            if not isinstance(relation, dict):
                continue
            related = [source_to_knowledge[item] for item in _relation_reference_ids(relation) if item in source_to_knowledge]
            if len(related) < 2:
                raise ValueError(f"R relation {relation.get('id', relation_index)} must reference at least two known assertions")
            links.append({
                "id": slug(f"clean_{relation_index}", "link"), "link_type": "registry_relation",
                "source": related[0], "target": related[-1], "reasoning": False,
                "introduced_at_step": 1, "status": "active",
                "metadata": {
                    "source_system": "clean_claims", "relation_id": relation.get("id"),
                    "relation_type": relation.get("relation_type"), "inference_type": relation.get("inference_type"),
                    "description": relation.get("relation_description"),
                },
            })

        payload["changes"]["added"] = [item["id"] for item in knowledge_items]
        payload["review"] = {"status": "needs_review", "issues": []}
        failure, tool_artifacts = invoke_optional_tool(context, 1, payload)
        return failure or emit_snapshot(context, 1, payload, extra_artifacts=tool_artifacts)


class Step2NormalizeClaimsPlugin:
    """Semantic normalization boundary; deterministic source categories were imported in Step 1."""

    def run(self, context: StageContext) -> StageResult:
        _, payload = inherit_snapshot(context, 2)
        _, paper_text = imported_value(context, "paper.text")
        paper_lines = paper_text.splitlines()
        paragraphs = _paper_paragraphs(paper_lines)
        anchors = {item.get("anchor_id"): item for item in payload["source_anchors"] if isinstance(item, dict)}
        paragraph_anchor_ids = [item["anchor_id"] for item in paragraphs]
        modified: list[str] = []
        for item in payload["knowledge"]:
            content = item.get("content", {}).get("canonical", "")
            best = _best_paragraph_anchor(str(content), paragraphs)
            if best is None or best["anchor_id"] not in paragraph_anchor_ids:
                continue
            source_ids = item.setdefault("source_anchor_ids", [])
            if best["anchor_id"] not in source_ids:
                source_ids.append(best["anchor_id"])
                modified.append(item["id"])
        candidates = _experiment_candidates(context, paragraphs, anchors)
        payload["changes"]["modified"] = modified
        payload["review"] = {"status": "needs_review", "issues": [{"code": "CLAIM_COMPLETION_PENDING", "message": "Experiment S/A/B/M/R/U enrichment requires semantic review."}]}
        failure, tool_artifacts = invoke_optional_tool(context, 2, payload, extra_parameters={
            "experiment_candidates": candidates,
            "source_anchor_ids": paragraph_anchor_ids,
        })
        return failure or emit_snapshot(context, 2, payload, extra_artifacts=tool_artifacts)


class Step3AnalyzeReasoningPlugin:
    def run(self, context: StageContext) -> StageResult:
        _, payload = inherit_snapshot(context, 3)
        _, claims = imported_value(context, "claims.cleaned")
        assertions = claims.get("assertions", []) if isinstance(claims, dict) else []
        relations = claims.get("relations", []) if isinstance(claims, dict) else []
        if not isinstance(assertions, list) or not isinstance(relations, list):
            raise ValueError("claims.cleaned has invalid assertions/relations")
        claim_ids = {item["id"] for item in payload["knowledge"] if item.get("type") == "claim"}
        references: dict[str, str] = {}
        for index, assertion in enumerate(assertions, 1):
            if not isinstance(assertion, dict):
                continue
            source_id = str(assertion.get("id", f"assertion_{index}"))
            knowledge_id = slug(source_id, "claim")
            if knowledge_id not in claim_ids:
                continue
            references[source_id] = knowledge_id
            references[str(assertion.get("number", index))] = knowledge_id
        relation_by_id = {str(item.get("id", index)): item for index, item in enumerate(relations) if isinstance(item, dict)}
        helpers: list[JSONDict] = []
        operators: list[JSONDict] = []
        weakpoints: list[JSONDict] = []
        issues: list[JSONDict] = []
        existing_operator_ids = {item["id"] for item in payload["operators"]}

        def materialize(node: JSONDict, relation_id: str, expression: str, counter: list[int]) -> str:
            if node["kind"] == "claim":
                return node["id"]
            if node["kind"] == "implies":
                raise ValueError("推出 is a weakpoint direction, not an Operator")
            if node["kind"] == "negation":
                variables = [materialize(node["operand"], relation_id, expression, counter)]
            else:
                variables = [materialize(node["left"], relation_id, expression, counter), materialize(node["right"], relation_id, expression, counter)]
            sequence = counter[0]
            counter[0] += 1
            helper_id = slug(f"{relation_id}_{sequence}", "helper_expression")
            operator_id = slug(f"{relation_id}_{sequence}", "operator_expression")
            helpers.append(knowledge(
                helper_id, f"{node['kind']}({','.join(variables)})",
                first_seen_step=3, visibility="formal_internal",
                metadata={"source_relation_id": relation_id, "expression": expression, "ast_kind": node["kind"]},
            ))
            if operator_id not in existing_operator_ids:
                operators.append({"id": operator_id, "type": node["kind"], "variables": variables, "conclusion": helper_id,
                                  "visibility": "formal_internal", "metadata": {"source_relation_id": relation_id, "expression": expression}})
                existing_operator_ids.add(operator_id)
            return helper_id

        for link in payload["non_reasoning_links"]:
            metadata = link.get("metadata", {})
            relation_id = str(metadata.get("relation_id", ""))
            relation = relation_by_id.get(relation_id)
            if relation is None:
                issues.append({"code": "RELATION_SOURCE_MISSING", "message": "Cannot classify a relation absent from frozen clean claims.", "link_id": link.get("id")})
                continue
            expression = relation.get("expression")
            if not isinstance(expression, str) or not expression.strip():
                # Legacy clean-claims inputs have no expression. Their endpoints still form a weakpoint.
                expression = str(metadata.get("description") or metadata.get("relation_type") or "")
                ast = None
            elif "例子或证据" in expression:
                # This relation is intentionally a weakpoint, not an AST.  Its fixed
                # Chinese connective words are descriptive prose, not conjunctions.
                ast = None
            else:
                try:
                    ast = _parse_expression(_expression_tokens(expression, references))
                except ValueError as exc:
                    issues.append({"code": "RELATION_EXPRESSION_INVALID", "message": str(exc), "link_id": link.get("id"), "relation_id": relation_id})
                    continue
            is_evidence = "例子或证据" in expression
            implication = ast if ast and ast.get("kind") == "implies" else None
            if ast is not None:
                try:
                    # The direction itself is intentionally not materialized; all fixed logical subexpressions are.
                    if implication is not None:
                        materialize(implication["left"], relation_id, expression, [1])
                        materialize(implication["right"], relation_id, expression, [100])
                    elif not is_evidence:
                        materialize(ast, relation_id, expression, [1])
                except ValueError as exc:
                    issues.append({"code": "RELATION_EXPRESSION_INVALID", "message": str(exc), "link_id": link.get("id"), "relation_id": relation_id})
                    continue
            if implication is not None:
                evidence_ids = _ast_claim_ids(implication["left"])
                target_ids = _ast_claim_ids(implication["right"])
            elif is_evidence or ast is None:
                evidence_ids, target_ids = [link["source"]], [link["target"]]
            else:
                continue
            target_claim_id = link["target"] if link["target"] in target_ids else (target_ids[0] if len(target_ids) == 1 else None)
            if target_claim_id is None or not evidence_ids:
                issues.append({"code": "WEAKPOINT_ENDPOINT_AMBIGUOUS", "message": "A weakpoint requires evidence claims and exactly one target claim.", "link_id": link.get("id"), "relation_id": relation_id})
                continue
            evidence_anchor_ids = sorted({anchor_id for claim in payload["knowledge"] if claim.get("id") in evidence_ids for anchor_id in claim.get("source_anchor_ids", [])})
            weakpoints.append({
                "id": f"weakpoint_{relation_id}", "proposal_type": "weakpoint",
                "payload": {"evidence_claim_ids": evidence_ids, "target_claim_id": target_claim_id,
                            "reasoning_type": None, "evidence_anchor_ids": evidence_anchor_ids, "expression": expression},
                "supersedes": None, "review": None,
            })
        payload["knowledge"].extend(helpers)
        payload["operators"].extend(operators)
        payload["workflow_proposals"] = weakpoints
        removed_links = [item["id"] for item in payload["non_reasoning_links"]]
        payload["non_reasoning_links"] = []
        payload["changes"]["added"] = [*(item["id"] for item in helpers), *(item["id"] for item in operators), *(item["id"] for item in weakpoints)]
        payload["changes"]["removed"] = removed_links
        if weakpoints:
            issues.append({"code": "WEAKPOINT_REVIEW_PENDING", "message": "Mechanically identified weakpoints require Step 4 semantic review.", "weakpoint_ids": [item["id"] for item in weakpoints]})
        payload["review"] = {"status": "needs_review", "issues": issues}
        failure, tool_artifacts = invoke_optional_tool(context, 3, payload)
        return failure or emit_snapshot(context, 3, payload, extra_artifacts=tool_artifacts)


def _formalize_deduction(strategy: JSONDict, knowledge_items: list[JSONDict], operators: list[JSONDict]) -> tuple[list[str], list[str]]:
    strategy_id, premises, conclusion = strategy["id"], strategy["premises"], strategy["conclusion"]
    helper_ids: list[str] = []
    operator_ids: list[str] = []
    variables = list(premises)
    if len(premises) > 1:
        conjunction_id = slug(strategy_id, "helper_all_true")
        knowledge_items.append(knowledge(conjunction_id, f"all_true({','.join(premises)})", first_seen_step=4, metadata={"helper_kind": "conjunction_result", "owning_strategy_id": strategy_id}, visibility="formal_internal"))
        operator_id = slug(strategy_id, "operator_conjunction")
        operators.append({"id": operator_id, "type": "conjunction", "variables": premises, "conclusion": conjunction_id, "visibility": "formal_internal", "metadata": {"owning_strategy_id": strategy_id}})
        helper_ids.append(conjunction_id)
        operator_ids.append(operator_id)
        variables = [conjunction_id]
    implication_id = slug(strategy_id, "helper_implication")
    knowledge_items.append(knowledge(implication_id, f"implies({variables[0]},{conclusion})", first_seen_step=4, metadata={"helper_kind": "implication_result", "owning_strategy_id": strategy_id}, visibility="formal_internal"))
    operator_id = slug(strategy_id, "operator_implication")
    operators.append({"id": operator_id, "type": "implication", "variables": [variables[0], conclusion], "conclusion": implication_id, "visibility": "formal_internal", "metadata": {"owning_strategy_id": strategy_id}})
    helper_ids.append(implication_id)
    operator_ids.append(operator_id)
    return helper_ids, operator_ids


class Step4FormalizeReasoningPlugin:
    def run(self, context: StageContext) -> StageResult:
        _, payload = inherit_snapshot(context, 4)
        step3_reasoning = copy.deepcopy(payload["reasoning_units"])
        added: list[str] = []
        modified: list[str] = []
        for strategy in payload["reasoning_units"]:
            necessity_test = (strategy.get("coarse") or {}).get("necessity_test", {})
            if strategy.get("form") != "coarse" or strategy.get("type") != "deduction" or necessity_test.get("status") != "confirmed" or necessity_test.get("if_false_conclusion_must_fail") is not True:
                continue
            helper_ids, operator_ids = _formalize_deduction(strategy, payload["knowledge"], payload["operators"])
            strategy["form"] = "formal"
            strategy["formal"] = {"interface_claims": [*strategy["premises"], strategy["conclusion"]], "private_claims": helper_ids, "operator_ids": operator_ids}
            strategy["coarse"] = None
            strategy["review_status"] = "automatically_formalized"
            strategy["projection"] = {"summary_label": f"{' + '.join(strategy['premises'])} entails {strategy['conclusion']}", "fold_group": f"fold_{strategy['id']}"}
            added.extend([*helper_ids, *operator_ids])
            modified.append(strategy["id"])
        payload["changes"]["added"] = added
        payload["changes"]["modified"] = modified
        payload["formalization_id"] = f"formalization_{context.run_id}"
        payload["formalization_level"] = "fine" if all(item.get("form") == "formal" for item in payload["reasoning_units"]) else "mixed"
        payload["methodology"] = {"name": "05-formalization-methodology", "version": "1"}
        pending = [item["id"] for item in payload["reasoning_units"] if item.get("form") != "formal"]
        payload["review"] = {"status": "approved" if not pending else "needs_review", "issues": [] if not pending else [{"code": "UNEXPANDED_REASONING", "message": "Named or uncertain reasoning requires semantic expansion.", "reasoning_unit_ids": pending}]}
        failure, tool_artifacts = invoke_optional_tool(context, 4, payload)
        if failure is not None:
            return failure
        formalization_path = context.work_dir / "formalization.json"
        operator_by_id = {item["id"]: item for item in payload["operators"]}
        graph_knowledges: list[JSONDict] = []
        graph_knowledge_ids: set[str] = set()

        def authoring_operator(item: JSONDict, *, embedded: bool = False) -> JSONDict:
            operator_type = item["type"]
            variables = list(item["variables"])
            conclusion = item["conclusion"]
            if operator_type in {"implication", "equivalence"} and len(variables) == 1:
                relation_target = conclusion
                helper_id = f"helper_relation_{item['id']}"
                variables.append(relation_target)
                conclusion = helper_id
                if helper_id not in graph_knowledge_ids:
                    graph_knowledges.append({
                        "id": helper_id,
                        "type": "claim",
                        "format": "markdown",
                        "content": f"{operator_type}({variables[0]},{variables[1]})",
                        "source_anchor_ids": [],
                    })
                    graph_knowledge_ids.add(helper_id)
            converted: JSONDict = {
                "operator_id": item["id"],
                "scope": None if embedded else "local",
                "operator": operator_type,
                "variables": variables,
                "conclusion": conclusion,
            }
            if item.get("metadata"):
                converted["metadata"] = item["metadata"]
            return converted

        for item in payload["knowledge"]:
            knowledge_type = item["type"]
            if knowledge_type == "composition":
                knowledge_type = "claim"
            converted = {
                "id": item["id"],
                "type": knowledge_type,
                "format": "markdown",
                "content": item.get("content", {}).get("canonical", ""),
                "source_anchor_ids": item.get("source_anchor_ids", []),
            }
            if isinstance(item.get("observation"), dict):
                converted["observation"] = item["observation"]
            if isinstance(item.get("lineage"), dict):
                converted["lineage"] = item["lineage"]
            graph_knowledges.append(converted)
            graph_knowledge_ids.add(item["id"])

        graph_strategies: list[JSONDict] = []
        pending_proposals: list[JSONDict] = []
        formal_operator_ids: set[str] = set()
        for item in payload["reasoning_units"]:
            if item.get("form") != "formal":
                pending_proposals.append({
                    "id": f"proposal_formalize_{item['id']}",
                    "proposal_type": "formalization",
                    "payload": {
                        "strategy_id": item["id"],
                        "reasoning_type": item["type"],
                        "premises": item.get("premises", []),
                        "conclusion": item.get("conclusion"),
                        "background": item.get("background", []),
                    },
                    "supersedes": None,
                    "review": None,
                })
                continue
            operator_ids = list((item.get("formal") or {}).get("operator_ids", []))
            formal_operator_ids.update(operator_ids)
            strategy: JSONDict = {
                "strategy_id": item["id"],
                "scope": "local",
                "type": {"conflict": "elimination", "induction": "support"}.get(item["type"], item["type"]),
                "premises": item.get("premises", []),
                "conclusion": item.get("conclusion"),
                "background": item.get("background", []),
                "formal_expr": {
                    "operators": [authoring_operator(operator_by_id[operator_id], embedded=True) for operator_id in operator_ids]
                },
            }
            graph_strategies.append(strategy)

        relation_proposals: list[JSONDict] = []
        for link in payload["non_reasoning_links"]:
            decision = None
            if link.get("status") in {"active", "reviewed", "approved"}:
                decision = {"decision": "approved", "reviewer": "pipeline-migration", "rationale": "Accepted before authoring projection."}
            relation_proposals.append({
                "id": f"proposal_{link['id']}",
                "proposal_type": "relation",
                "payload": {
                    "source": link.get("source"),
                    "target": link.get("target"),
                    "relation_type": link.get("link_type"),
                },
                "supersedes": None,
                "review": decision,
            })

        final_reasoning = {item["id"]: item for item in payload["reasoning_units"]}
        weakpoint_proposals: list[JSONDict] = []
        gap_type_by_classification = {
            "weakpoint": "missing_premise",
            "scope_alignment": "scope_jump",
            "conditional_tension": "conditional_conflict",
        }
        reasoning_type_fallback = {"support": "induction", "infer": "induction", "conflict": "elimination"}
        for item in step3_reasoning:
            coarse = item.get("coarse") or {}
            classification = coarse.get("classification")
            if classification not in gap_type_by_classification:
                continue
            final_item = final_reasoning.get(item["id"], {})
            review = None
            if final_item.get("form") == "formal":
                review = {
                    "decision": "approved",
                    "reviewer": "step4-formalization",
                    "rationale": "The reviewed weakpoint was expanded into a formal strategy.",
                }
            weakpoint_proposals.append({
                "id": f"proposal_weakpoint_{item['id']}",
                "proposal_type": "weakpoint",
                "payload": {
                    "evidence_claim_ids": item.get("premises", []),
                    "constraint_claim_ids": item.get("background", []),
                    "target_claim_id": item.get("conclusion"),
                    "source_record_ids": [],
                    "reasoning_type": reasoning_type_fallback.get(item.get("type"), item.get("type")),
                    "gap_type": gap_type_by_classification[classification],
                    "rationale": (coarse.get("necessity_test") or {}).get("rationale", ""),
                },
                "supersedes": None,
                "review": review,
            })

        # Step 3 mechanically owns relation-derived weakpoints.  Keep them while
        # adding Step 4's semantic/formalization proposals, de-duplicated by ID.
        proposals_by_id = {
            item["id"]: item for item in payload.get("workflow_proposals", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for item in [*relation_proposals, *weakpoint_proposals, *pending_proposals]:
            proposals_by_id[item["id"]] = item
        payload["workflow_proposals"] = list(proposals_by_id.values())
        return emit_snapshot(context, 4, payload, extra_artifacts=tool_artifacts)
