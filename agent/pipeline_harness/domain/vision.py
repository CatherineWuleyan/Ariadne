"""Step 2 visual evidence reader using the configured DeepSeek vision endpoint."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..models import JSONDict
from .tools import ToolCallRequest, ToolCallResponse


def _load_local_deepseek_env() -> None:
    """Load only the two supported local settings without logging their values."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() not in {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"}:
            continue
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class DeepSeekFlashVisionTool:
    """Extract reviewable S/A/B/M/R/U observation proposals in Step 2."""

    name = "deepseek-v4-flash-vision-exp"
    version = "1"
    model = "deepseek-v4-flash-vision-exp"

    def __init__(self) -> None:
        self._artifacts: dict[str, tuple[Path, str]] = {}

    def bind_artifacts(self, artifacts: Mapping[str, tuple[Path, str]]) -> None:
        self._artifacts = dict(artifacts)

    def invoke(self, request: ToolCallRequest) -> ToolCallResponse:
        _load_local_deepseek_env()
        candidates = request.parameters.get("experiment_candidates", [])
        if not isinstance(candidates, list):
            raise ValueError("experiment_candidates must be a list")
        if not candidates:
            return ToolCallResponse(
                request.call_id, "succeeded", {"skipped": "no_figure_references"},
                {"snapshot_patch": {}}, metadata={"candidate_count": 0},
            )
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        content: list[JSONDict] = [{"type": "text", "text": (
            "You are a scientific figure reader. Use only the supplied figures and cited paper paragraphs. "
            "Do not infer facts not visible in the evidence. If the figure is a schematic or contains no measured result, "
            "return {\"status\":\"insufficient_context\",\"needed_context\":\"no measured experimental result is visible\",\"claims\":[]}. "
            "Otherwise return one JSON object aggregating every supported observation claim."
        )}]
        for candidate in candidates:
            content.extend(self._candidate_content(candidate))
        messages = [{"role": "user", "content": content}]
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0, "response_format": {"type": "json_object"}}, ensure_ascii=False).encode("utf-8")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        http_request = Request(
            f"{base_url}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(http_request, timeout=180) as response:  # noqa: S310 -- configured API endpoint
                raw: Any = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"DeepSeek vision request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"DeepSeek vision request failed: {exc.reason}") from exc
        return ToolCallResponse(
            request.call_id, "succeeded", raw, self._normalize(raw, request, candidates),
            metadata={"candidate_count": len(candidates), "model": self.model},
        )

    def _normalize(self, raw: Any, request: ToolCallRequest, candidates: list[JSONDict]) -> JSONDict:
        content = self._response_content(raw)
        try:
            result = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("DeepSeek vision response is not valid JSON") from exc
        if not isinstance(result, dict):
            raise ValueError("DeepSeek vision response must be a JSON object")
        paragraph_order = {
            anchor_id: index
            for index, anchor_id in enumerate(request.parameters.get("source_anchor_ids", []))
            if isinstance(anchor_id, str)
        }
        allowed_paragraphs = set(paragraph_order)
        figure_order = {
            anchor_id: index
            for index, candidate in enumerate(candidates)
            for anchor_id in candidate.get("figure", {}).get("source_anchor_ids", [])
            if isinstance(anchor_id, str)
        }
        figure_anchors = {
            anchor_id
            for candidate in candidates
            for anchor_id in candidate.get("figure", {}).get("source_anchor_ids", [])
            if isinstance(anchor_id, str)
        }
        status = result.get("status")
        claims = result.get("claims", [])
        if status == "insufficient_context" or not claims:
            needed = result.get("needed_context", "The selected paragraph window did not support an experiment claim.")
            return {"snapshot_patch": {"review": {"status": "needs_review", "issues": [{
                "code": "EXPERIMENT_CONTEXT_INSUFFICIENT",
                "message": str(needed),
                "action": "Retain this evidence-insufficient audit result and skip the candidate without retrying.",
            }]}}}
        if status != "claims_extracted" or not isinstance(claims, list):
            raise ValueError("DeepSeek vision response must declare claims_extracted with a claims array")
        normalized_claims: list[tuple[dict[str, str], str, list[str], list[str]]] = []
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                raise ValueError(f"claim {index} must be an object")
            fields = {name: claim.get(name) for name in ("S", "A", "B", "M", "R", "U")}
            if not all(isinstance(fields[name], str) and fields[name].strip() for name in ("S", "A", "M", "R")):
                raise ValueError(f"claim {index} must provide non-empty S/A/M/R strings")
            for optional_name in ("B", "U"):
                value = fields[optional_name]
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    raise ValueError(f"claim {index} optional {optional_name} must be a non-empty string when present")
            canonical = claim.get("content")
            if not isinstance(canonical, str) or not canonical.strip():
                raise ValueError(f"claim {index} must provide one fluent content sentence")
            paragraph_ids = claim.get("paragraph_anchor_ids", [])
            figure_ids = claim.get("figure_anchor_ids", [])
            if not isinstance(paragraph_ids, list) or not paragraph_ids or not all(isinstance(value, str) for value in paragraph_ids):
                raise ValueError(f"claim {index} must cite one or more paragraph anchors")
            if not isinstance(figure_ids, list) or not figure_ids or not all(isinstance(value, str) for value in figure_ids):
                return {"snapshot_patch": {"review": {"status": "needs_review", "issues": [{"code": "EXPERIMENT_CONTEXT_INSUFFICIENT", "message": "vision response produced a claim without figure anchors", "action": "Skip this figure candidate and retain the evidence-insufficient audit result."}]}}}
            if not set(paragraph_ids) <= allowed_paragraphs or not set(figure_ids) <= figure_anchors:
                raise ValueError(f"claim {index} cites anchors outside the selected evidence")
            normalized_claims.append((fields, canonical.strip(), paragraph_ids, figure_ids))
        normalized_claims.sort(key=lambda item: (
            min((figure_order[anchor_id] for anchor_id in item[3]), default=len(figure_order)),
            min((paragraph_order[anchor_id] for anchor_id in item[2]), default=len(paragraph_order)),
            *(str(item[0].get(name, "")).casefold() for name in ("S", "A", "B", "M", "R")),
        ))
        knowledge: list[JSONDict] = []
        for number, (_fields, canonical, paragraph_ids, figure_ids) in enumerate(normalized_claims, 1):
            knowledge.append({
                "id": f"claim_O{number:02d}",
                "type": "claim",
                "content": {"canonical": canonical}, "self_contained": True, "origin": "extracted",
                "visibility": "public", "source_anchor_ids": list(dict.fromkeys([*paragraph_ids, *figure_ids])),
                "external_ids": [], "epistemic": {"prior_status": "unset", "prior_ref": None},
                "first_seen_step": 2, "metadata": {},
            })
        return {"snapshot_patch": {
            "knowledge": knowledge,
            "review": {"status": "needs_review", "issues": [{
                "code": "OBSERVATION_PROPOSALS_PENDING_REVIEW",
                "message": "S/A/B/M/R/U observation proposals were extracted from the selected visual evidence.",
                "knowledge_ids": [item["id"] for item in knowledge],
            }]},
        }}

    @staticmethod
    def _response_content(raw: Any) -> str:
        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("DeepSeek vision response has no choices[0].message.content") from exc
        if not isinstance(content, str):
            raise ValueError("DeepSeek vision response content must be a string")
        return content

    def _candidate_content(self, candidate: JSONDict) -> list[JSONDict]:
        figure = candidate.get("figure")
        paragraphs = candidate.get("paragraphs")
        if not isinstance(figure, dict) or not isinstance(paragraphs, list):
            raise ValueError("experiment candidate requires figure and paragraphs")
        artifact_id = figure.get("artifact_id")
        if not isinstance(artifact_id, str) or artifact_id not in self._artifacts:
            raise ValueError(f"candidate figure artifact is unavailable: {artifact_id}")
        path, media_type = self._artifacts[artifact_id]
        if media_type not in {"image/jpeg", "image/png"}:
            raise ValueError(f"DeepSeek vision supports JPEG/PNG figures, got {media_type}")
        image_data = base64.b64encode(path.read_bytes()).decode("ascii")
        paragraph_text = "\n\n".join(
            f"[{item.get('anchor_id')}] {item.get('text')}" for item in paragraphs if isinstance(item, dict)
        )
        instruction = (
            f"Candidate figure: {figure.get('label') or artifact_id}. Read the image together with the cited paragraphs below. "
            "Extract complete experiment claims using these field definitions: "
            "S (Setting) is the experimental setting, scope, or range, including the dataset or split, model or architecture, "
            "pruning fraction or range, and other evaluation conditions that distinguish one comparison from another. "
            "A (Action) is the treatment, intervention, method, or experimental object being evaluated; write it as a concise noun phrase without the comparison result. "
            "B (Baseline) is the baseline or control being compared against; write it as a concise noun phrase without words such as 'compared with' or 'to'. "
            "M (Measure) is the measured metric; write it as a concise metric noun phrase. "
            "R (Result) is only the observed numerical or directional outcome of comparing A with B on M; do not put settings, ranges, or evaluation conditions in R. "
            "U (Uncertainty) is explicitly reported uncertainty information, such as random seeds, sample count, confidence intervals, or mean plus or minus standard deviation. "
            "Use English for every supplied S/A/B/M/R/U value. Also write content as one fluent, self-contained English sentence. "
            "Choose the sentence's grammar, word order, subject, and clause structure naturally from the evidence; do not concatenate fields into a fixed template. "
            "You may rephrase the evidence for grammar and fluency, "
            "but must not add, remove, generalize, narrow, reverse, combine, or otherwise change its meaning. Preserve all material entities, conditions, "
            "comparison directions, metrics, numerical values, and qualifiers. If the selected paper context is insufficient to extract a complete "
            "experiment claim, return status insufficient_context rather than guessing: "
            "{\"status\":\"insufficient_context\",\"needed_context\":\"...\"}. Otherwise return "
            "{\"status\":\"claims_extracted\",\"claims\":[{\"S\":\"...\",\"A\":\"...\",\"M\":\"...\","
            "\"R\":\"...\",\"content\":\"...\",\"paragraph_anchor_ids\":[\"...\"],"
            "\"figure_anchor_ids\":[\"...\"]}]}. Within one figure, emit one claim per self-contained experimental finding the "
            "paper reports, not per measurement. Group every number that belongs to a single reported finding into one claim. "
            "Produce a separate claim only when the paper states a genuinely distinct finding: a materially different comparison, "
            "direction, or conclusion, whose meaning would be lost by merging. Never generate a Cartesian product of treatments, "
            "conditions, datasets, or metrics. A change in pruning fraction or range is a change in setting; it justifies a "
            "separate claim only when the paper also reports a distinct result or conclusion for it. Do not split solely for "
            "uncertainty differences. "
            f"Allowed paragraph anchors: {[item.get('anchor_id') for item in paragraphs if isinstance(item, dict)]}. "
            f"Allowed figure anchors: {figure.get('source_anchor_ids', [])}. Cite only those anchors. "
            "B and U are optional: omit either field when it is not explicitly reported, and never write an absence placeholder such as "
            "'uncertainty not reported'. Do not infer facts absent from the image or paragraphs.\n\n"
            "Paragraph evidence:\n" + paragraph_text
        )
        return [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_data}"}},
        ]
