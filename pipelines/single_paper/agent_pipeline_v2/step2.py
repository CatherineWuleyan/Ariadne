"""Pipeline 7.0 Step 2: anchor and review experimental observations."""
from __future__ import annotations

import copy
import json
import os
import time
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pipeline_harness.domain.stages import (
    _cosine,
    _figure_aliases,
    _paper_paragraphs,
    _tokens,
)
from pipeline_harness.domain.tools import (
    DomainTool,
    ToolCallRequest,
    ToolCallResponse,
    validate_tool_response,
)
from pipeline_harness.models import Finding, JSONDict
from pipeline_harness.plugins import ArtifactDraft, StageContext, StageResult, instantiate
from pipeline_harness.store import atomic_write_json, read_json
from pipeline_harness.domain.vision import DeepSeekFlashVisionTool

from .authoring import content_hash, emit_formalization
from .step1 import INPUT_BUNDLE_KIND, PAPER_TEXT_KIND


STEP_NAME = "step2_complete_observations"
MODEL_NAME = "deepseek-v4-flash"
_API_RETRIES = 3
_CANDIDATE_CALL_SLOTS = 4
_MAX_PARALLEL_CANDIDATES = 4


def _urlopen_with_retry(request: Request, *, timeout: int = 180):
    """Retry transient DNS/transport failures without masking HTTP errors."""
    last_error = None
    for attempt in range(_API_RETRIES):
        try:
            return urlopen(request, timeout=timeout)  # noqa: S310
        except URLError as exc:
            last_error = exc
            if attempt == _API_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
    raise last_error  # pragma: no cover
_IMAGE_PATTERN = re.compile(r"!\[[^]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_FIGURE_REFERENCE_PATTERN = re.compile(r"\b(?:fig(?:ure)?\.?)\s*([0-9]+[a-z]?)\b", re.IGNORECASE)
_PARAGRAPH_ANCHOR_PATTERN = re.compile(r"^anchor_paragraph_p(\d+)$")
_TABLE_BLOCK_PATTERN = re.compile(r"<table[\s>]", re.IGNORECASE)
_TABLE_REFERENCE_PATTERN = re.compile(r"\btable\.?\s*([0-9]+[a-z]?)\b", re.IGNORECASE)
_TABLE_CAPTION_PATTERN = re.compile(r"^\s*table\s*([0-9]+[a-z]?)\s*[.:]", re.IGNORECASE | re.MULTILINE)


def _best_paragraph_anchor(content: str, paragraphs: list[JSONDict]) -> JSONDict | None:
    """Apply Pipeline 7.0's deterministic anchoring threshold."""
    query = _tokens(content)
    if not query:
        return None
    eligible: list[tuple[float, int, JSONDict]] = []
    for index, paragraph in enumerate(paragraphs):
        text = str(paragraph.get("text", ""))
        nonempty_lines = [line.strip() for line in text.splitlines() if line.strip()]
        if nonempty_lines and all(line.startswith("#") for line in nonempty_lines):
            continue
        paragraph_tokens = _tokens(text)
        shared = set(query) & set(paragraph_tokens)
        score = _cosine(query, paragraph_tokens)
        if len(shared) >= 2 and score >= 0.2:
            eligible.append((score, index, paragraph))
    return max(eligible, default=(0.0, 0, None), key=lambda item: (item[0], -item[1]))[2]


def _anchors_share_local_context(left: list[str], right: list[str], distance: int = 2) -> bool:
    """Treat same or nearby paper paragraphs as one local experiment context."""
    if set(left) & set(right):
        return True
    left_numbers = {
        int(match.group(1))
        for anchor_id in left
        if (match := _PARAGRAPH_ANCHOR_PATTERN.match(anchor_id))
    }
    right_numbers = {
        int(match.group(1))
        for anchor_id in right
        if (match := _PARAGRAPH_ANCHOR_PATTERN.match(anchor_id))
    }
    return any(abs(a - b) <= distance for a in left_numbers for b in right_numbers)


def _global_candidate_claims(
    candidate: JSONDict,
    document: JSONDict,
    local_ids: set[str],
    *,
    threshold: float = 0.12,
) -> list[JSONDict]:
    """Add a bounded global relation-candidate layer across experiment windows."""
    paragraph_text = " ".join(
        str(item.get("text", "")) for item in candidate.get("paragraphs", []) if isinstance(item, dict)
    )
    query_tokens = _tokens(paragraph_text)
    if not query_tokens:
        return []
    ranked: list[tuple[float, str, JSONDict]] = []
    for knowledge_id, knowledge in document["knowledges"].items():
        if knowledge_id in local_ids or knowledge.get("type") != "claim":
            continue
        content = str(knowledge.get("content", {}).get("canonical", ""))
        score = _cosine(query_tokens, _tokens(content))
        if score >= threshold:
            ranked.append((score, knowledge_id, {
                "id": knowledge_id, "type": "claim", "content": content,
                "source_anchor_ids": list(knowledge.get("source_anchor_ids", [])),
            }))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    # Keep the global layer bounded so relation prompts do not become a
    # quadratic all-claims prompt for long papers.
    return [item[2] for item in ranked[:24]]


def _attach_related_claims(candidate: JSONDict, document: JSONDict) -> JSONDict:
    """Attach the same bounded local/global target set to every extractor path."""
    candidate = copy.deepcopy(candidate)
    candidate_anchor_ids = [
        item["anchor_id"] for item in candidate.get("paragraphs", [])
        if isinstance(item, dict) and isinstance(item.get("anchor_id"), str)
    ]
    candidate["related_claims"] = [
        {"id": knowledge_id, "type": "claim", "content": knowledge["content"]["canonical"],
         "source_anchor_ids": knowledge.get("source_anchor_ids", [])}
        for knowledge_id, knowledge in document["knowledges"].items()
        if knowledge.get("type") == "claim"
        and _anchors_share_local_context(candidate_anchor_ids, knowledge.get("source_anchor_ids", []))
    ]
    local_claim_ids = {item["id"] for item in candidate["related_claims"]}
    candidate["related_claims"].extend(
        item for item in _global_candidate_claims(candidate, document, local_claim_ids)
        if item["id"] not in local_claim_ids
    )
    return candidate


def _deduplicate_extractions(
    claims: list[JSONDict],
    relations: list[JSONDict],
) -> tuple[list[JSONDict], list[JSONDict]]:
    """Globally deduplicate observations and preserve all downstream references."""
    def key(text: Any) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip().casefold()

    canonical_by_key: dict[str, JSONDict] = {}
    aliases: dict[str, str] = {}
    unique_claims: list[JSONDict] = []
    for claim in claims:
        claim_key = key(claim.get("content"))
        extraction_key = str(claim.get("_extraction_key", f"generated:{len(unique_claims)}"))
        if claim_key in canonical_by_key:
            aliases[extraction_key] = str(canonical_by_key[claim_key].get("_extraction_key", extraction_key))
            continue
        canonical_by_key[claim_key] = claim
        aliases[extraction_key] = extraction_key
        unique_claims.append(claim)

    unique_relations: list[JSONDict] = []
    # Relation identity is the endpoint pair, not the model's wording.  A
    # single source/target pair must have one canonical relation even when
    # overlapping figure windows produce different expressions.
    seen_relations: set[tuple[tuple[str, ...], str]] = set()
    for relation in relations:
        sources = tuple(sorted(aliases.get(str(value), str(value)) for value in relation.get("observation_keys", [])))
        signature = (sources, str(relation.get("claim_id", "")))
        if signature in seen_relations:
            continue
        seen_relations.add(signature)
        copied = dict(relation)
        copied["observation_keys"] = list(sources)
        expression = str(copied.get("expression", ""))
        for old, mapped in aliases.items():
            if old != mapped:
                expression = expression.replace(f"[O:{old}]", f"[O:{mapped}]")
        copied["expression"] = expression
        unique_relations.append(copied)
    return unique_claims, unique_relations


def _load_deepseek_env() -> None:
    """Load only the supported DeepSeek settings without exposing values."""
    source_path = Path(__file__).resolve()
    env_path = next((parent / "agent" / ".env" for parent in source_path.parents
                     if (parent / "agent" / ".env").is_file()), source_path.parents[2] / "agent" / ".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"}:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _response_content(raw: Any) -> str:
    """Prefer content, falling back to reasoning_content for JSON responses."""
    try:
        message = raw["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("DeepSeek response has no choices[0].message") from exc
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("DeepSeek response content must be a string")
    if isinstance(content, str) and content.strip():
        return content
    reasoning = message.get("reasoning_content")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ValueError("DeepSeek response reasoning_content must be a string")
    return reasoning if isinstance(reasoning, str) else ""


class DeepSeekV4FlashObservationTool:
    """Extract text-grounded S/A/B/M/R/U claims with DeepSeek V4 Flash."""

    name = "deepseek-v4-flash-observation"
    version = "7"
    model = MODEL_NAME

    @classmethod
    def _extract_relations(
        cls, candidate: JSONDict, observations: list[JSONDict], base_url: str,
    ) -> tuple[list[JSONDict], list[Any]]:
        """Run relation review for an already extracted observation batch."""
        relations: list[JSONDict] = []
        raw_responses: list[Any] = []
        groups = cls._relation_groups(observations, candidate)
        for component in cls._relation_group_components(groups):
            relation_request = Request(
                f"{base_url}/chat/completions",
                data=json.dumps({
                    "model": cls.model,
                    "messages": [{"role": "user", "content": cls._relation_prompt(component)}],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                }, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with _urlopen_with_retry(relation_request) as response:  # noqa: S310
                    relation_raw: Any = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                raise RuntimeError(f"DeepSeek relation request failed with HTTP {exc.code}") from exc
            except URLError as exc:
                raise RuntimeError(f"DeepSeek relation request failed: {exc.reason}") from exc
            raw_responses.append(relation_raw)
            try:
                relations.extend(cls._normalize_group_relations(_response_content(relation_raw), component))
            except Exception as exc:
                failure = ValueError(f"DeepSeek relation response normalization failed: {exc}")
                # Preserve the received provider payload for the caller's
                # existing semantic-review audit artifact.
                failure.raw_responses = list(raw_responses)  # type: ignore[attr-defined]
                raise failure from exc
        return relations, raw_responses

    def invoke(self, request: ToolCallRequest) -> ToolCallResponse:
        if request.operation == "normalize_weakpoint_clusters":
            return self._normalize_weakpoint_clusters(request)
        if request.operation == "classify_weakpoints":
            return self._classify_weakpoints(request)
        if request.operation == "rewrite_observations":
            return self._rewrite_observations(request)
        _load_deepseek_env()
        candidates = request.parameters.get("experiment_candidates")
        if not isinstance(candidates, list):
            raise ValueError("experiment_candidates must be a list")
        if not candidates:
            # No figure/table window exists: Step 1 already imported every
            # observation claim, so there is nothing left to extract here.
            return ToolCallResponse(
                request.call_id,
                "succeeded",
                {"skipped": "no_experiment_candidates"},
                {"status": "claims_extracted", "claims": [], "relations": []},
                metadata={"candidate_count": 0, "model": self.model},
            )
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        selected_claims = request.parameters.get("selected_claims")
        frozen_claims = request.parameters.get("frozen_claims")
        jobs = [
            (
                candidate,
                self._claims_for_candidate(selected_claims, candidate),
                self._claims_for_candidate(frozen_claims, candidate),
            )
            for candidate in candidates
        ]
        if isinstance(selected_claims, list):
            jobs = [job for job in jobs if job[1]]
            if not jobs:
                raise ValueError("selected claims do not map to any experiment candidate")
        raw_responses: list[Any] = []
        claims: list[JSONDict] = []

        def normalization_failure(exc: Exception) -> ToolCallResponse:
            return ToolCallResponse(
                request.call_id,
                "failed",
                {"responses": list(raw_responses)},
                error={"type": type(exc).__name__, "message": str(exc)},
                metadata={
                    "candidate_count": len(candidates),
                    "request_count": len(raw_responses),
                    "model": self.model,
                },
            )

        for candidate, selected_for_candidate, frozen_for_candidate in jobs:
            prompt = self._prompt(
                [candidate],
                selected_for_candidate if isinstance(selected_claims, list) else None,
                request.parameters.get("review_advice"),
                frozen_for_candidate,
            )
            body = json.dumps(
                {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            http_request = Request(
                f"{base_url}/chat/completions",
                data=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with _urlopen_with_retry(http_request) as response:  # noqa: S310
                    raw: Any = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace").strip()
                suffix = f": {detail[:2000]}" if detail else ""
                raise RuntimeError(f"DeepSeek request failed with HTTP {exc.code}{suffix}") from exc
            except URLError as exc:
                raise RuntimeError(f"DeepSeek request failed: {exc.reason}") from exc
            raw_responses.append(raw)
            try:
                normalized = self._normalize(
                    _response_content(raw),
                    [candidate],
                    selected_for_candidate if isinstance(selected_claims, list) else None,
                )
            except Exception as exc:
                return normalization_failure(exc)
            if normalized.get("status") == "insufficient_context":
                return ToolCallResponse(
                    request.call_id,
                    "succeeded",
                    {"responses": raw_responses},
                    normalized,
                    metadata={"candidate_count": len(candidates), "request_count": len(raw_responses), "model": self.model},
                )
            claims.extend(normalized["claims"])
        if isinstance(selected_claims, list):
            expected_ids = {item["id"] for item in selected_claims if isinstance(item, dict) and isinstance(item.get("id"), str)}
            actual_ids = [item.get("id") for item in claims]
            if set(actual_ids) != expected_ids or len(actual_ids) != len(set(actual_ids)):
                return normalization_failure(ValueError("targeted rewrite must return exactly the selected claim ids"))
        normalized = {
            "status": "claims_extracted",
            "claims": claims,
            "relations": [],
        }
        return ToolCallResponse(
            request.call_id,
            "succeeded",
            {"responses": raw_responses},
            normalized,
            metadata={"candidate_count": len(candidates), "request_count": len(raw_responses), "model": self.model},
        )

    @staticmethod
    def _rewrite_observation_prompt(selected: list[JSONDict]) -> str:
        return (
            "Return JSON only. Rewrite each supplied experimental observation into one fluent, complete English sentence "
            "using the S/A/B/M/R structure: S=setting/scope, A=treatment/method/object, M=measure, R=result, with optional "
            "B=baseline and U=uncertainty. Preserve the meaning, scope, comparison direction, conditions, result, and any "
            "reported uncertainty. Do not add, omit, generalize, narrow, reinterpret, or move facts between fields. "
            "A and M must be present; omit B or U when the source does not state them (never write absence placeholders). "
            "Do not mechanically concatenate a fixed template. Retain each supplied id verbatim. Use the supplied source "
            "excerpts only to recover a setting or action that the observation itself omits. Return exactly one rewritten "
            "claim per id and no other ids. Return the refined S/A/B/M/R fields together with the complete sentence. "
            "Return {\"claims\":[{\"id\":\"...\",\"S\":\"...\",\"A\":\"...\",\"M\":\"...\",\"content\":\"...\","
            "\"paragraph_anchor_ids\":[\"...\"]}]}.\n\n"
            + json.dumps(selected, ensure_ascii=False)
        )

    @staticmethod
    def _normalize_rewritten_observations(content: str, selected: list[JSONDict]) -> list[JSONDict]:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("DeepSeek rewrite response is not valid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"claims"}:
            raise ValueError("DeepSeek rewrite response must contain only claims")
        items = payload["claims"]
        expected_ids = {item["id"] for item in selected if isinstance(item, dict) and isinstance(item.get("id"), str)}
        if not isinstance(items, list) or len(items) != len(expected_ids):
            raise ValueError("DeepSeek must return exactly one rewritten claim per selected id")
        normalized: list[JSONDict] = []
        seen: set[str] = set()
        allowed_fields = {"id", "content", "paragraph_anchor_ids", *_OBSERVATION_COMPONENT_FIELDS}
        for item in items:
            if not isinstance(item, dict) or not set(item) <= allowed_fields:
                raise ValueError("rewritten claims contain unexpected fields")
            if not {"id", "content", "paragraph_anchor_ids"} <= set(item):
                raise ValueError("rewritten claims require id, content, paragraph_anchor_ids")
            claim_id = item["id"]
            if not isinstance(claim_id, str) or claim_id not in expected_ids or claim_id in seen:
                raise ValueError("rewritten claim has an unknown or duplicate id")
            rewritten_content = item["content"]
            anchors = item["paragraph_anchor_ids"]
            if not isinstance(rewritten_content, str) or not rewritten_content.strip():
                raise ValueError("rewritten claim content must be non-empty")
            if not isinstance(anchors, list) or not anchors or not all(isinstance(anchor, str) for anchor in anchors):
                raise ValueError("rewritten claim requires paragraph anchors")
            seen.add(claim_id)
            refined = {"id": claim_id, "content": rewritten_content.strip(), "paragraph_anchor_ids": list(anchors)}
            for field in _OBSERVATION_COMPONENT_FIELDS:
                value = item.get(field)
                if isinstance(value, str) and value.strip():
                    refined[field] = value.strip()
            normalized.append(refined)
        if seen != expected_ids:
            raise ValueError("DeepSeek omitted a selected id")
        return normalized

    def _rewrite_observations(self, request: ToolCallRequest) -> ToolCallResponse:
        """Rewrite imported observation claims into S/A/B/M/R format, retaining ids, without generating E."""
        _load_deepseek_env()
        selected = request.parameters.get("selected_claims")
        if not isinstance(selected, list) or not selected:
            raise ValueError("selected_claims must be a non-empty list")
        for item in selected:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("selected claims require string ids")
            content = item.get("content")
            if isinstance(content, dict):
                content = content.get("canonical")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("selected claims require non-empty content")
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": self._rewrite_observation_prompt(selected)}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        http_request = Request(
            f"{base_url}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST",
        )
        try:
            with _urlopen_with_retry(http_request) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"DeepSeek rewrite request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"DeepSeek rewrite request failed: {exc.reason}") from exc
        claims = self._normalize_rewritten_observations(_response_content(raw), selected)
        return ToolCallResponse(
            request.call_id, "succeeded", {"responses": [raw]},
            {"status": "claims_extracted", "claims": claims, "relations": []},
            metadata={"candidate_count": 0, "request_count": 1, "model": self.model},
        )

    def _classify_weakpoints(self, request: ToolCallRequest) -> ToolCallResponse:
        """Classify only the supplied weakpoint records; never infer new graph data."""
        _load_deepseek_env()
        records = request.parameters.get("weakpoints")
        if not isinstance(records, list) or not records:
            raise ValueError("weakpoints must be a non-empty list")
        record_ids = [item.get("weakpoint_id") for item in records if isinstance(item, dict)]
        if len(record_ids) != len(records) or any(not isinstance(item, str) or not item for item in record_ids):
            raise ValueError("every weakpoint requires a non-empty weakpoint_id")
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("weakpoint IDs must be unique")
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        prompt = (
            "Classify every weakpoint using only its supplied evidence claims, one or more target claims, expression, and "
            "evidence-anchor excerpts. Do not use outside knowledge or invent author intent, hidden premises, "
            "alternative explanations, mappings, or conditions.\n\n"
            "Allowed labels only:\n"
            "- deduction: supplied premises plus a source-grounded rule, definition, model, computation, or bounded intermediate proposition makes the target follow. "
            "The intermediate proposition or rule need not already have an ID: mark the weakpoint as deduction when the supplied excerpts support that missing layer, because Step 4 will extract and connect it. "
            "Never label a purely empirical observation as deduction when no such source-grounded rule or chain is available.\n"
            "- abduction: observations support an explanatory hypothesis, mechanism, or general law that goes beyond the observations, "
            "with alternatives not ruled out by the supplied material. A target broader than one experiment may still be abduction when the supplied evidence is a legitimate example or partial support; Step 4 may add an AltExp alternative explanation and keep uncertainty in the premises.\n"
            "- analogy: a supplied source-domain law/structure is transferred to a target domain through an identifiable but unexpanded mapping or condition.\n"
            "- null: the supplied material cannot identify one of the above, even after allowing a source-grounded intermediate proposition or an explicit alternative explanation. Do not use null merely because the relation is empirical or the target is broader.\n\n"
            "Screening gate before any non-null label: every evidence and target ID must be distinct and grounded in at least one supplied original-paper excerpt; reject duplicate/equivalent endpoint content, mere topical co-occurrence, unsupported causal or quantitative leaps, and conclusions whose scope is plainly outside the supplied evidence. If the only problem is a recoverable intermediate rule/claim or an unresolved alternative explanation, keep deduction/abduction and let Step 4 expand it; otherwise return null.\n\n"
            "Do not emit separate cross-layer evidence relations from each atomic experiment to a claim that already summarizes those experiments; keep the adjacent summary relation only. Do not retain an A→C shortcut when the supplied relations already form A→B→C unless the paper explicitly states A→C.\n\n"
            "Return JSON only: {\"classifications\":[{\"weakpoint_id\":\"...\",\"reasoning_type\":\"abduction|analogy|deduction|null\"}]}. "
            "Return every supplied ID exactly once and no other fields.\n\n"
            + ("Validation feedback from a previous attempt. Repair only the listed error and return the same IDs:\n"
               + str(request.parameters.get("repair_feedback")) + "\n\n"
               if request.parameters.get("repair_feedback") else "")
            + f"Weakpoints:\n{json.dumps(records, ensure_ascii=False)}"
        )
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        http_request = Request(
            f"{base_url}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST",
        )
        try:
            with _urlopen_with_retry(http_request) as response:  # noqa: S310
                raw: Any = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            suffix = f": {detail[:2000]}" if detail else ""
            raise RuntimeError(f"DeepSeek weakpoint classification failed with HTTP {exc.code}{suffix}") from exc
        except URLError as exc:
            raise RuntimeError(f"DeepSeek weakpoint classification failed: {exc.reason}") from exc
        try:
            payload = json.loads(_response_content(raw))
        except json.JSONDecodeError as exc:
            raise ValueError("DeepSeek weakpoint classification response is not valid JSON") from exc
        classifications = payload.get("classifications") if isinstance(payload, dict) else None
        if not isinstance(classifications, list) or len(classifications) != len(records):
            raise ValueError("DeepSeek must classify every weakpoint exactly once")
        allowed = {"abduction", "analogy", "deduction", None}
        resolved: dict[str, str | None] = {}
        for item in classifications:
            if not isinstance(item, dict) or set(item) != {"weakpoint_id", "reasoning_type"}:
                raise ValueError("weakpoint classifications must contain only weakpoint_id and reasoning_type")
            weakpoint_id, reasoning_type = item["weakpoint_id"], item["reasoning_type"]
            if not isinstance(weakpoint_id, str) or weakpoint_id not in record_ids or weakpoint_id in resolved:
                raise ValueError("weakpoint classification has an unknown or duplicate ID")
            # JSON `null` is the contract value.  Models sometimes serialize that
            # literal as the string "null" despite the response schema; normalize
            # this representation without assigning a substantive category.
            if reasoning_type == "null":
                reasoning_type = None
            if reasoning_type not in allowed:
                raise ValueError("weakpoint classification has an unsupported reasoning_type")
            resolved[weakpoint_id] = reasoning_type
        if set(resolved) != set(record_ids):
            raise ValueError("DeepSeek weakpoint classification omitted an ID")
        return ToolCallResponse(
            request.call_id, "succeeded", {"response": raw},
            {"classifications": [{"weakpoint_id": item, "reasoning_type": resolved[item]} for item in record_ids]},
            metadata={"weakpoint_count": len(records), "model": self.model},
        )

    def _normalize_weakpoint_clusters(self, request: ToolCallRequest) -> ToolCallResponse:
        """Jointly orient and partition bounded candidate-relation clusters."""
        _load_deepseek_env()
        clusters = request.parameters.get("clusters")
        if not isinstance(clusters, list) or not clusters:
            raise ValueError("clusters must be a non-empty list")
        cluster_ids = [item.get("cluster_id") for item in clusters if isinstance(item, dict)]
        if (len(cluster_ids) != len(clusters)
                or any(not isinstance(item, str) or not item for item in cluster_ids)
                or len(cluster_ids) != len(set(cluster_ids))):
            raise ValueError("clusters require unique non-empty cluster_id values")
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        prompt = (
            "Normalize each bounded scientific argument cluster into its complete minimal set of reasoning weakpoints. "
            "Candidate relations are coarse extraction hints: you may merge them, reverse their direction, or replace a transitive shortcut with adjacent relations, but only within the supplied cluster and only when the supplied claims and original-paper excerpts justify it. "
            "Do not create claims, notes, facts, IDs, conditions, mappings, or intermediate propositions. Step 4 handles source-grounded intermediate propositions. "
            "Fixed equivalence/contradiction relations are outside this task. relation_context_id only bounds review context and never proves that relations should merge.\n\n"
            "Prefer a minimal adjacent hierarchy. Do not retain A-to-C when the cluster supports A-to-B and B-to-C, unless an independent supplied relation explicitly asserts A-to-C. "
            "A multi-target weakpoint is allowed only when the same premises, scope, and reasoning family directly support every target. "
            "When a non-experimental claim B is a self-contained composite statement that strictly entails several atomic observation claims O, represent B -> [O1,O2] as one deduction weakpoint. "
            "If those observation claims were separately extracted as evidence for the same explanatory hypothesis A, prefer the non-duplicative adjacent structure B -> A when B faithfully contains the relevant observations; do not also emit redundant O1 -> A and O2 -> A weakpoints. "
            "Experimental observations supporting a general hypothesis are abduction, never deduction.\n\n"
            "Labels: deduction means supplied premises plus source-stated rules/conditions, possibly through a source-grounded intermediate proposition, can strictly entail every target; abduction means an observation supports an explanatory hypothesis, mechanism, or generalization while alternatives remain; analogy requires a source-domain law and identifiable cross-domain mapping; null means none can be established even with a bounded Step 4 expansion. "
            "For deduction, do not hide unsupported rules or empirical assertions, but do mark a relation when the missing rule/intermediate claim is recoverable from the supplied excerpts. For abduction, premises are observations and targets may be broader non-experimental explanations or empirical generalizations when the evidence is explicitly supportive.\n\n"
            "Before assigning a non-null label, screen the relation: endpoints must be valid and distinct, at least one endpoint must have a supplied paper-text excerpt, and the link must be more than topical co-occurrence, an unsupported causal/quantitative leap, or a duplicate/equivalence. If only a source-grounded intermediate layer or alternative explanation is missing, retain the candidate for Step 4 expansion; if the evidence cannot support even that bounded expansion, use null.\n\n"
            "Return JSON only with exactly this schema: {\"clusters\":[{\"cluster_id\":\"...\",\"weakpoints\":[{\"member_relation_ids\":[\"...\"],\"evidence_claim_ids\":[\"...\"],\"target_claim_id\":[\"...\"],\"reasoning_type\":null,\"expression\":\"...\"}],\"rejected_relation_ids\":[\"...\"]}]}. "
            "Return every cluster exactly once. Every candidate relation ID must occur exactly once, either in one weakpoint's member_relation_ids or in rejected_relation_ids. "
            "Every weakpoint must consume at least one candidate relation. Use only supplied claim IDs, keep evidence and targets non-empty, unique, and disjoint, and cite every endpoint as [claim_id] in expression. "
            "Reject a candidate only when no supported reasoning relation remains after joint normalization."
            + ("\n\nValidation feedback from a previous attempt. Repair only these errors; do not change the supplied claims or relation IDs:\n"
               + str(request.parameters.get("repair_feedback", ""))
               if request.parameters.get("repair_feedback") else "")
            + "\n\nClusters:\n"
            + json.dumps(clusters, ensure_ascii=False)
        )
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        http_request = Request(
            f"{base_url}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST",
        )
        try:
            with _urlopen_with_retry(http_request) as response:  # noqa: S310
                raw: Any = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            suffix = f": {detail[:2000]}" if detail else ""
            raise RuntimeError(f"DeepSeek weakpoint cluster normalization failed with HTTP {exc.code}{suffix}") from exc
        except URLError as exc:
            raise RuntimeError(f"DeepSeek weakpoint cluster normalization failed: {exc.reason}") from exc
        try:
            payload = json.loads(_response_content(raw))
        except json.JSONDecodeError as exc:
            raise ValueError("DeepSeek weakpoint cluster response is not valid JSON") from exc
        items = payload.get("clusters") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) != len(clusters):
            raise ValueError("DeepSeek must normalize every weakpoint cluster exactly once")
        source_by_id = {item["cluster_id"]: item for item in clusters}
        normalized_by_id: dict[str, JSONDict] = {}
        allowed = {"abduction", "analogy", "deduction", None}
        for item in items:
            if (not isinstance(item, dict)
                    or set(item) != {"cluster_id", "weakpoints", "rejected_relation_ids"}
                    or item.get("cluster_id") not in source_by_id
                    or item["cluster_id"] in normalized_by_id):
                raise ValueError("weakpoint cluster result has invalid fields or ID")
            source = source_by_id[item["cluster_id"]]
            relation_ids = {
                relation["relation_id"] for relation in source.get("candidate_relations", [])
                if isinstance(relation, dict) and isinstance(relation.get("relation_id"), str)
            }
            node_ids = {
                node["claim_id"] for node in source.get("claims", [])
                if isinstance(node, dict) and isinstance(node.get("claim_id"), str)
            }
            weakpoints, rejected = item["weakpoints"], item["rejected_relation_ids"]
            if (not isinstance(weakpoints, list) or not isinstance(rejected, list)
                    or any(not isinstance(key, str) for key in rejected)):
                raise ValueError("weakpoint cluster requires weakpoints and rejected relation IDs")
            consumed: list[str] = list(rejected)
            normalized_weakpoints: list[JSONDict] = []
            for weakpoint in weakpoints:
                if not isinstance(weakpoint, dict) or set(weakpoint) != {
                    "member_relation_ids", "evidence_claim_ids", "target_claim_id", "reasoning_type", "expression",
                }:
                    raise ValueError("normalized weakpoint has invalid fields")
                members = weakpoint["member_relation_ids"]
                evidence = weakpoint["evidence_claim_ids"]
                targets = weakpoint["target_claim_id"]
                if any(not isinstance(values, list) or not values or len(values) != len(set(values))
                       or any(not isinstance(key, str) or not key for key in values)
                       for values in (members, evidence, targets)):
                    raise ValueError("normalized weakpoint IDs must be non-empty and unique")
                reasoning_type = weakpoint["reasoning_type"]
                if reasoning_type == "null":
                    reasoning_type = None
                expression = weakpoint["expression"]
                if (reasoning_type not in allowed or not set(evidence + targets) <= node_ids
                        or set(evidence) & set(targets) or not set(members) <= relation_ids
                        or not isinstance(expression, str) or not expression.strip()):
                    raise ValueError("normalized weakpoint has unsupported endpoints, type, or expression")
                if any(f"[{key}]" not in expression for key in [*evidence, *targets]):
                    raise ValueError("normalized weakpoint expression must cite every endpoint")
                consumed.extend(members)
                normalized_weakpoints.append({**weakpoint, "reasoning_type": reasoning_type})
            if len(consumed) != len(set(consumed)) or set(consumed) != relation_ids:
                raise ValueError("every cluster relation must be consumed or rejected exactly once")
            normalized_by_id[item["cluster_id"]] = {
                "cluster_id": item["cluster_id"], "weakpoints": normalized_weakpoints,
                "rejected_relation_ids": rejected,
            }
        if set(normalized_by_id) != set(source_by_id):
            raise ValueError("DeepSeek weakpoint cluster response omitted a cluster")
        return ToolCallResponse(
            request.call_id, "succeeded", {"response": raw},
            {"clusters": [normalized_by_id[item] for item in cluster_ids]},
            metadata={"cluster_count": len(clusters), "model": self.model},
        )

    @staticmethod
    def _relation_groups(observations: list[JSONDict], candidate: JSONDict) -> list[JSONDict]:
        """Offer each local target one set of eligible observation claims."""
        groups: list[JSONDict] = []
        for item in candidate.get("related_claims", []):
            if (not isinstance(item, dict) or item.get("type") != "claim"
                    or not isinstance(item.get("id"), str) or not isinstance(item.get("content"), str)):
                continue
            evidence_candidates = [
                {
                    "observation_key": observation["observation_key"],
                    "observation": observation["content"],
                }
                for observation in observations
            ]
            if evidence_candidates:
                groups.append({
                    "group_id": f"group_{len(groups) + 1}",
                    "target_claim_id": item["id"],
                    "target_claim_or_note": item["content"],
                    "evidence_candidates": evidence_candidates,
                })
        return groups

    @staticmethod
    def _relation_group_components(groups: list[JSONDict], max_groups: int = 6) -> list[list[JSONDict]]:
        """Batch small pairwise-overlap triangles, not long overlap chains."""
        if len(groups) < 3:
            return [[group] for group in groups]
        claim_sets = [
            {str(group["target_claim_id"]), *{
                str(item["observation_key"])
                for item in group.get("evidence_candidates", [])
            }}
            for group in groups
        ]
        parent = list(range(len(groups)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                for k in range(j + 1, len(groups)):
                    if (claim_sets[i] & claim_sets[j]
                            and claim_sets[i] & claim_sets[k]
                            and claim_sets[j] & claim_sets[k]):
                        roots = {find(i), find(j), find(k)}
                        size = sum(find(x) in roots for x in range(len(groups)))
                        if size <= max_groups:
                            union(i, j)
                            union(i, k)
                        break
        components: dict[int, list[JSONDict]] = {}
        for index, group in enumerate(groups):
            components.setdefault(find(index), []).append(group)
        return list(components.values())

    @staticmethod
    def _relation_prompt(groups: list[JSONDict]) -> str:
        return (
            "Return JSON only. Classify every supplied target-centered candidate group exactly once; do not add or omit groups. "
            "Each group contains one target claim and the observation claims O that share a source anchor or local experiment context with it. "
            "Use only the supplied text and IDs; do not invent premises, mechanisms, relations, or IDs. "
            "relation_type must be exactly one of evidence, conjunction, disjunction, negation, implication, equivalence, contradiction, none. "
            "Use fixed logical expressions only: 且 / 和 / 与 for conjunction, 或 for disjunction, 非 for negation, "
            "推出 for implication direction, 等价 for equivalence, and 矛盾 or 不可同时成立 for contradiction. "
            "When observation claims support a general claim, default to evidence, never implication: do not write '[O:...] 推出 [target]'. "
            "For each group, return relations as an array. A relation may select one or more eligible observation keys. "
            "Select multiple observations only when they jointly support the target and removing one changes the relation's meaning; otherwise use a single observation. "
            "For evidence, write '[O:key] 是 [target] 的例子或证据' or '([O:key1] 和 [O:key2]) 是 [target] 的例子或证据'. "
            "Every non-none expression must cite every selected [O:key] and the group's exact [target_claim_id]. "
            "Use an empty relations array if no relation is supported. "
            "Return {\"classifications\":[{\"group_id\":\"group_1\",\"relations\":[{\"source_observation_keys\":[\"o_1\",\"o_2\"],"
            "\"relation_type\":\"evidence\",\"expression\":\"([O:o_1] 和 [O:o_2]) 是 [claim_7] 的例子或证据\"}]}]}.\nGroups:\n"
            + json.dumps(groups, ensure_ascii=False)
        )

    @staticmethod
    def _normalize_group_relations(content: str, groups: list[JSONDict]) -> list[JSONDict]:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("DeepSeek relation response is not valid JSON") from exc
        classifications = payload.get("classifications") if isinstance(payload, dict) else None
        if not isinstance(classifications, list) or len(classifications) != len(groups):
            raise ValueError("DeepSeek relation response must classify every candidate group exactly once")
        by_id = {group["group_id"]: group for group in groups}
        seen: set[str] = set()
        relations: list[JSONDict] = []
        allowed = {"evidence", "conjunction", "disjunction", "negation", "implication", "equivalence", "contradiction", "none"}
        for item in classifications:
            if not isinstance(item, dict) or set(item) != {"group_id", "relations"} or item.get("group_id") not in by_id or item["group_id"] in seen:
                raise ValueError("DeepSeek relation response has invalid or duplicate group_id")
            seen.add(item["group_id"])
            group_relations = item["relations"]
            if not isinstance(group_relations, list):
                raise ValueError("DeepSeek group relations must be an array")
            group = by_id[item["group_id"]]
            eligible = {candidate["observation_key"] for candidate in group["evidence_candidates"]}
            relation_sets: set[tuple[str, ...]] = set()
            for relation in group_relations:
                if not isinstance(relation, dict) or set(relation) != {"source_observation_keys", "relation_type", "expression"}:
                    raise ValueError("DeepSeek group relation has an invalid shape")
                source_keys = relation["source_observation_keys"]
                relation_type = relation["relation_type"]
                expression = relation["expression"]
                if not isinstance(source_keys, list) or not source_keys or not all(isinstance(key, str) for key in source_keys):
                    raise ValueError("DeepSeek group relation requires source observation keys")
                if len(source_keys) != len(set(source_keys)) or not set(source_keys) <= eligible:
                    raise ValueError("DeepSeek group relation references an ineligible observation")
                if relation_type not in allowed - {"none"}:
                    raise ValueError("DeepSeek relation response has an unsupported relation_type")
                if not isinstance(expression, str) or f"[{group['target_claim_id']}]" not in expression:
                    raise ValueError("DeepSeek relation expression must cite its target claim")
                if any(f"[O:{key}]" not in expression for key in source_keys):
                    raise ValueError("DeepSeek relation expression must cite every selected observation")
                source_signature = tuple(sorted(source_keys))
                if source_signature in relation_sets:
                    raise ValueError("DeepSeek group relation duplicates an evidence set")
                relation_sets.add(source_signature)
                relations.append({"observation_keys": source_keys, "claim_id": group["target_claim_id"], "expression": expression})
        if seen != set(by_id):
            raise ValueError("DeepSeek relation response omitted a candidate group")
        return relations

    @staticmethod
    def _claims_for_candidate(claims: Any, candidate: JSONDict) -> list[JSONDict]:
        if not isinstance(claims, list):
            return []
        anchor_ids = {
            item.get("anchor_id")
            for item in candidate.get("paragraphs", [])
            if isinstance(item, dict) and isinstance(item.get("anchor_id"), str)
        }
        return [
            item
            for item in claims
            if isinstance(item, dict)
            and isinstance(item.get("source_anchor_ids"), list)
            and bool(anchor_ids & set(item["source_anchor_ids"]))
        ] or [
            # Legacy claims may only carry claims-file anchors when the
            # experiment is encoded in a figure.  Let each bounded figure
            # window assess the targeted claim; windows without evidence
            # return insufficient_context, while the owning figure preserves
            # the original ID.
            item for item in claims if isinstance(item, dict)
        ]

    @staticmethod
    def _prompt(
        candidates: list[JSONDict],
        selected_claims: Any,
        review_advice: Any,
        frozen_claims: Any,
        repair_feedback: Any = None,
    ) -> str:
        evidence = []
        for candidate in candidates:
            allowed_anchor_ids = [
                item["anchor_id"]
                for item in candidate.get("paragraphs", [])
                if isinstance(item, dict) and isinstance(item.get("anchor_id"), str)
            ]
            focus_anchor_ids = candidate.get("focus_anchor_ids", allowed_anchor_ids)
            paragraphs = "\n\n".join(
                f"[{item['anchor_id']}] {item['text']}"
                for item in candidate.get("paragraphs", [])
                if isinstance(item, dict)
            )
            kind = candidate.get("kind", "figure")
            label = candidate.get("table_label") if kind == "table" else candidate.get("image_name")
            evidence.append(
                f"Candidate {candidate.get('candidate_id')} / {kind} {label}. "
                f"Allowed paragraph_anchor_ids: {json.dumps(allowed_anchor_ids)}. "
                f"Focus paragraph_anchor_ids: {json.dumps(focus_anchor_ids)}.\n{paragraphs}"
            )
        rewrite = ""
        if isinstance(selected_claims, list):
            rewrite = (
                "\nThis is a targeted human-requested rewrite. Return exactly one corrected claim for "
                "each supplied id, retain each id verbatim, and do not return any unselected claim. "
                "Frozen claims are immutable context: do not rewrite them, and ensure the corrected claims "
                "remain distinct from them.\n"
                f"Selected claims: {json.dumps(selected_claims, ensure_ascii=False)}\n"
                f"Frozen claims: {json.dumps(frozen_claims or [], ensure_ascii=False)}\n"
                f"Human advice: {review_advice}\n"
            )
        table_guidance = ""
        if any(candidate.get("kind") == "table" for candidate in candidates):
            labels = ", ".join(
                str(candidate.get("table_label") or candidate.get("candidate_id"))
                for candidate in candidates
                if candidate.get("kind") == "table"
            )
            table_guidance = (
                f"\nThis candidate is a TABLE ({labels}). Read the entire table together with its caption and the "
                "surrounding text, and extract the significant experimental findings the paper draws from it. Do not "
                "emit one claim per cell, per row, per column, or per (dataset × method × metric) combination as a "
                "separate claim. If it is a large results matrix whose caption or text draws only a few aggregate "
                "conclusions, emit only those; for cells the paper draws no specific finding from, emit nothing. "
            )
        return (
            "Return JSON only. Use only the supplied paper paragraphs. Do not use outside knowledge or infer missing facts. "
            "First decide whether the supplied text actually reports an experiment or measured observation attributable "
            "to this paper. Do not return method descriptions, background, proposed experiments, or claims about prior "
            "work as experimental claims; if no attributable result is present, return insufficient_context. "
            "For each genuine experimental observation, extract S/A/(optional B)/M/(optional R)/(optional U): S is setting "
            "or scope; A is the treatment, method, intervention, or experimental object; B is an optional "
            "baseline or control and may be omitted when the supplied evidence states an observation without one; "
            "M is the measure; R is the observed numerical or directional comparison result and may be omitted only when "
            "the supplied text reports the experimental observation but does not state a result; U is optional and contains only an explicitly reported "
            "uncertainty value directly corresponding to R. Omit B or U when the evidence does not report it; never emit "
            "'baseline not reported', 'uncertainty not reported', or another absence placeholder. Use English. "
            "A and M are required for every returned claim. S and R may be absent only when the source genuinely does not "
            "state them; never fill them with guesses or absence placeholders. For each claim, directly write content as one fluent, complete English sentence. You may improve grammar and flow, "
            "but must preserve the meaning, scope, comparison direction, conditions, result, and any reported uncertainty of "
            "S/A/B/M/R and optional U. Do not mechanically concatenate a fixed template. Do not add, omit, merge, generalize, "
            "narrow, reinterpret, or move information between fields. "
            "For one figure or table, emit one claim per self-contained experimental finding this paper reports, not per "
            "measurement. Group every number that belongs to a single reported finding into one claim. Produce a separate "
            "claim only when the paper states a genuinely distinct finding: a materially different comparison, direction, "
            "or conclusion, whose meaning would be lost by merging. Never generate a Cartesian product of treatments, "
            "conditions, datasets, or metrics. A difference in a number, label, dataset, condition, or metric alone is not "
            "a distinct finding unless the paper reports a distinct result or conclusion for it. Do not invent distinctions "
            "unsupported by the evidence. "
            + table_guidance
            + "Return every distinct, explicitly supported experimental result; do not omit a broad result merely "
            "because more specific conditional results are also returned. "
            "Each claim must cite one or more paragraph_anchor_ids from its own candidate and include its "
            "candidate_id. Cite only IDs in that candidate's Allowed paragraph_anchor_ids list; never invent "
            "an anchor ID or cite an ID from another candidate. Each claim must cite at least one ID from its "
            "Focus paragraph_anchor_ids list; use expanded context only to complete the experiment associated "
            "with that focus evidence. The expanded window can contain related work, background, or results for a "
            "different figure. Never extract a result attributed to cited studies or prior work. Extract only a result "
            "that the supplied text explicitly attributes to this paper's experiment represented by the candidate "
            "image or its caption; if that result is not yet present, return insufficient_context. "
            "Do not assess relations between observations and existing claims or notes in this call. "
            "If evidence is insufficient, return "
            "{\"status\":\"insufficient_context\",\"needed_context\":\"...\"}; never guess. Otherwise "
            "return {\"status\":\"claims_extracted\",\"claims\":[{\"candidate_id\":\"...\","
            "\"S\":\"...\",\"A\":\"...\",\"M\":\"...\","
            "\"R\":\"...\",\"content\":\"...\"," 
            "\"paragraph_anchor_ids\":[\"...\"]}]}."
            + rewrite
            + ("\n\nRepair feedback from the previous validation attempt. Correct only the reported issue, re-check every claim against the supplied evidence, and return the complete corrected JSON:\n" + str(repair_feedback) if repair_feedback else "")
            + "\n\nEvidence:\n"
            + "\n\n".join(evidence)
        )

    @staticmethod
    def _normalize(content: str, candidates: list[JSONDict], selected_claims: Any = None) -> JSONDict:
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("DeepSeek response is not valid JSON") from exc
        if not isinstance(result, dict):
            raise ValueError("DeepSeek response must be a JSON object")
        if result.get("status") == "insufficient_context":
            return {
                "status": "insufficient_context",
                "needed_context": str(result.get("needed_context", "More paper context is required.")),
            }
        claims = result.get("claims")
        if result.get("status") != "claims_extracted" or not isinstance(claims, list):
            raise ValueError("DeepSeek response must declare claims_extracted with a claims array")
        if "relations" in result and result["relations"] not in ([], None):
            raise ValueError("observation extraction must not return relations")
        candidate_order = {
            candidate["candidate_id"]: index
            for index, candidate in enumerate(candidates)
            if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str)
        }
        anchors_by_candidate = {
            candidate["candidate_id"]: {
                item["anchor_id"]
                for item in candidate.get("paragraphs", [])
                if isinstance(item, dict) and isinstance(item.get("anchor_id"), str)
            }
            for candidate in candidates
            if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str)
        }
        focus_anchors_by_candidate = {
            candidate["candidate_id"]: set(candidate.get("focus_anchor_ids", ()))
            for candidate in candidates
            if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str)
        }
        selected_ids = {
            item["id"]
            for item in selected_claims or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        normalized: list[JSONDict] = []
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                raise ValueError(f"claim {index} must be an object")
            candidate_id = claim.get("candidate_id")
            if candidate_id not in candidate_order:
                raise ValueError(f"claim {index} cites an unknown candidate_id")
            fields = {name: claim.get(name) for name in ("S", "A", "M", "R")}
            # A and M are required to establish that this is an experimental
            # claim. S and R may be absent when the source genuinely omits
            # scope or an explicit numerical/directional result; never invent
            # an absence placeholder.
            for name in ("A", "M"):
                if not isinstance(fields[name], str) or not fields[name].strip():
                    raise ValueError(f"claim {index} must provide non-empty {name}")
            for name in ("S", "R"):
                if fields[name] is not None and (not isinstance(fields[name], str) or not fields[name].strip()):
                    raise ValueError(f"claim {index} {name} must be omitted or a non-empty string")
            baseline = claim.get("B")
            if isinstance(baseline, str) and not baseline.strip():
                baseline = None
            if baseline is not None and (not isinstance(baseline, str) or not baseline.strip()):
                raise ValueError(f"claim {index} B must be omitted or a non-empty string")
            if isinstance(baseline, str) and baseline.strip().casefold() == "baseline not reported":
                raise ValueError(f"claim {index} must omit B instead of using an absence placeholder")
            uncertainty = claim.get("U")
            if isinstance(uncertainty, str) and not uncertainty.strip():
                uncertainty = None
            if uncertainty is not None and (not isinstance(uncertainty, str) or not uncertainty.strip()):
                raise ValueError(f"claim {index} U must be omitted or a non-empty string")
            if isinstance(uncertainty, str) and uncertainty.strip().casefold() == "uncertainty not reported":
                raise ValueError(f"claim {index} must omit U instead of using an absence placeholder")
            claim_content = claim.get("content")
            if not isinstance(claim_content, str) or not claim_content.strip():
                raise ValueError(f"claim {index} must provide non-empty content")
            anchor_ids = claim.get("paragraph_anchor_ids")
            if (
                not isinstance(anchor_ids, list)
                or not anchor_ids
                or not all(isinstance(value, str) for value in anchor_ids)
                or not set(anchor_ids) <= anchors_by_candidate[candidate_id]
            ):
                raise ValueError(f"claim {index} cites paragraph anchors outside its candidate")
            if focus_anchors_by_candidate[candidate_id] and not (
                set(anchor_ids) & focus_anchors_by_candidate[candidate_id]
            ):
                raise ValueError(f"claim {index} cites no focus paragraph anchor for its candidate")
            claim_id = claim.get("id")
            if selected_ids and claim_id not in selected_ids:
                raise ValueError(f"rewritten claim {index} must retain a selected id")
            normalized.append(
                {
                    "_extraction_key": f"{candidate_id}:{index}",
                    **({"id": claim_id} if isinstance(claim_id, str) else {}),
                    "candidate_id": candidate_id,
                    **{name: str(value).strip() for name, value in fields.items() if isinstance(value, str) and value.strip()},
                    **({"B": baseline.strip()} if isinstance(baseline, str) else {}),
                    **({"U": uncertainty.strip()} if isinstance(uncertainty, str) else {}),
                    "content": claim_content.strip(),
                    "paragraph_anchor_ids": list(dict.fromkeys(anchor_ids)),
                }
            )
        if selected_ids and {item.get("id") for item in normalized} != selected_ids:
            raise ValueError("targeted rewrite must return exactly the selected claim ids")
        normalized.sort(
            key=lambda item: (
                candidate_order[item["candidate_id"]],
                item["paragraph_anchor_ids"],
                *(str(item.get(name, "")).casefold() for name in ("S", "A", "B", "M", "R")),
            )
        )
        return {"status": "claims_extracted", "claims": normalized, "relations": []}


def _latest_formalization(context: StageContext) -> tuple[Any, JSONDict]:
    refs = context.find_all("formalization")
    if len(refs) != 1:
        raise ValueError(f"Step 2 requires exactly one current formalization, found {len(refs)}")
    return refs[0], read_json(context.artifact_path(refs[0]))


def _paper_text(context: StageContext) -> str:
    bundles = context.find_all(INPUT_BUNDLE_KIND)
    if bundles:
        if len(bundles) != 1:
            raise ValueError("Step 2 requires at most one input.bundle")
        value = read_json(context.artifact_path(bundles[0]))
        sources = value.get("sources")
        entries = sources.get(PAPER_TEXT_KIND) if isinstance(sources, dict) else None
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise ValueError("input.bundle must contain exactly one source.paper_text")
        paper = entries[0].get("value")
    else:
        paper_ref = context.require_one(PAPER_TEXT_KIND)
        paper = context.artifact_path(paper_ref).read_text(encoding="utf-8-sig")
    if not isinstance(paper, str) or not paper.strip():
        raise ValueError("source.paper_text value must be non-empty text")
    return paper


def _is_table_block(text: str) -> bool:
    """Return whether a paragraph is a raw table block rather than prose."""
    return bool(_TABLE_BLOCK_PATTERN.search(text))


def _paragraph_image_names(text: str) -> set[str]:
    """Return the casefolded file names of every image a paragraph embeds."""
    names: set[str] = set()
    for match in _IMAGE_PATTERN.finditer(text):
        raw_name = match.group(1).strip("<>")
        path_name = PurePosixPath(raw_name.split("?", 1)[0].split("#", 1)[0]).name
        image_name = path_name or raw_name
        if image_name:
            names.add(image_name.casefold())
    return names


def _table_number(paragraphs: list[JSONDict], index: int, window: int = 2) -> str | None:
    """Resolve a table block's caption number from nearby paragraphs."""
    low = max(0, index - window)
    high = min(len(paragraphs), index + window + 1)
    for candidate_index in range(low, high):
        match = _TABLE_CAPTION_PATTERN.search(paragraphs[candidate_index]["text"])
        if match:
            return match.group(1).lower()
    for candidate_index in range(low, high):
        match = _TABLE_REFERENCE_PATTERN.search(paragraphs[candidate_index]["text"])
        if match:
            return match.group(1).lower()
    return None


def _experiment_candidates(paragraphs: list[JSONDict], radius: int = 1) -> list[JSONDict]:
    images: list[str] = []
    seen_names: set[str] = set()
    for paragraph in paragraphs:
        for name in _paragraph_image_names(paragraph["text"]):
            if name not in seen_names:
                seen_names.add(name)
                images.append(name)
    table_indexes = [index for index, paragraph in enumerate(paragraphs) if _is_table_block(paragraph["text"])]
    image_names_by_index = {
        index: _paragraph_image_names(paragraph["text"])
        for index, paragraph in enumerate(paragraphs)
    }
    image_placeholder_indexes = {index for index, names in image_names_by_index.items() if names}

    def window(indexes: set[int], foreign: set[int]) -> list[int]:
        return sorted({
            neighbor
            for index in indexes
            for neighbor in range(max(0, index - radius), min(len(paragraphs), index + radius + 1))
            if neighbor not in foreign
        })

    def paragraph_items(indexes: list[int]) -> list[JSONDict]:
        return [
            {
                "anchor_id": paragraphs[index]["anchor_id"],
                "tag": paragraphs[index]["tag"],
                "text": paragraphs[index]["text"],
            }
            for index in indexes
        ]

    candidates: list[JSONDict] = []
    # Figure candidates: a figure owns only its own image placeholder; every other
    # embedded figure and every table block is a foreign object and must be excluded.
    for sequence, image_name in enumerate(images, 1):
        stem = PurePosixPath(image_name).stem
        aliases = _figure_aliases(stem) | _figure_aliases(image_name)
        own_image_key = image_name.casefold()
        matches = {
            index
            for index, paragraph in enumerate(paragraphs)
            if any(alias in re.sub(r"[^a-z0-9]", "", paragraph["text"].lower()) for alias in aliases)
        }
        # Keep the local mechanical window, but also retrieve every paragraph
        # in the source that explicitly cites the same Figure/Fig. number.
        # This is deterministic and lets the LLM see result/discussion text
        # that is often far from the image placeholder or caption.
        referenced_numbers = {
            match.group(1).lower()
            for index in matches
            for match in _FIGURE_REFERENCE_PATTERN.finditer(paragraphs[index]["text"])
        }
        if referenced_numbers:
            matches.update(
                index
                for index, paragraph in enumerate(paragraphs)
                if any(
                    match.group(1).lower() in referenced_numbers
                    for match in _FIGURE_REFERENCE_PATTERN.finditer(paragraph["text"])
                )
            )
        foreign = {
            index
            for index in image_placeholder_indexes
            if own_image_key not in image_names_by_index.get(index, set())
        } | set(table_indexes)
        candidates.append(
            {
                "candidate_id": f"image_{sequence:02d}_{re.sub(r'[^a-z0-9]+', '_', stem.lower()).strip('_') or 'unnamed'}",
                "kind": "figure",
                "image_name": image_name,
                "paragraphs": paragraph_items(window(matches, foreign)),
            }
        )

    # Table candidates: a table owns its caption and its own table block; every other
    # table block and every image placeholder is foreign and must be excluded.
    for sequence, table_index in enumerate(table_indexes, 1):
        number = _table_number(paragraphs, table_index)
        label = f"Table {number}" if number else f"Table {sequence}"
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "unnamed"
        matches = {table_index}
        if number:
            matches.update(
                index
                for index, paragraph in enumerate(paragraphs)
                if any(
                    match.group(1).lower() == number
                    for match in _TABLE_REFERENCE_PATTERN.finditer(paragraph["text"])
                )
            )
        foreign = (set(table_indexes) - {table_index}) | image_placeholder_indexes
        candidates.append(
            {
                "candidate_id": f"table_{sequence:02d}_{slug}",
                "kind": "table",
                "table_label": label,
                "paragraphs": paragraph_items(window(matches, foreign)),
            }
        )
    return candidates


def _tool_call(
    context: StageContext,
    candidates: list[JSONDict],
    *,
    call_number: int,
    selected_claims: list[JSONDict] | None = None,
    review_advice: str | None = None,
    frozen_claims: list[JSONDict] | None = None,
    observations_only: bool = False,
    operation: str | None = None,
) -> tuple[JSONDict, ArtifactDraft]:
    spec = context.options.get("tool_plugin")
    if not isinstance(spec, str):
        raise ValueError("Step 2 requires options.tool_plugin in module:object form")
    tool = instantiate(spec)
    if not isinstance(tool, DomainTool):
        raise TypeError(f"{spec} does not implement DomainTool")
    request = ToolCallRequest(
        call_id=f"semantic_step_2_{context.run_id}_{context.attempt}_{call_number}",
        tool_name=tool.name,
        tool_version=tool.version,
        operation=operation or ("extract_observation_claims" if selected_claims is None else "revise_observation_claims"),
        inputs=[
            {"artifact_id": ref.artifact_id, "kind": ref.kind, "sha256": ref.sha256}
            for ref in context.inputs
        ],
        parameters={
            "experiment_candidates": candidates,
            **({"selected_claims": selected_claims} if selected_claims is not None else {}),
            **({"review_advice": review_advice} if review_advice is not None else {}),
            **({"frozen_claims": frozen_claims} if frozen_claims is not None else {}),
            **({"observations_only": True} if observations_only else {}),
        },
    )
    response: ToolCallResponse | None = None
    current_request = request
    for attempt in range(2):
        try:
            response = tool.invoke(current_request)
            if not isinstance(response, ToolCallResponse):
                raise TypeError(f"{spec} returned {type(response).__name__}, not ToolCallResponse")
            validate_tool_response(current_request, response)
        except Exception as exc:
            response = ToolCallResponse(
                current_request.call_id,
                "failed",
                None,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
        if response.status == "succeeded":
            break
        message = str((response.error or {}).get("message", ""))
        # Validation/content failures are actionable prompt violations and
        # should be repaired by the model once. Billing/auth failures are not
        # prompt problems and must not be retried repeatedly.
        if attempt == 0 and message and not re.search(r"HTTP (401|402|403)\b|not configured|Insufficient Balance", message, re.I):
            current_request = ToolCallRequest(
                f"{request.call_id}_repair", request.tool_name, request.tool_version,
                request.operation, request.inputs,
                {**copy.deepcopy(request.parameters), "repair_feedback": message},
            )
            continue
        break
    assert response is not None
    response_path = context.work_dir / f"semantic_step_2_tool_response_{call_number}.json"
    atomic_write_json(response_path, {"request": request.to_dict(), "response": response.to_dict()})
    draft = ArtifactDraft(
        response_path,
        "tool.semantic_review.response",
        "application/json",
        {
            "schema_version": "1.0.0",
            "step": 2,
            "tool_call_id": request.call_id,
            "tool_name": tool.name,
            "tool_version": tool.version,
            "status": response.status,
        },
    )
    if response.status != "succeeded" or not isinstance(response.normalized, dict):
        raise ValueError(str((response.error or {}).get("message", "Step 2 semantic tool failed")))
    return response.normalized, draft


def _vision_call(
    context: StageContext,
    candidate: JSONDict,
    *,
    call_number: int,
) -> tuple[list[JSONDict], ArtifactDraft]:
    """Read a figure after text-only extraction remains evidence-insufficient."""
    tool = DeepSeekFlashVisionTool()
    artifacts = {
        ref.artifact_id: (context.artifact_path(ref), ref.media_type)
        for ref in context.inputs
        if ref.kind == "source.original_figure"
    }
    tool.bind_artifacts(artifacts)
    request = ToolCallRequest(
        call_id=f"semantic_step_2_vision_{context.run_id}_{context.attempt}_{call_number}",
        tool_name=tool.name,
        tool_version=tool.version,
        operation="extract_observation_claims",
        inputs=[{"artifact_id": ref.artifact_id, "kind": ref.kind, "sha256": ref.sha256} for ref in context.inputs],
        parameters={
            "experiment_candidates": [candidate],
            "source_anchor_ids": [item["anchor_id"] for item in candidate.get("paragraphs", []) if isinstance(item, dict)],
        },
    )
    try:
        response = tool.invoke(request)
        if not isinstance(response, ToolCallResponse):
            raise TypeError(f"vision tool returned {type(response).__name__}, not ToolCallResponse")
        validate_tool_response(request, response)
    except Exception as exc:
        response = ToolCallResponse(request.call_id, "failed", None, error={"type": type(exc).__name__, "message": str(exc)})
    response_path = context.work_dir / f"semantic_step_2_vision_response_{call_number}.json"
    atomic_write_json(response_path, {"request": request.to_dict(), "response": response.to_dict()})
    draft = ArtifactDraft(response_path, "tool.semantic_review.response", "application/json", {
        "schema_version": "1.0.0", "step": 2, "tool_call_id": request.call_id,
        "tool_name": tool.name, "tool_version": tool.version, "status": response.status,
    })
    if response.status != "succeeded" or not isinstance(response.normalized, dict):
        raise ValueError(str((response.error or {}).get("message", "Step 2 vision tool failed")))
    knowledge = response.normalized.get("snapshot_patch", {}).get("knowledge", [])
    if not isinstance(knowledge, list):
        return [], draft
    claims: list[JSONDict] = []
    for index, item in enumerate(knowledge):
        if not isinstance(item, dict) or not isinstance(item.get("content"), dict):
            continue
        canonical = item["content"].get("canonical")
        anchors = item.get("source_anchor_ids", [])
        if isinstance(canonical, str) and canonical.strip() and isinstance(anchors, list) and anchors:
            claims.append({
                "content": canonical.strip(),
                "paragraph_anchor_ids": list(dict.fromkeys(str(anchor) for anchor in anchors)),
                "_extraction_key": f"{candidate.get('candidate_id', 'vision')}:{index}",
            })
    return claims, draft


def _vision_candidate(candidate: JSONDict, context: StageContext) -> JSONDict | None:
    """Attach an existing source.original_figure artifact to a text candidate."""
    image_name = str(candidate.get("image_name", "")).casefold()
    refs = context.find_all("source.original_figure")
    def artifact_name(item: Any) -> str:
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        harness = metadata.get("_harness", {})
        return str(metadata.get("source_filename", metadata.get("figure", harness.get("declared_path", ""))))
    ref = next((item for item in refs if artifact_name(item).casefold() == image_name), None)
    if ref is None:
        return None
    paragraphs = list(candidate.get("paragraphs", []))
    return {
        "candidate_id": candidate.get("candidate_id"),
        "figure": {
            "artifact_id": ref.artifact_id,
            "media_type": ref.media_type,
            "label": ref.metadata.get("figure", ref.metadata.get("source_filename", candidate.get("image_name"))),
            # The existing paragraph anchors remain the source-of-truth locators;
            # vision adds no new persisted anchor type.
            "source_anchor_ids": [item["anchor_id"] for item in paragraphs if isinstance(item, dict) and isinstance(item.get("anchor_id"), str)],
        },
        "paragraphs": paragraphs,
    }


def _tool_audit_drafts(context: StageContext) -> list[ArtifactDraft]:
    drafts: list[ArtifactDraft] = []
    for path in sorted(context.work_dir.glob("semantic_step_2_tool_response_*.json")):
        payload = read_json(path)
        request = payload.get("request", {})
        response = payload.get("response", {})
        drafts.append(
            ArtifactDraft(
                path,
                "tool.semantic_review.response",
                "application/json",
                {
                    "schema_version": "1.0.0",
                    "step": 2,
                    "tool_call_id": request.get("call_id", "unknown"),
                    "tool_name": request.get("tool_name", "unknown"),
                    "tool_version": request.get("tool_version", "unknown"),
                    "status": response.get("status", "failed"),
                },
            )
        )
    return drafts


def _extract_with_expansion(
    context: StageContext,
    paragraphs: list[JSONDict],
    document: JSONDict,
    *,
    selected_claims: list[JSONDict] | None = None,
    review_advice: str | None = None,
    frozen_claims: list[JSONDict] | None = None,
    first_call_number: int = 1,
    observations_only: bool = False,
) -> tuple[list[JSONDict], list[JSONDict], list[ArtifactDraft]]:
    drafts: list[ArtifactDraft] = []
    initial_candidates = _experiment_candidates(paragraphs)
    if not initial_candidates:
        return [], [], drafts
    if selected_claims is not None:
        selected_anchor_ids = {
            anchor_id
            for claim in selected_claims
            if isinstance(claim, dict)
            for anchor_id in claim.get("source_anchor_ids", [])
            if isinstance(anchor_id, str)
        }
        initial_candidates = [
            candidate
            for candidate in initial_candidates
            if selected_anchor_ids
            & {
                item["anchor_id"]
                for item in candidate["paragraphs"]
                if isinstance(item, dict) and isinstance(item.get("anchor_id"), str)
            }
        ]
        if not initial_candidates:
            # Figure-only experiments can have claims-file anchors without a
            # paragraph anchor; assess all discovered figure windows instead.
            initial_candidates = _experiment_candidates(paragraphs)
    def process_candidate(
        candidate_index: int, initial_candidate: JSONDict,
    ) -> tuple[list[JSONDict], list[JSONDict], list[ArtifactDraft]]:
        """Process one candidate without sharing mutable result state."""
        candidate_claims: list[JSONDict] = []
        candidate_relations: list[JSONDict] = []
        candidate_drafts: list[ArtifactDraft] = []
        candidate_id = initial_candidate["candidate_id"]
        focus_anchor_ids = [item["anchor_id"] for item in initial_candidate["paragraphs"]]
        previous_signature: tuple[str, ...] | None = None
        candidate_call_start = first_call_number + candidate_index * _CANDIDATE_CALL_SLOTS
        # One initial window plus at most two deterministic expansions.
        extracted_from_text = False
        for radius in range(1, 4):
            candidates = _experiment_candidates(paragraphs, radius)
            candidate = next((item for item in candidates if item["candidate_id"] == candidate_id), None)
            if candidate is None:
                break
            candidate["focus_anchor_ids"] = focus_anchor_ids
            candidate_anchor_ids = [item["anchor_id"] for item in candidate["paragraphs"]]
            candidate = _attach_related_claims(candidate, document)
            signature = tuple(candidate_anchor_ids)
            if signature == previous_signature:
                continue
            previous_signature = signature
            try:
                normalized, _draft = _tool_call(
                    context,
                    [candidate],
                    call_number=candidate_call_start + radius - 1,
                    selected_claims=selected_claims,
                    review_advice=review_advice,
                    frozen_claims=frozen_claims,
                    observations_only=observations_only,
                )
            except Exception:
                break
            if normalized.get("status") == "insufficient_context":
                continue
            if normalized.get("status") != "claims_extracted":
                break
            extracted = normalized.get("claims")
            extracted_relations = normalized.get("relations", [])
            if not isinstance(extracted, list) or not all(isinstance(item, dict) for item in extracted):
                break
            if not isinstance(extracted_relations, list) or not all(isinstance(item, dict) for item in extracted_relations):
                break
            candidate_claims.extend(extracted)
            candidate_relations.extend({**item, "relation_context_id": candidate_id} for item in extracted_relations)
            extracted_from_text = True
            break
        if not extracted_from_text and initial_candidate.get("kind", "figure") != "table":
            # Text-only v4-flash has exhausted the deterministic context
            # expansion. Fall back to the supplied local figure, if any.
            vision_candidate = _vision_candidate(initial_candidate, context)
            if vision_candidate is not None:
                try:
                    visual_claims, visual_draft = _vision_call(
                        context, vision_candidate, call_number=candidate_call_start + 3,
                    )
                    candidate_drafts.append(visual_draft)
                    if visual_claims:
                        candidate_claims.extend(visual_claims)
                        # Vision extraction must use the same relation pass as
                        # text extraction so every visual observation reaches
                        # the observation-to-claim relation stage.
                        expanded_candidates = _experiment_candidates(paragraphs, radius=3)
                        relation_base = next(
                            (item for item in expanded_candidates if item["candidate_id"] == candidate_id),
                            initial_candidate,
                        )
                        relation_candidate = _attach_related_claims(relation_base, document)
                        relation_tool = DeepSeekV4FlashObservationTool()
                        _load_deepseek_env()
                        api_key = os.environ.get("DEEPSEEK_API_KEY")
                        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
                        if not api_key:
                            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
                        visual_relations, relation_responses = relation_tool._extract_relations(
                            relation_candidate,
                            [{"observation_key": item["_extraction_key"],
                              "content": item["content"],
                              "paragraph_anchor_ids": list(item["paragraph_anchor_ids"])}
                             for item in visual_claims],
                            base_url,
                        )
                        candidate_relations.extend({**item, "relation_context_id": candidate_id} for item in visual_relations)
                        if relation_responses:
                            response_path = context.work_dir / f"semantic_step_2_relation_response_{candidate_call_start + 3}.json"
                            atomic_write_json(response_path, {"responses": relation_responses})
                            candidate_drafts.append(ArtifactDraft(response_path, "tool.semantic_review.response", "application/json", {
                                "schema_version": "1.0.0", "step": 2,
                                "tool_name": relation_tool.name, "tool_version": relation_tool.version,
                                "status": "succeeded",
                            }))
                except Exception:
                    # Do not silently discard relation failures.  The runner
                    # must mark the stage failed so the audit identifies an
                    # API/normalization problem instead of emitting observation
                    # claims without their relation pass.
                    raise
        return candidate_claims, candidate_relations, candidate_drafts

    # Candidate contexts are independent.  Keep a small fixed pool because
    # the configured provider may rate-limit large bursts of requests.
    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_CANDIDATES, len(initial_candidates))) as executor:
        results = list(executor.map(
            lambda item: process_candidate(*item),
            enumerate(initial_candidates),
        ))
    claims: list[JSONDict] = []
    relations: list[JSONDict] = []
    drafts: list[ArtifactDraft] = []
    for candidate_claims, candidate_relations, candidate_drafts in results:
        claims.extend(candidate_claims)
        relations.extend(candidate_relations)
        drafts.extend(candidate_drafts)
    # Reconstruct the audit list from disk so failed calls are retained even
    # though _tool_call raised before returning its draft.
    claims, relations = _deduplicate_extractions(claims, relations)
    return claims, relations, []


def _observation_items(claims: list[JSONDict], existing: dict[str, JSONDict] | None = None) -> tuple[dict[str, JSONDict], dict[str, str]]:
    existing = existing or {}
    existing_observations = {
        key: value for key, value in existing.items() if value.get("type") == "observation_claim"
    }
    used_existing: set[str] = set()
    new_counter = 0

    def match_existing(claim: JSONDict) -> str | None:
        tokens = _tokens(str(claim.get("content", "")))
        if not tokens:
            return None
        best_key: str | None = None
        best_score = 0.0
        for key, value in existing_observations.items():
            if key in used_existing:
                continue
            canonical = str(value.get("content", {}).get("canonical", ""))
            score = _cosine(tokens, _tokens(canonical))
            if score > best_score:
                best_score = score
                best_key = key
        if best_key is not None and best_score >= 0.85:
            used_existing.add(best_key)
            return best_key
        return None

    ids: dict[str, str] = {}
    observations: dict[str, JSONDict] = {}
    for index, claim in enumerate(claims, 1):
        claim_id = claim.get("id") if isinstance(claim.get("id"), str) else None
        extraction_key = str(claim.get("_extraction_key", claim_id or f"generated:{index}"))
        if claim_id:
            stable = claim_id
        else:
            matched = match_existing(claim)
            stable = matched if matched is not None else f"claim_O{new_counter + 1:02d}"
            if matched is None:
                new_counter += 1
        ids[extraction_key] = stable
        if stable not in observations:
            content = {"canonical": claim["content"]}
            for field in _OBSERVATION_COMPONENT_FIELDS:
                value = claim.get(field)
                if isinstance(value, str) and value.strip():
                    content[field] = value.strip()
            observations[stable] = {
                "type": "observation_claim",
                "content": content,
                "source_anchor_ids": list(claim.get("paragraph_anchor_ids", [])),
            }
    return observations, ids


_OBSERVATION_COMPONENT_FIELDS = ("S", "A", "B", "M", "R", "U")
_NUMBER_TOKEN_PATTERN = re.compile(r"\d+(?:\.\d+)?")

_JUDGE_DUPLICATES_PROMPT = (
    "You are comparing pairs of experimental observation claims extracted from the same paper. "
    "Each claim has free text plus optional structured components: "
    "S=setting/scope, A=treatment/method/object, B=baseline, M=measure, R=result, U=uncertainty. "
    "For every supplied pair, decide whether the two claims describe the SAME experimental observation "
    "(same object/dataset + same measure + same numerical or directional result), even when wording differs. "
    "Rules: "
    "1) same object (A) + same measure (M) + same result values (R) => duplicate; "
    "2) different object/dataset/model, OR different measure, OR different result values => NOT duplicate, "
    "even when the sentence template is highly similar; "
    "3) extra detail such as a possible cause or a qualifier does NOT make a claim different when A, M, and R match; "
    "4) never merge claims about different datasets (e.g. Cora-Link vs Cora-Node vs Proteins). "
    "Return JSON only: {\"merge\": [[\"keep_id\", \"drop_id\"], ...]}. "
    "Use only the supplied ids; omit non-duplicate pairs. If none are duplicates, return {\"merge\": []}.\n"
)


def _observation_fingerprint(node: JSONDict) -> dict[str, Any]:
    content = node.get("content") or {}
    canonical = re.sub(r"\s+", " ", str(content.get("canonical", ""))).strip().casefold()
    return {
        "anchors": frozenset(node.get("source_anchor_ids", [])),
        "numbers": frozenset(_NUMBER_TOKEN_PATTERN.findall(canonical)),
        "tokens": _tokens(canonical),
    }


def _judge_duplicate_observations(
    context: StageContext,
    observations: dict[str, JSONDict],
    candidate_pairs: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Ask the model to confirm which candidate pairs are the same observation."""
    _load_deepseek_env()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return []
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    payload_pairs = []
    for left, right in candidate_pairs:
        left_node = observations[left]
        right_node = observations[right]
        left_content = left_node.get("content") or {}
        right_content = right_node.get("content") or {}
        payload_pairs.append({
            "a": {
                "id": left,
                "text": left_content.get("canonical", ""),
                "components": {field: left_content[field] for field in _OBSERVATION_COMPONENT_FIELDS if left_content.get(field)},
                "anchors": left_node.get("source_anchor_ids", []),
            },
            "b": {
                "id": right,
                "text": right_content.get("canonical", ""),
                "components": {field: right_content[field] for field in _OBSERVATION_COMPONENT_FIELDS if right_content.get(field)},
                "anchors": right_node.get("source_anchor_ids", []),
            },
        })
    prompt = _JUDGE_DUPLICATES_PROMPT + json.dumps({"pairs": payload_pairs}, ensure_ascii=False)
    http_request = Request(
        f"{base}/chat/completions",
        data=json.dumps({
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urlopen_with_retry(http_request, timeout=180) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return []
    try:
        result = json.loads(_response_content(raw))
    except (json.JSONDecodeError, ValueError):
        return []
    merge = result.get("merge")
    if not isinstance(merge, list):
        return []
    validated: list[tuple[str, str]] = []
    for pair in merge:
        if (
            isinstance(pair, list) and len(pair) == 2
            and all(isinstance(item, str) and item in observations for item in pair)
            and pair[0] != pair[1]
        ):
            validated.append((pair[0], pair[1]))
    atomic_write_json(
        context.work_dir / "semantic_step_2_dedup_judge_response.json",
        {"candidate_pairs": candidate_pairs, "response": result, "merge": validated},
    )
    return validated


def _deduplicate_observations_judge(
    context: StageContext,
    observations: dict[str, JSONDict],
    extraction_to_observation: dict[str, str],
) -> tuple[dict[str, JSONDict], dict[str, str]]:
    """Merge same-observation duplicates via an LLM judge (semantic, not lexical)."""
    if len(observations) < 2:
        return observations, extraction_to_observation
    fingerprints = {key: _observation_fingerprint(node) for key, node in observations.items()}
    keys = sorted(observations)
    candidate_pairs: list[tuple[str, str]] = []
    for index, left in enumerate(keys):
        left_fp = fingerprints[left]
        if not left_fp["anchors"]:
            continue
        for right in keys[index + 1:]:
            right_fp = fingerprints[right]
            if not (left_fp["anchors"] & right_fp["anchors"]):
                continue
            share_number = bool(left_fp["numbers"] & right_fp["numbers"])
            lexically_close = _cosine(left_fp["tokens"], right_fp["tokens"]) >= 0.6
            if share_number or lexically_close:
                candidate_pairs.append((left, right))
    if not candidate_pairs:
        return observations, extraction_to_observation
    merge = _judge_duplicate_observations(context, observations, candidate_pairs)
    if not merge:
        return observations, extraction_to_observation
    remap: dict[str, str] = {}
    for keep, drop in merge:
        if keep in observations and drop in observations and drop not in remap:
            remap[drop] = keep
    # Resolve transitive remaps (B->A, C->B  =>  C->A) before deleting nodes;
    # drop any cycle the model may have emitted so merging stays acyclic.
    resolved: dict[str, str] = {}
    for drop, keep in remap.items():
        root = keep
        seen = {drop}
        while root in remap and root not in seen:
            seen.add(root)
            root = remap[root]
        if root in seen or root == drop:
            continue
        resolved[drop] = root
    remap = resolved
    if not remap:
        return observations, extraction_to_observation
    merged = dict(observations)
    for drop, keep in remap.items():
        if drop not in merged:
            continue
        merged[keep]["source_anchor_ids"] = list(dict.fromkeys(
            [*merged[keep].get("source_anchor_ids", []),
             *merged[drop].get("source_anchor_ids", [])]
        ))
        del merged[drop]
    remapped_ids = {key: remap.get(value, value) for key, value in extraction_to_observation.items()}
    return merged, remapped_ids


def _observation_binding(
    refined_observations: list[JSONDict],
    observation_ids: dict[str, str],
    existing: dict[str, JSONDict] | None = None,
) -> tuple[dict[str, JSONDict], dict[str, str]]:
    """Bind refined S/A/B/M/R onto the single experiment node for each observation.

    Stage 1 deleted the phenomenon claim E: an observation claim O *is* the
    observation proposition.  Binding therefore writes the refined content and
    S/A/B/M/R back onto the node selected by document priority (reuse the
    original imported claim ID, otherwise claim_Ox) and merges the observation
    anchors.  It never creates a parallel phenomenon node or an equivalence
    operator.
    """
    existing = existing or {}
    by_key: dict[str, JSONDict] = {}
    for item in refined_observations:
        key = item.get("observation_key")
        if (not isinstance(key, str) or key not in observation_ids or key in by_key
                or not isinstance(item.get("content"), str) or not item["content"].strip()
                or not isinstance(item.get("paragraph_anchor_ids"), list)
                or not item["paragraph_anchor_ids"]
                or len(item["paragraph_anchor_ids"]) != len(set(item["paragraph_anchor_ids"]))):
            # Keep every valid observation even when one refinement is bad.
            continue
        by_key[key] = item
    bound_nodes: dict[str, JSONDict] = {}
    bound_ids: dict[str, str] = {}
    for key, observation_id in observation_ids.items():
        if key not in by_key:
            continue
        item = by_key[key]
        node = existing.get(observation_id)
        node = node if isinstance(node, dict) else {}
        content = dict(node.get("content") or {})
        content["canonical"] = item["content"]
        for field in _OBSERVATION_COMPONENT_FIELDS:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                content[field] = value.strip()
        merged_anchors = list(dict.fromkeys([
            *node.get("source_anchor_ids", []),
            *item["paragraph_anchor_ids"],
        ]))
        bound_nodes[observation_id] = {
            **node,
            "type": "observation_claim",
            "content": content,
            "source_anchor_ids": merged_anchors,
        }
        bound_ids[key] = observation_id
    return bound_nodes, bound_ids


def _relation_items(
    relations: list[JSONDict], document: JSONDict,
) -> tuple[list[JSONDict], list[str]]:
    """Turn observation-to-target relation proposals into non-reasoning links."""
    links = list(document["workflow"]["non_reasoning_links"])
    existing = {link["id"] for link in links}
    endpoint_keys: set[tuple[tuple[str, ...], str]] = {
        (tuple(sorted(str(item) for item in link.get("sources", []))), str(link.get("target", "")))
        for link in links
    }
    rejected: list[str] = []
    for index, relation in enumerate(relations, 1):
        observation_keys = relation.get("observation_keys")
        claim_id = relation.get("claim_id")
        if not isinstance(observation_keys, list) or not observation_keys or not isinstance(claim_id, str):
            rejected.append(f"relation_step2_{index}: invalid endpoints")
            continue
        if any(
            not isinstance(key, str) or not key or key not in document["knowledges"]
            for key in observation_keys
        ) or claim_id not in document["knowledges"]:
            rejected.append(f"relation_step2_{index}: unknown observation or claim")
            continue
        link_id = f"relation_step2_{index}"
        if link_id in existing:
            rejected.append(f"{link_id}: duplicate relation id")
            continue
        expression = relation["expression"]
        relation_context_id = relation.get("relation_context_id")
        if not isinstance(relation_context_id, str) or not relation_context_id:
            rejected.append(f"{link_id}: missing relation_context_id")
            continue
        for key in observation_keys:
            expression = expression.replace(f"[O:{key}]", f"[{key}]")
        if "[O:" in expression:
            rejected.append(f"{link_id}: unknown observation in expression")
            continue
        endpoint_key = (tuple(sorted(observation_keys)), claim_id)
        if endpoint_key in endpoint_keys:
            # Overlapping windows may yield different wording for the same
            # endpoints. Keep the first deterministic relation and discard
            # only the duplicate, never a distinct endpoint pair.
            continue
        links.append({
            "id": link_id, "link_type": "imported_relation", "sources": list(observation_keys), "target": claim_id,
            "reasoning": False, "metadata": {"relation": {
                "expression": expression, "relation_context_id": relation_context_id,
            }},
        })
        existing.add(link_id)
        endpoint_keys.add(endpoint_key)
    return links, rejected


def _rewrite_imported_observations(
    context: StageContext,
    imported: list[JSONDict],
    paragraphs: list[JSONDict],
) -> tuple[list[JSONDict], list[ArtifactDraft]]:
    """Rewrite imported observation claims into S/A/B/M/R form, retaining ids, without E."""
    if not imported:
        return [], []
    anchor_text = {
        item["anchor_id"]: item["text"]
        for item in paragraphs
        if isinstance(item, dict) and isinstance(item.get("anchor_id"), str)
    }
    selected = []
    for item in imported:
        excerpts = [
            f"[{anchor_id}] {anchor_text[anchor_id]}"
            for anchor_id in item.get("source_anchor_ids", [])
            if isinstance(anchor_id, str) and anchor_id in anchor_text
        ]
        selected.append({
            "id": item["id"],
            "content": item["content"],
            "source_anchor_ids": list(item.get("source_anchor_ids", [])),
            "source_excerpts": excerpts,
        })
    candidates = _experiment_candidates(paragraphs)
    try:
        normalized, draft = _tool_call(
            context,
            candidates,
            call_number=1,
            selected_claims=selected,
            operation="rewrite_observations",
        )
    except Exception:
        return [], []
    claims = normalized.get("claims", [])
    if not isinstance(claims, list) or not all(isinstance(item, dict) for item in claims):
        raise ValueError("rewrite_observations returned invalid claims")
    return list(claims), [draft]


def _extract_relations_pass(
    context: StageContext,
    paragraphs: list[JSONDict],
    document: JSONDict,
    observations: list[JSONDict],
    observation_ids: dict[str, str],
    *,
    call_number: int,
) -> tuple[list[JSONDict], list[ArtifactDraft]]:
    """Extract target-centered relations for surviving observation nodes, per figure window."""
    if not observations:
        return [], []
    by_stable = {
        item["observation_key"]: item
        for item in observations
        if isinstance(item, dict) and isinstance(item.get("observation_key"), str)
    }
    candidate_to_stable: dict[str, list[str]] = {}
    for extraction_key, stable in observation_ids.items():
        if ":" not in extraction_key:
            continue
        candidate_id = extraction_key.rsplit(":", 1)[0]
        candidate_to_stable.setdefault(candidate_id, []).append(stable)
    tool = instantiate(context.options["tool_plugin"])
    _load_deepseek_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    relations: list[JSONDict] = []
    drafts: list[ArtifactDraft] = []
    for index, candidate in enumerate(_experiment_candidates(paragraphs)):
        candidate = _attach_related_claims(candidate, document)
        stable_ids = candidate_to_stable.get(candidate["candidate_id"], [])
        candidate_observations = [by_stable[stable] for stable in stable_ids if stable in by_stable]
        if not candidate_observations:
            continue
        try:
            candidate_relations, raw_responses = tool._extract_relations(candidate, candidate_observations, base_url)
        except Exception:
            continue
        relations.extend({**item, "relation_context_id": candidate["candidate_id"]} for item in candidate_relations)
        if raw_responses:
            response_path = context.work_dir / f"semantic_step_2_relation_response_{call_number + index}.json"
            atomic_write_json(response_path, {"responses": raw_responses})
            drafts.append(ArtifactDraft(response_path, "tool.semantic_review.response", "application/json", {
                "schema_version": "1.0.0", "step": 2, "tool_call_id": "relations",
                "tool_name": tool.name, "tool_version": tool.version, "status": "succeeded",
            }))
    return relations, drafts


def _revision(
    document: JSONDict,
    context: StageContext,
    *,
    revision_id: str,
    review_status: str,
    modified: list[str],
) -> None:
    previous_revision = document["revision"]
    document["revision"] = {
        "revision_id": revision_id,
        "supersedes": previous_revision.get("revision_id"),
        "parent_hash": previous_revision.get("content_hash"),
        "content_hash": "",
    }
    # Artifact identity plus revision.parent_hash is the history chain.  The
    # schema-1.1 field remains present only as an empty compatibility field.
    document["workflow"]["revisions"] = []
    document["revision"]["content_hash"] = content_hash(document)


def _with_extra_artifacts(result: StageResult, drafts: list[ArtifactDraft]) -> StageResult:
    return StageResult(
        result.status,
        [*result.artifacts, *drafts],
        list(result.findings),
        dict(result.metadata),
    )


class Step2ExtractObservationsPlugin:
    """Mechanically anchor Knowledge, extract observations, and write relations."""

    def run(self, context: StageContext) -> StageResult:
        try:
            _, source = _latest_formalization(context)
            document = copy.deepcopy(source)
            paper = _paper_text(context)
            paragraphs = _paper_paragraphs(paper.splitlines())
            modified: list[str] = []

            # 1. Mechanically anchor all knowledge nodes (including imported observations).
            for knowledge_id, knowledge in document["knowledges"].items():
                best = _best_paragraph_anchor(knowledge["content"]["canonical"], paragraphs)
                if best is None:
                    continue
                source_ids = knowledge.setdefault("source_anchor_ids", [])
                if best["anchor_id"] not in source_ids:
                    source_ids.append(best["anchor_id"])
                    modified.append(knowledge_id)

            imported_observation_claims = [
                {"id": key, "content": value["content"]["canonical"],
                 "source_anchor_ids": list(value.get("source_anchor_ids", []))}
                for key, value in document["knowledges"].items()
                if value.get("type") == "observation_claim"
            ]
            drafts: list[ArtifactDraft] = []

            # 2. Rewrite imported observation claims into S/A/B/M/R form (in memory).
            rewritten_imported: list[JSONDict] = []
            if imported_observation_claims:
                rewritten, rewrite_drafts = _rewrite_imported_observations(
                    context, imported_observation_claims, paragraphs,
                )
                drafts.extend(rewrite_drafts)
                rewritten_imported = [
                    item for item in rewritten
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                    and item["id"] in document["knowledges"]
                ]

            # 3. Retrieve the full text for observation claims missed by Step 1.
            new_claims, _, extract_drafts = _extract_with_expansion(
                context, paragraphs, document,
                selected_claims=None,
                observations_only=True,
                first_call_number=2 if imported_observation_claims else 1,
            )
            drafts.extend(extract_drafts)

            # 4. Deduplicate new observations against imported ones (content-based, imported priority).
            observations, extraction_to_observation = _observation_items(new_claims, document["knowledges"])

            # 4.5 Judge-merge same-observation duplicates among the newly extracted set.
            observations, extraction_to_observation = _deduplicate_observations_judge(
                context, observations, extraction_to_observation,
            )

            # 5. Bind the refined observation proposition onto its one experiment
            #    node: reuse the original imported claim ID when it exists,
            #    otherwise keep the newly discovered claim_Ox ID.  No phenomenon
            #    claim E and no equivalence operator is created.
            refined = [
                {
                    "observation_key": item["id"],
                    "content": item["content"],
                    "paragraph_anchor_ids": list(item.get("paragraph_anchor_ids", [])),
                    **{
                        field: item[field]
                        for field in _OBSERVATION_COMPONENT_FIELDS
                        if isinstance(item.get(field), str) and item[field].strip()
                    },
                }
                for item in rewritten_imported
            ]
            refined.extend(
                {
                    "observation_key": knowledge_id,
                    "content": observations[knowledge_id]["content"]["canonical"],
                    "paragraph_anchor_ids": list(observations[knowledge_id].get("source_anchor_ids", [])),
                    **{
                        field: observations[knowledge_id]["content"][field]
                        for field in _OBSERVATION_COMPONENT_FIELDS
                        if isinstance(observations[knowledge_id]["content"].get(field), str)
                    },
                }
                for knowledge_id in observations
            )
            bound_nodes, _binding_ids = _observation_binding(
                refined,
                {item["observation_key"]: item["observation_key"] for item in refined},
                document["knowledges"],
            )
            for knowledge_id, node in bound_nodes.items():
                document["knowledges"][knowledge_id] = node
                if knowledge_id not in document["graph"]["nodes"]:
                    document["graph"]["nodes"].append(knowledge_id)
                modified.append(knowledge_id)

            # 6. Extract target-centered relations for the surviving observations.
            observation_items = [
                {
                    "observation_key": knowledge_id,
                    "content": node["content"]["canonical"],
                    "paragraph_anchor_ids": list(node.get("source_anchor_ids", [])),
                }
                for knowledge_id, node in bound_nodes.items()
            ]
            relations, relation_drafts = _extract_relations_pass(
                context, paragraphs, document, observation_items, extraction_to_observation,
                call_number=3 if imported_observation_claims else 2,
            )
            drafts.extend(relation_drafts)

            # 7. Persist the observation-to-target relations.
            document["workflow"]["non_reasoning_links"], rejected_relations = _relation_items(relations, document)
            findings = [Finding("STEP2_REJECTED_RELATION", "warning", message) for message in rejected_relations]
            _revision(
                document,
                context,
                revision_id=f"revision_{context.run_id}_step_2",
                review_status="automated",
                modified=list(dict.fromkeys(modified)),
            )
            emitted = emit_formalization(context, document, step=2, step_name=STEP_NAME)
            drafts.extend(_tool_audit_drafts(context))
            return StageResult(emitted.status, [*emitted.artifacts, *drafts], [*findings, *emitted.findings], emitted.metadata)
        except Exception as exc:
            return StageResult(
                "failed",
                artifacts=_tool_audit_drafts(context),
                findings=[Finding("STEP2_EXTRACTION_FAILED", "error", str(exc))],
                metadata={"step": 2},
            )
