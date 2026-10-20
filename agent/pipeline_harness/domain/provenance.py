from __future__ import annotations

import json
from typing import Any, Mapping

from ..models import JSONDict
from ..plugins import StageContext
from .contracts import canonical_hash
from .validation import make_validation_finding


def _resolve_json_pointer(value: Any, pointer: str) -> Any:
    current = value
    for raw_part in pointer.lstrip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise ValueError(f"JSON pointer cannot traverse {part}")
    return current


def collect_runtime_anchor_findings(context: StageContext, payload: Mapping[str, Any]) -> list[JSONDict]:
    refs = {ref.artifact_id: ref for ref in context.inputs}
    json_cache: dict[str, Any] = {}
    text_cache: dict[str, list[str]] = {}
    findings: list[JSONDict] = []
    anchors = payload.get("source_anchors", [])
    if not isinstance(anchors, list):
        return findings
    for index, anchor in enumerate(anchors):
        if not isinstance(anchor, dict):
            continue
        anchor_id = str(anchor.get("anchor_id", f"source_anchor[{index}]"))
        artifact_id = anchor.get("artifact_id")
        ref = refs.get(artifact_id) if isinstance(artifact_id, str) else None
        pointer = f"/source_anchors/{index}"
        if ref is None:
            findings.append(make_validation_finding(
                "source_anchor.artifact_available", "error", "source_anchor", anchor_id,
                f"Source artifact {artifact_id} is unavailable in this stage", f"{pointer}/artifact_id",
                "Import the source through the input manifest and preserve its ArtifactRef.",
            ))
            continue
        locator = anchor.get("locator")
        if not isinstance(locator, dict):
            continue
        artifact_path = context.artifact_path(ref)
        try:
            if locator.get("type") == "json_pointer":
                if ref.artifact_id not in json_cache:
                    with artifact_path.open("r", encoding="utf-8-sig") as handle:
                        json_cache[ref.artifact_id] = json.load(handle)
                _resolve_json_pointer(json_cache[ref.artifact_id], str(locator.get("pointer", "")))
            elif locator.get("type") == "markdown_span":
                if ref.artifact_id not in text_cache:
                    text_cache[ref.artifact_id] = artifact_path.read_text(encoding="utf-8").splitlines()
                lines = text_cache[ref.artifact_id]
                start, end = locator.get("start_line"), locator.get("end_line")
                if not isinstance(start, int) or not isinstance(end, int) or end > len(lines):
                    raise ValueError("line span exceeds the source document")
                expected_hash = locator.get("quote_sha256")
                actual_hash = canonical_hash(" ".join(line.strip() for line in lines[start - 1:end]))
                if expected_hash is not None and actual_hash != expected_hash:
                    raise ValueError("quote hash is stale")
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            findings.append(make_validation_finding(
                "source_anchor.resolves", "error", "source_anchor", anchor_id,
                f"Source anchor does not resolve: {exc}", f"{pointer}/locator",
                "Repair the locator or re-import the referenced source.",
            ))
    return findings
