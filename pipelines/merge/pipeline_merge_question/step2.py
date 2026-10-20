"""Step 2: retrieve a bounded cross-Package local context from Gaia IR."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from pipeline_harness.domain.stages import _cosine, _tokens
from pipeline_harness.models import ArtifactRef, Finding, JSONDict
from pipeline_harness.plugins import ArtifactDraft, StageContext, StageResult
from pipeline_harness.store import atomic_write_json

from .step1 import (
    Step1InputError,
    _read_json,
    _step0_options,
    validate_step1_inputs,
)


LOCAL_CONTEXT_SCHEMA = "gaia.integration.local_context"
LOCAL_CONTEXT_VERSION = "1.0.0"
RETRIEVAL_METHOD = "local_cosine_v1"
TOP_K = 20
MIN_SHARED_TOKENS = 2
MIN_COSINE = 0.2
GROUP_OVERLAP_MERGE = 0.7
GROUPING_METHOD = "seed_overlap_v1"


class Step2RetrievalError(ValueError):
    """A deterministic Step 2 failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _source_artifacts(context: StageContext) -> list[ArtifactRef]:
    return [
        ref for ref in context.inputs
        if ref.kind in {"gaia.ir", "formalization", "knowledge.index"}
    ]


def _load_packages(context: StageContext) -> dict[str, tuple[ArtifactRef, JSONDict]]:
    packages: dict[str, tuple[ArtifactRef, JSONDict]] = {}
    for ref in context.find_all("gaia.ir"):
        payload = _read_json(context, ref)
        namespace = payload.get("namespace")
        package_name = payload.get("package_name")
        if not isinstance(namespace, str) or not isinstance(package_name, str):
            raise Step2RetrievalError(
                "STEP2_GAIA_IR_INVALID",
                f"gaia.ir {ref.artifact_id} has an invalid Package identity",
            )
        identity = f"{namespace}:{package_name}"
        if identity in packages:
            raise Step2RetrievalError(
                "STEP2_PACKAGE_DUPLICATE",
                f"Package {identity} occurs more than once in Step 2 inputs",
            )
        packages[identity] = (ref, payload)
    return packages


def _knowledge_records(
    identity: str,
    payload: Mapping[str, Any],
) -> list[JSONDict]:
    records: list[JSONDict] = []
    for item in payload.get("knowledges", []):
        if not isinstance(item, Mapping):
            continue
        qid = item.get("id")
        content = item.get("content")
        if not isinstance(qid, str) or not qid or not isinstance(content, str) or not content.strip():
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        visibility = metadata.get("visibility", metadata.get("helper_visibility"))
        # Strategy interface claims (for example AltExp placeholders) are
        # compiler scaffolding, not paper propositions.  Retrieving them
        # creates spurious cross-paper candidates and can swamp the actual
        # evidence needed by merge.
        source_knowledge_id = str(metadata.get("source_knowledge_id") or "")
        if (visibility in {"formal_internal", "strategy_interface"}
                or metadata.get("generated_kind") == "interface_claim"
                or "step4_weakpoint_relation" in source_knowledge_id):
            continue
        records.append({
            "qid": qid,
            "package": identity,
            "local_id": metadata.get("source_knowledge_id", item.get("label", qid)),
            "content": content,
        })
    return records


def _index_visibility(
    context: StageContext,
    packages: Mapping[str, tuple[ArtifactRef, JSONDict]],
    validation: Mapping[str, Any],
) -> set[str]:
    """Return local IDs marked internal by optional indexes, for conservative filtering."""
    internal: set[str] = set()
    bindings = validation.get("knowledge_index_bindings", {})
    if not isinstance(bindings, Mapping):
        return internal
    package_local_ids: dict[str, dict[str, str]] = {}
    for identity, (_, payload) in packages.items():
        package_local_ids[identity] = {
            str((item.get("metadata") or {}).get("source_knowledge_id", item.get("label", item.get("id")))): str(item["id"])
            for item in payload.get("knowledges", [])
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
    for ref in context.find_all("knowledge.index"):
        identity = bindings.get(ref.artifact_id)
        if not isinstance(identity, str) or identity not in package_local_ids:
            continue
        index = _read_json(context, ref)
        for entry in index.get("entries", []):
            if isinstance(entry, Mapping) and entry.get("visibility") == "formal_internal":
                local_id = entry.get("knowledge_id")
                qid = package_local_ids[identity].get(str(local_id))
                if qid:
                    internal.add(qid)
    return internal


def _selected_seed_qids(
    options: Mapping[str, Any],
    packages: Mapping[str, tuple[ArtifactRef, JSONDict]],
    records: Mapping[str, list[JSONDict]],
) -> tuple[set[str], set[str]]:
    mode = options["mode"]
    selected_packages = set(options["scope"]["selection"])
    identities = set(packages)
    package_aliases = {identity.split(":", 1)[1]: identity for identity in identities}
    selected_identities = {
        value if value in identities else package_aliases.get(value, value)
        for value in selected_packages
    }
    selected_identities &= identities
    if mode in {"bootstrap", "incremental"}:
        seeds = {
            record["qid"]
            for identity in selected_identities
            for record in records[identity]
        }
        return seeds, selected_identities

    qid_map = {
        record["qid"]: identity
        for identity, values in records.items()
        for record in values
    }
    seeds: set[str] = set()
    for value in options["scope"]["selection"]:
        if value in qid_map:
            seeds.add(value)
        elif value in identities or value in package_aliases:
            identity = value if value in identities else package_aliases[value]
            seeds.update(record["qid"] for record in records[identity])
    return seeds, selected_identities


def _retrieve(
    seeds: list[JSONDict],
    corpus: list[JSONDict],
) -> list[JSONDict]:
    hits: dict[str, JSONDict] = {}
    for seed in seeds:
        query_tokens = _tokens(seed["content"])
        if not query_tokens:
            continue
        for candidate in corpus:
            if candidate["package"] == seed["package"]:
                continue
            candidate_tokens = _tokens(candidate["content"])
            shared = len(set(query_tokens) & set(candidate_tokens))
            score = _cosine(query_tokens, candidate_tokens)
            if shared < MIN_SHARED_TOKENS or score < MIN_COSINE:
                continue
            current = hits.get(candidate["qid"])
            hit = {
                "qid": candidate["qid"],
                "package": candidate["package"],
                "query_seed_qids": [seed["qid"]],
                "rank_score": score,
                "shared_token_count": shared,
                "retrieval_method": RETRIEVAL_METHOD,
            }
            if current is None:
                hits[candidate["qid"]] = hit
            else:
                current["query_seed_qids"] = sorted(set(current["query_seed_qids"]) | {seed["qid"]})
                if (score, shared) > (current["rank_score"], current["shared_token_count"]):
                    current["rank_score"] = score
                    current["shared_token_count"] = shared
    ranked = sorted(
        hits.values(),
        key=lambda item: (-item["rank_score"], -item["shared_token_count"], item["qid"]),
    )[:TOP_K]
    for rank, item in enumerate(ranked, 1):
        item["rank"] = rank
        del item["rank_score"]
    return ranked


def _group_id(qids: list[str]) -> str:
    digest = hashlib.sha256("|".join(sorted(qids)).encode("utf-8")).hexdigest()[:16]
    return f"group_{digest}"


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _build_groups(
    seeds: list[JSONDict],
    hits: list[JSONDict],
    one_hop: list[JSONDict],
) -> list[JSONDict]:
    """Build deterministic, overlapping proposition groups from Step 2 hits.

    A seed defines an initial content-related group. Groups may share propositions,
    but near-duplicates are merged using Jaccard overlap on proposition QIDs only.
    One-hop structures are attached after the proposition sets are finalized.
    """
    hit_by_seed: dict[str, set[str]] = {str(seed["qid"]): set() for seed in seeds}
    for hit in hits:
        for seed_qid in hit.get("query_seed_qids", []):
            if str(seed_qid) in hit_by_seed:
                hit_by_seed[str(seed_qid)].add(str(hit["qid"]))

    initial: list[dict[str, Any]] = []
    for seed in sorted(seeds, key=lambda item: str(item["qid"])):
        seed_qid = str(seed["qid"])
        propositions = {seed_qid, *hit_by_seed.get(seed_qid, set())}
        if len(propositions) < 2:
            continue
        initial.append({"seed_qids": {seed_qid}, "proposition_qids": propositions})

    # Merge only near-duplicate groups. This preserves meaningful overlap while
    # preventing many seed groups from becoming the same context repeatedly.
    groups: list[dict[str, Any]] = []
    for candidate in initial:
        merged = False
        for current in groups:
            if _jaccard(current["proposition_qids"], candidate["proposition_qids"]) > GROUP_OVERLAP_MERGE:
                current["seed_qids"].update(candidate["seed_qids"])
                current["proposition_qids"].update(candidate["proposition_qids"])
                merged = True
                break
        if not merged:
            groups.append(candidate)

    result: list[JSONDict] = []
    for group in groups:
        propositions = sorted(group["proposition_qids"])
        proposition_set = set(propositions)
        node_ids = sorted(
            f"{node['kind']}:{node['package']}:{node['id']}"
            for node in one_hop
            if proposition_set & {str(qid) for qid in node.get("connected_qids", [])}
        )
        packages = sorted({str(seed["package"]) for seed in seeds if str(seed["qid"]) in group["seed_qids"]}
                          | {str(hit["package"]) for hit in hits if str(hit["qid"]) in proposition_set})
        if len(packages) < 2:
            continue
        result.append({
            "group_id": _group_id(propositions),
            "seed_qids": sorted(group["seed_qids"]),
            "proposition_qids": propositions,
            "one_hop_node_ids": node_ids,
            "package_set": packages,
            "grouping_method": GROUPING_METHOD,
        })
    return sorted(result, key=lambda item: item["group_id"])


def _one_hop(
    hits: list[JSONDict],
    packages: Mapping[str, tuple[ArtifactRef, JSONDict]],
    formalizations: Mapping[str, JSONDict],
) -> list[JSONDict]:
    hit_qids = {item["qid"] for item in hits}
    nodes: dict[tuple[str, str, str], JSONDict] = {}
    for identity, (_, payload) in packages.items():
        for operator in payload.get("operators", []):
            if not isinstance(operator, Mapping):
                continue
            refs = [*operator.get("variables", []), operator.get("conclusion")]
            connected = sorted({value for value in refs if isinstance(value, str) and value in hit_qids})
            if connected:
                all_refs = sorted({value for value in refs if isinstance(value, str) and value})
                key = ("operator", str(operator.get("operator_id", operator.get("id", ""))), identity)
                nodes[key] = {"kind": "operator", "id": key[1], "package": identity, "connected_qids": all_refs}
        for strategy in payload.get("strategies", []):
            if not isinstance(strategy, Mapping):
                continue
            refs = [*strategy.get("premises", []), *strategy.get("background", []), strategy.get("conclusion")]
            formal_expr = strategy.get("formal_expr")
            if isinstance(formal_expr, Mapping):
                for operator in formal_expr.get("operators", []):
                    if isinstance(operator, Mapping):
                        refs.extend([*operator.get("variables", []), operator.get("conclusion")])
            connected = sorted({value for value in refs if isinstance(value, str) and value in hit_qids})
            if connected:
                all_refs = sorted({value for value in refs if isinstance(value, str) and value})
                strategy_id = str(strategy.get("strategy_id", ""))
                nodes[("strategy", strategy_id, identity)] = {
                    "kind": "strategy", "id": strategy_id, "package": identity, "connected_qids": all_refs,
                }
    for identity, document in formalizations.items():
        workflow = document.get("workflow")
        if not isinstance(workflow, Mapping):
            continue
        for weakpoint in workflow.get("weakpoints", []):
            if not isinstance(weakpoint, Mapping):
                continue
            payload = weakpoint.get("payload", weakpoint)
            if not isinstance(payload, Mapping):
                continue
            refs = [*payload.get("evidence_claim_ids", []), *payload.get("target_claim_id", [])]
            if any(isinstance(value, str) and any(value == qid or qid.endswith(f"::{value}") for qid in hit_qids) for value in refs):
                weakpoint_id = str(weakpoint.get("id", weakpoint.get("weakpoint_id", "")))
                nodes[("weakpoint", weakpoint_id, identity)] = {
                    "kind": "weakpoint", "id": weakpoint_id, "package": identity,
                    "connected_qids": sorted(str(value) for value in refs if isinstance(value, str)),
                }
    qid_packages = {
        str(item["id"]): identity
        for identity, (_, payload) in packages.items()
        for item in payload.get("knowledges", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    for node in list(nodes.values()):
        for qid in node["connected_qids"]:
            identity = qid_packages.get(qid)
            if identity is not None and qid not in hit_qids:
                nodes[("knowledge", qid, identity)] = {
                    "kind": "knowledge", "id": qid, "package": identity,
                    "connected_qids": [],
                }
    return sorted(nodes.values(), key=lambda item: (item["kind"], item["package"], item["id"]))


def build_local_context(context: StageContext) -> JSONDict:
    validation = validate_step1_inputs(context)
    options = _step0_options(context)
    packages = _load_packages(context)
    records = {
        identity: _knowledge_records(identity, payload)
        for identity, (_, payload) in packages.items()
    }
    internal_qids = _index_visibility(context, packages, validation)
    for identity in records:
        records[identity] = [item for item in records[identity] if item["qid"] not in internal_qids]
    seeds, selected_identities = _selected_seed_qids(options, packages, records)
    seed_records = [record for values in records.values() for record in values if record["qid"] in seeds]
    allowed_identities = set(packages)
    if options["mode"] == "reconcile" and selected_identities:
        allowed_identities = set(packages)
    corpus = [record for identity, values in records.items() if identity in allowed_identities for record in values]
    hits = _retrieve(sorted(seed_records, key=lambda item: item["qid"]), corpus)
    formalizations: dict[str, JSONDict] = {}
    for ref in context.find_all("formalization"):
        document = _read_json(context, ref)
        package = document.get("package", {})
        if isinstance(package, Mapping):
            identity = f"{package.get('namespace')}:{package.get('name')}"
            if identity in packages:
                formalizations[identity] = document
    # A query seed is itself a direct entry point.  Expand its existing
    # one-hop structures alongside retrieved cross-package hits; otherwise an
    # incremental run can omit the new paper's own weakpoints and strategies.
    expansion_hits = list({item["qid"]: item for item in [*seed_records, *hits]}.values())
    one_hop = _one_hop(expansion_hits, packages, formalizations)
    groups = _build_groups(seed_records, hits, one_hop)
    package_set = sorted({item["package"] for item in hits} | {item["package"] for item in one_hop})
    return {
        "schema_name": LOCAL_CONTEXT_SCHEMA,
        "schema_version": LOCAL_CONTEXT_VERSION,
        "step": 2,
        "mode": options["mode"],
        "domain": options["scope"]["domain"],
        "source_artifacts": [
            {"artifact_id": ref.artifact_id, "kind": ref.kind, "sha256": ref.sha256}
            for ref in _source_artifacts(context)
        ],
        "query_seeds": [
            {"qid": record["qid"], "package": record["package"]}
            for record in sorted(seed_records, key=lambda item: item["qid"])
        ],
        "excluded_query_packages": sorted({record["package"] for record in seed_records}),
        "direct_hits": hits,
        "one_hop_nodes": one_hop,
        "groups": groups,
        "package_set": package_set,
    }


class Step2RetrieveDomainLocalPlugin:
    """Create a bounded, replayable local candidate context."""

    def run(self, context: StageContext) -> StageResult:
        try:
            document = build_local_context(context)
        except Step1InputError as exc:
            return StageResult("failed", findings=[Finding(exc.code, "error", str(exc))])
        except Step2RetrievalError as exc:
            return StageResult("failed", findings=[Finding(exc.code, "error", str(exc))])
        output = context.work_dir / "integration_local_context.json"
        atomic_write_json(output, document)
        return StageResult(
            "succeeded",
            artifacts=[ArtifactDraft(output, "integration.local_context", "application/json", {
                "schema_name": LOCAL_CONTEXT_SCHEMA,
                "schema_version": LOCAL_CONTEXT_VERSION,
                "step": 2,
                "retrieval_method": RETRIEVAL_METHOD,
                "candidate_count": len(document["direct_hits"]),
                "group_count": len(document["groups"]),
            })],
            metadata={
                "step": 2,
                "retrieval_method": RETRIEVAL_METHOD,
                "query_seed_count": len(document["query_seeds"]),
                "candidate_count": len(document["direct_hits"]),
                "one_hop_count": len(document["one_hop_nodes"]),
                "group_count": len(document["groups"]),
            },
        )
