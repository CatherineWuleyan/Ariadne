from __future__ import annotations

from typing import Any, Mapping

from ..models import JSONDict
from .contracts import SCHEMA_VERSION, canonical_hash
from .validation import validate_snapshot


def build_knowledge_index(snapshot: Mapping[str, Any]) -> JSONDict:
    validate_snapshot(snapshot)
    step_number = int(snapshot["step"]["number"])
    incoming: dict[str, list[str]] = {}
    outgoing: dict[str, list[str]] = {}
    for strategy in snapshot.get("reasoning_units", []):
        incoming.setdefault(strategy["conclusion"], []).append(strategy["id"])
        for premise in strategy.get("premises", []):
            outgoing.setdefault(premise, []).append(strategy["id"])
    link_map: dict[str, list[str]] = {}
    for link in snapshot.get("non_reasoning_links", []):
        link_map.setdefault(link["source"], []).append(link["id"])
        link_map.setdefault(link["target"], []).append(link["id"])
    entries = []
    for item in snapshot.get("knowledge", []):
        canonical = item.get("content", {}).get("canonical", "")
        entries.append({
            "knowledge_id": item["id"], "type": item["type"],
            "title": item.get("title"), "canonical_text": canonical,
            "content_hash": canonical_hash({"type": item["type"], "content": canonical}),
            "languages": [key for key in ("en", "zh") if item.get("content", {}).get(key)],
            "external_ids": list(item.get("external_ids", [])),
            "source_anchor_ids": list(item.get("source_anchor_ids", [])),
            "first_seen_step": int(item.get("first_seen_step", step_number)), "current_step": step_number,
            "incoming_reasoning": sorted(incoming.get(item["id"], [])),
            "outgoing_reasoning": sorted(outgoing.get(item["id"], [])),
            "non_reasoning_links": sorted(link_map.get(item["id"], [])),
            "visibility": item.get("visibility", "public"),
            "search_text": " ".join(filter(None, [item["id"], item.get("title"), canonical, item.get("content", {}).get("en"), item.get("content", {}).get("zh")])),
        })
    external_id_map: dict[str, str] = {}
    for entry in entries:
        for external in entry["external_ids"]:
            if isinstance(external, dict) and isinstance(external.get("system"), str) and isinstance(external.get("id"), str):
                external_id_map[f"{external['system']}:{external['id']}"] = entry["knowledge_id"]
    return {
        "schema_name": "gaia.knowledge.index", "schema_version": SCHEMA_VERSION,
        "source": {"snapshot_id": snapshot["snapshot_id"], "step": step_number, "snapshot_hash": canonical_hash(snapshot)},
        "entries": entries, "external_id_map": external_id_map,
        "source_anchors": list(snapshot.get("source_anchors", [])),
    }


def validate_knowledge_index(index: Mapping[str, Any]) -> None:
    if index.get("schema_name") != "gaia.knowledge.index" or index.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported knowledge index schema")
    entries = index.get("entries")
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise ValueError("entries must be a list of objects")
    ids = [item.get("knowledge_id") for item in entries]
    if any(not isinstance(item, str) or not item for item in ids):
        raise ValueError("index entries require knowledge_id")
    if len(ids) != len(set(ids)):
        raise ValueError("knowledge index contains duplicate IDs")
    anchors = index.get("source_anchors", [])
    if not isinstance(anchors, list) or not all(isinstance(item, dict) for item in anchors):
        raise ValueError("source_anchors must be a list of objects")
    anchor_ids = {anchor.get("anchor_id") for anchor in anchors}
    for entry in entries:
        first_seen_step = entry.get("first_seen_step")
        current_step = entry.get("current_step")
        if (not isinstance(first_seen_step, int) or isinstance(first_seen_step, bool)
                or not isinstance(current_step, int) or isinstance(current_step, bool)
                or first_seen_step < 1 or current_step < first_seen_step):
            raise ValueError(f"index entry {entry['knowledge_id']} has invalid step provenance")
        if entry.get("visibility") not in {"public", "formal_internal"}:
            raise ValueError(f"index entry {entry['knowledge_id']} has invalid visibility")
        for field in ("incoming_reasoning", "outgoing_reasoning", "non_reasoning_links"):
            values = entry.get(field)
            if (not isinstance(values, list)
                    or any(not isinstance(item, str) or not item for item in values)
                    or len(values) != len(set(values))):
                raise ValueError(f"index entry {entry['knowledge_id']} has invalid {field}")
        source_anchor_ids = entry.get("source_anchor_ids", [])
        if not isinstance(source_anchor_ids, list):
            raise ValueError(f"index entry {entry['knowledge_id']} source_anchor_ids must be a list")
        missing = set(source_anchor_ids) - anchor_ids
        if missing:
            raise ValueError(f"index entry {entry['knowledge_id']} has missing anchors: {sorted(missing)}")
