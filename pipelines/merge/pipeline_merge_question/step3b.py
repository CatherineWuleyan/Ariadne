"""Step 3b: reconcile the domain spine and re-link the Packages onto it.

Two problems with the graph Step 3 produces show up only once several Packages
are merged at once — the situation a concurrent cluster run creates:

1. **The domain level is duplicated.** Each Package (or each cluster run) may
   restate the same domain conclusion in its own words, and Step 3's synthesis
   mints one `candidate_K_*` per wording.  Identical JSON is deduplicated
   mechanically, but a paraphrase is not, so the central graph ends up with N
   nodes that are one statement said N ways.
2. **Some Package conclusions never reach the domain level.** Step 2's retrieval
   is lexical and Step 3's first gate is neutral, so a conclusion that really
   does bear on a domain conclusion can end up attached to nothing.

This stage repairs both, in that order, and does nothing else:

* **semantic dedup (LLM)** — partition the domain-level conclusions into
  equivalence classes by meaning, then mechanically merge each class into one
  representative that keeps every member's content and provenance, rewriting
  every relation endpoint through the alias map.
* **link retry (LLM + mechanical check)** — for every integration-owned
  conclusion that supports no domain conclusion after the merge, ask once more,
  against the deduplicated spine only, which domain conclusion it supports or
  contradicts.  A returned link is written as an ordinary weakpoint, so Step 4
  validates and formalizes it exactly like every other relation.  A conclusion
  that still matches nothing is reported in `unlinked_conclusion_ids` instead of
  being attached to a wrong node.

The stage never invents a domain conclusion, never rewrites Package content, and
never promotes a context conclusion to the spine: dropping an unlinked
conclusion is the documented outcome, silently wiring it to something plausible
is not.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping

from pipeline_harness.models import Finding, JSONDict
from pipeline_harness.plugins import ArtifactDraft, StageContext, StageResult
from pipeline_harness.store import atomic_write_json

from .step1 import Step1InputError, _read_json, validate_step1_inputs
from .step3 import SCHEMA_VERSION as PROPOSAL_VERSION
from .step3 import PROPOSAL_SCHEMA, _Progress, _post_json, _worker_count

SPINE_SCHEMA = "gaia.integration.domain_spine"
SPINE_VERSION = "1.0.0"
# The artifact kind Step 3 writes and Step 4 reads.  Step 3b republishes its
# repaired proposal under the same kind, which is what makes Step 4 pick the
# aligned version up without knowing this stage exists.
PROPOSAL_KIND = "integration.step3_proposals"
STEP = "3b"
# Relations a Package conclusion may bear to a domain conclusion.  Only these
# four are offered, so the model cannot invent a relation family Step 4 does not
# know how to formalize.
LINK_RELATIONS = ("supports", "refines", "contradicts", "none")
GROUP_BATCH = 24
LINK_BATCH = 16
_BRACKET_ID = re.compile(r"\[([^\]]+)\]")


def _stable_id(prefix: str, values: list[str]) -> str:
    raw = "|".join([prefix, *sorted(values)])
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _local_id(qid: str) -> str:
    return str(qid).split("::")[-1]


def _is_conclusion_candidate(qid: Any) -> bool:
    return isinstance(qid, str) and _local_id(qid).startswith("candidate_K_")


def _is_premise_candidate(qid: Any) -> bool:
    return isinstance(qid, str) and _local_id(qid).startswith("candidate_A_")


def _domain_level(candidate: Mapping[str, Any]) -> int | None:
    level = candidate.get("level")
    return level if isinstance(level, int) and level > 0 else None


def _batch(items: list[JSONDict], size: int) -> list[list[JSONDict]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _dedup_prompt(candidates: list[JSONDict]) -> str:
    payload = [{"id": item["id"], "content": str(item.get("content") or "")} for item in candidates]
    return (
        "You are a domain-graph auditor. The statements below were each produced as an independent domain "
        "conclusion of a cross-paper integration. Some are the SAME proposition worded differently; the rest are "
        "genuinely distinct conclusions.\n"
        "Group the statements that assert the same proposition, so that one group can be replaced by a single node "
        "without losing information.\n"
        "Rules:\n"
        "1. Same proposition means: same subject, same predicate, same direction, same scope and same conditions. "
        "Two statements about the same topic are NOT the same proposition.\n"
        "2. A statement that only implies another, or holds in a narrower setting, is a different conclusion. Keep it "
        "separate; do not merge a general claim with a special case of it.\n"
        "3. Every id must appear in exactly one group, and a group may contain a single id.\n"
        'Return JSON only: {"groups":[{"ids":["..."],"reason":"<=12 words"}]}\n'
        f"STATEMENTS={json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def _link_prompt(domain: list[JSONDict], premises: list[JSONDict]) -> str:
    spines = [{"id": item["id"], "content": item["content"]} for item in domain]
    items = [{"id": item["id"], "content": item["content"]} for item in premises]
    return (
        "You are a domain-graph auditor. DOMAIN CONCLUSIONS are the established top-level conclusions of this "
        "integration. Each INPUT statement is a conclusion an individual source Package reached; none of them is "
        "currently attached to a domain conclusion.\n"
        "For every INPUT, decide which ONE domain conclusion it most directly bears on, and how:\n"
        "- supports: the input is evidence for it, or an instance of it;\n"
        "- refines: the input narrows or qualifies it without contradicting it;\n"
        "- contradicts: the input cannot be true together with it;\n"
        "- none: no domain conclusion is genuinely related. Use none whenever the link would only be topical; a "
        "wrong link is worse than a missing one.\n"
        "Return JSON only: {\"links\":[{\"id\":\"...\",\"domain_id\":\"...|null\",\"relation\":\"...\","
        "\"confidence\":0.0,\"reason\":\"<=12 words\"}]}\n"
        f"DOMAIN_CONCLUSIONS={json.dumps(spines, ensure_ascii=False, sort_keys=True)}\n"
        f"INPUTS={json.dumps(items, ensure_ascii=False, sort_keys=True)}"
    )


def _decode_list(prompt: str, key: str) -> list[JSONDict]:
    value = _post_json(prompt)
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        raise ValueError(f"LLM response must contain {key} array")
    return [item for item in value[key] if isinstance(item, dict)]


def _equivalence_classes(
    candidates: list[JSONDict], max_workers: int | None = None,
) -> tuple[dict[str, str], list[JSONDict], int]:
    """Partition `candidate_K_*` statements into semantic equivalence classes.

    The LLM only proposes groups; membership is then applied mechanically, and
    any id it drops, duplicates, invents or splits is corrected here.  The
    representative of a class is its lexicographically smallest member id, so the
    result does not depend on response order.
    """
    parent = {str(item["id"]): str(item["id"]) for item in candidates}

    def find(qid: str) -> str:
        while parent[qid] != qid:
            parent[qid] = parent[parent[qid]]
            qid = parent[qid]
        return qid

    def union(left: str, right: str) -> None:
        first, second = find(left), find(right)
        if first != second:
            representative, alias = min(first, second), max(first, second)
            parent[alias] = representative

    batches = _batch(candidates, GROUP_BATCH)
    proposed = 0
    if batches:
        workers = max_workers if max_workers is not None else _worker_count("GAIA_MERGE_JUDGE_WORKERS", 8)
        progress = _Progress("step3b dedup batches", len(batches), every=4)
        progress.note(f"{len(candidates)} domain conclusions in {len(batches)} batches")

        def group_batch(batch):
            response = _decode_list(_dedup_prompt(batch), "groups")
            progress.tick()
            return response

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as pool:
            responses = list(pool.map(group_batch, batches))
        known = set(parent)
        for response in responses:
            for group in response:
                ids = [str(value) for value in group.get("ids") or [] if str(value) in known]
                ids = list(dict.fromkeys(ids))
                if len(ids) < 2:
                    continue
                proposed += 1
                for other in ids[1:]:
                    union(ids[0], other)

    aliases = {qid: find(qid) for qid in parent}
    classes: dict[str, list[str]] = defaultdict(list)
    for qid, representative in aliases.items():
        classes[representative].append(qid)
    merges = [{"representative_qid": representative, "merged_qids": sorted(members)}
              for representative, members in sorted(classes.items()) if len(members) > 1]
    return aliases, merges, proposed


def _merged_content(members: list[str], contents: Mapping[str, str]) -> str:
    """Keep the first statement and append the other wordings after a marker."""
    ordered = sorted(members)
    primary = contents.get(ordered[0], "").strip()
    rest = [f"{qid}: {contents[qid].strip()}" for qid in ordered[1:] if contents.get(qid, "").strip()]
    if not rest:
        return primary
    return f"{primary} [also stated as: " + " | ".join(rest) + "]"


def _rewrite_relation_endpoints(
    proposals: Mapping[str, Any], aliases: Mapping[str, str], absorbed: set[str],
) -> tuple[list[JSONDict], list[JSONDict]]:
    """Re-point every relation at its class representative and drop self-loops."""
    operators: list[JSONDict] = []
    for operator in proposals.get("operators") or []:
        if not isinstance(operator, Mapping):
            continue
        rewritten = copy.deepcopy(dict(operator))
        variables = list(dict.fromkeys(aliases.get(str(v), str(v)) for v in operator.get("variables") or []))
        if len(variables) < 2 or all(v in absorbed for v in variables):
            continue
        rewritten["variables"] = variables
        conclusion = operator.get("conclusion")
        if isinstance(conclusion, str):
            conclusion = aliases.get(conclusion, conclusion)
            if conclusion in variables:
                conclusion = None
            rewritten["conclusion"] = conclusion
        operators.append(rewritten)

    weakpoints: list[JSONDict] = []
    for weakpoint in proposals.get("weakpoints") or []:
        if not isinstance(weakpoint, Mapping) or not isinstance(weakpoint.get("payload"), Mapping):
            continue
        rewritten = copy.deepcopy(dict(weakpoint))
        payload = rewritten["payload"]
        evidence = list(dict.fromkeys(aliases.get(str(q), str(q)) for q in payload.get("evidence_claim_ids") or []))
        targets = list(dict.fromkeys(aliases.get(str(q), str(q)) for q in payload.get("target_claim_id") or []))
        if not evidence or not targets or set(evidence) & set(targets):
            continue  # a self-relation is not a relation
        payload["evidence_claim_ids"], payload["target_claim_id"] = evidence, targets
        # The expression is checked against the endpoints in Step 4, so it has to
        # follow the same rename as the arrays.
        payload["expression"] = _BRACKET_ID.sub(
            lambda match: f"[{aliases.get(match.group(1), match.group(1))}]",
            str(payload.get("expression") or ""),
        )
        weakpoints.append(rewritten)
    return operators, weakpoints


def _relation_targets(proposals: Mapping[str, Any]) -> dict[str, set[str]]:
    """Map every knowledge id to the ids it shares a relation with."""
    neighbours: dict[str, set[str]] = defaultdict(set)
    for operator in proposals.get("operators") or []:
        if not isinstance(operator, Mapping):
            continue
        ids = [str(v) for v in operator.get("variables") or []]
        if isinstance(operator.get("conclusion"), str):
            ids.append(operator["conclusion"])
        for left in ids:
            neighbours[left].update(right for right in ids if right != left)
    for weakpoint in proposals.get("weakpoints") or []:
        if not isinstance(weakpoint, Mapping) or not isinstance(weakpoint.get("payload"), Mapping):
            continue
        payload = weakpoint["payload"]
        evidence = [str(q) for q in payload.get("evidence_claim_ids") or []]
        targets = [str(q) for q in payload.get("target_claim_id") or []]
        for left in evidence:
            neighbours[left].update(targets)
        for right in targets:
            neighbours[right].update(evidence)
    return neighbours


def _link_retry(
    domain_ids: list[str], unlinked: list[str],
    contents: Mapping[str, str], neighbours: Mapping[str, set[str]],
    max_workers: int | None = None,
) -> tuple[list[JSONDict], list[str]]:
    """Ask once more which domain conclusion each unlinked premise supports.

    Returns the accepted links and the ids that still match nothing.  A link is
    accepted only when the model names a domain conclusion from the supplied
    spine with one of the four known relations, so a hallucinated target cannot
    enter the graph.
    """
    if not domain_ids or not unlinked:
        return [], list(unlinked)
    domain = [{"id": qid, "content": contents.get(qid, "")} for qid in sorted(domain_ids)]
    items = [{"id": qid, "content": contents.get(qid, "")} for qid in sorted(unlinked)]
    batches = _batch(items, LINK_BATCH)
    workers = max_workers if max_workers is not None else _worker_count("GAIA_MERGE_JUDGE_WORKERS", 8)
    progress = _Progress("step3b link retries", len(batches), every=4)
    progress.note(f"{len(items)} unlinked conclusions against {len(domain)} domain conclusions")

    def link_batch(batch):
        response = _decode_list(_link_prompt(domain, batch), "links")
        progress.tick()
        return response

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as pool:
        responses = list(pool.map(link_batch, batches))

    known = set(domain_ids)
    allowed = {item["id"] for item in items}
    links: list[JSONDict] = []
    placed: set[str] = set()
    for response in responses:
        for row in response:
            qid = str(row.get("id") or "")
            target = str(row.get("domain_id") or "")
            relation = str(row.get("relation") or "").lower()
            if qid not in allowed or target not in known or relation not in LINK_RELATIONS:
                continue
            if relation == "none" or qid == target or qid in placed:
                continue
            placed.add(qid)
            links.append({"id": qid, "domain_id": target, "relation": relation,
                          "confidence": row.get("confidence"), "reason": str(row.get("reason") or "")})
    return links, sorted(set(unlinked) - placed)


_LOCATE_PROMPT = """One conclusion of a cross-paper integration did not end up in any relation. Locate it.

For the INPUT statement, decide exactly one of:
- "synonym": it says the SAME proposition as one of the OTHER INTEGRATION CONCLUSIONS. Name that id.
- "supports": it supports or narrows one of the DOMAIN CONCLUSIONS. Name that id.
- "isolated": none of the supplied statements is the same proposition, and none of the domain
  conclusions is one it supports.

Rules:
1. Name only ids from the supplied lists; never invent one.
2. "synonym" means same subject, same predicate, same direction, same scope. A statement that merely
   implies another, or holds in a narrower setting, is not a synonym.
3. Prefer "isolated" over a topical match; a wrong link is worse than an honest gap.
Return JSON only: {"match":"synonym|supports|isolated","target":"<id>|null","reason":"<=14 words"}
INPUT={statement}"""


def _locate_ungrouped(
    ungrouped: list[str], domain_ids: list[str], integration_ids: list[str],
    contents: Mapping[str, str], max_workers: int | None = None,
) -> list[JSONDict]:
    """Ask what each conclusion that reached no relation is the same as, or supports.

    This is the "where did this cluster go?" audit.  A conclusion in no relation
    at all is invisible in the graph, so it cannot be distinguished from a
    conclusion the integration never formed; asking once, against the material
    that *is* there, turns it into an explicit record (`synonym` / `supports` /
    `isolated`) instead of silence.
    """
    if not ungrouped:
        return []
    domain = [{"id": qid, "content": contents.get(qid, "")} for qid in sorted(domain_ids)]
    others = [{"id": qid, "content": contents.get(qid, "")} for qid in sorted(integration_ids)
              if qid not in set(domain_ids)]
    batch_prompt = (f"DOMAIN CONCLUSIONS={json.dumps(domain, ensure_ascii=False, sort_keys=True)}\n"
                    f"OTHER INTEGRATION CONCLUSIONS={json.dumps(others, ensure_ascii=False, sort_keys=True)}")
    known = set(domain_ids) | set(integration_ids)

    def locate(qid: str) -> JSONDict:
        prompt = (f"{batch_prompt}\n" + _LOCATE_PROMPT.replace(
            "{statement}", json.dumps({"id": qid, "content": contents.get(qid, "")}, ensure_ascii=False)))
        try:
            response = _post_json(prompt)
        except RuntimeError as exc:
            return {"id": qid, "match": "unknown", "target": None, "reason": str(exc)[:120]}
        match = str(response.get("match") or "") if isinstance(response, dict) else ""
        target = str(response.get("target") or "") if isinstance(response, dict) else ""
        if match not in {"synonym", "supports", "isolated"} or (match != "isolated" and target not in known):
            match, target = "isolated", ""  # an unusable answer is a gap, not a link
        return {"id": qid, "match": match, "target": target or None,
                "reason": str(response.get("reason") or "")[:160] if isinstance(response, dict) else ""}

    workers = max_workers if max_workers is not None else _worker_count("GAIA_MERGE_JUDGE_WORKERS", 8)
    progress = _Progress("step3b ungrouped conclusions", len(ungrouped), every=10)
    progress.note(f"{len(ungrouped)} integration conclusions reached no relation at all")

    def run(qid: str) -> JSONDict:
        row = locate(qid)
        progress.tick()
        return row

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(ungrouped)))) as pool:
        return list(pool.map(run, sorted(ungrouped)))


class Step3bAlignDomainSpinePlugin:
    stage_name = "step3b_align_domain_spine"

    @staticmethod
    def _synthesize_links(links: list[JSONDict]) -> list[JSONDict]:
        """Turn accepted links into ordinary Step 4 weakpoints.

        `supports` and `refines` are a bounded deduction (`premise -> domain`).
        `contradicts` is a directed relation the integration owns, and Step 4
        formalizes it as an infer strategy, so it is not silently turned into a
        conjunction it did not earn.
        """
        weakpoints: list[JSONDict] = []
        for link in links:
            reasoning_type = "infer" if link["relation"] == "contradicts" else "deduction"
            weakpoints.append({
                "id": _stable_id("weakpoint_spine_link", [link["id"], link["domain_id"], link["relation"]]),
                "payload": {
                    "evidence_claim_ids": [link["id"]],
                    "target_claim_id": [link["domain_id"]],
                    "reasoning_type": reasoning_type,
                    "evidence_anchor_ids": [],
                    "expression": f"[{link['id']}] {link['relation']} [{link['domain_id']}]",
                },
                "metadata": {"spine_link_relation": link["relation"],
                             "spine_link_confidence": link.get("confidence"),
                             "spine_link_reason": link.get("reason")},
            })
        return weakpoints

    def run(self, context: StageContext) -> StageResult:
        try:
            validate_step1_inputs(context)
            proposal_ref = context.require_one(kind="integration.step3_proposals")
            proposals = _read_json(context, proposal_ref)
            if proposals.get("schema_name") != PROPOSAL_SCHEMA:
                raise ValueError("Step 3b requires the Step 3 proposal artifact")
            candidates = [dict(item) for item in proposals.get("candidate_knowledges") or []
                          if isinstance(item, Mapping) and isinstance(item.get("id"), str)]
            contents = {str(item["id"]): str(item.get("content") or "") for item in candidates}
            domain_candidates = [item for item in candidates
                                 if _is_conclusion_candidate(item["id"]) and _domain_level(item)]

            # ---- 1. semantic dedup of the domain level --------------------
            aliases, merges, proposed = _equivalence_classes(domain_candidates)
            absorbed = {alias for alias, representative in aliases.items() if alias != representative}
            operators, weakpoints = _rewrite_relation_endpoints(proposals, aliases, absorbed)
            merged_candidates: list[JSONDict] = []
            for item in candidates:
                qid = str(item["id"])
                if qid in absorbed:
                    continue
                if qid not in aliases or aliases[qid] == qid:
                    members = sorted([qid, *(alias for alias in absorbed if aliases[alias] == qid)])
                    if len(members) > 1:
                        item = {**item, "content": _merged_content(members, contents),
                                "provenance_qids": sorted(set(item.get("provenance_qids") or []) | set(members))}
                        contents[qid] = str(item["content"])
                    merged_candidates.append(item)
                    continue
                merged_candidates.append(item)

            # ---- 2. re-link the Packages onto the deduplicated spine ------
            domain_ids = sorted(str(item["id"]) for item in merged_candidates
                                if _is_conclusion_candidate(item["id"]) and _domain_level(item))
            spine_ids = set(domain_ids)
            neighbours = _relation_targets({"operators": operators, "weakpoints": weakpoints})
            linked = {qid for qid in contents if spine_ids & neighbours.get(qid, set())}
            integration_owned = sorted(
                qid for qid in contents
                if qid not in spine_ids and _is_premise_candidate(qid)
                and not (spine_ids & neighbours.get(qid, set()))
            )
            links, unlinked = _link_retry(domain_ids, integration_owned, contents, neighbours)
            weakpoints.extend(self._synthesize_links(links))
            weakpoints = list({json.dumps(item, sort_keys=True): item for item in weakpoints}.values())

            # Where did the conclusions that reached no relation at all go?  A
            # conclusion in no relation is invisible in the graph, so it cannot be
            # told apart from one the integration never formed.  Audit every one of
            # them against the material that does exist and record the verdict.
            integration_ids = sorted(qid for qid in contents if _is_premise_candidate(qid))
            ungrouped = sorted(qid for qid in integration_ids
                               if not neighbours.get(qid) and qid not in spine_ids
                               and qid not in {link["id"] for link in links})
            located = _locate_ungrouped(ungrouped, domain_ids, integration_ids, contents)
            synonym_pairs = [[row["id"], row["target"]] for row in located
                             if row["match"] == "synonym" and row["target"]]
            located_support = [{"id": row["id"], "domain_id": row["target"], "relation": "supports",
                                "confidence": None, "reason": row["reason"]}
                               for row in located if row["match"] == "supports" and row["target"]]
            if located_support:
                weakpoints.extend(self._synthesize_links(located_support))
                weakpoints = list({json.dumps(item, sort_keys=True): item for item in weakpoints}.values())
            still_isolated = sorted(row["id"] for row in located if row["match"] == "isolated")

            artifact = {
                "schema_name": SPINE_SCHEMA,
                "schema_version": SPINE_VERSION,
                "step": STEP,
                "source_proposal_artifact": {"artifact_id": proposal_ref.artifact_id,
                                             "kind": proposal_ref.kind, "sha256": proposal_ref.sha256},
                "package_set": sorted(set(proposals.get("package_set") or [])),
                "domain_conclusion_count": len(domain_ids),
                "domain_conclusion_ids": domain_ids,
                "dedup_classes": [{
                    **merge,
                    "canonical_content": contents.get(merge["representative_qid"], ""),
                } for merge in merges],
                "proposed_class_count": proposed,
                "cluster_links": links,
                "unlinked_conclusion_ids": unlinked,
                "ungrouped_conclusion_count": len(ungrouped),
                "ungrouped_conclusions": located,
                "synonym_pairs": synonym_pairs,
                "isolated_conclusion_ids": still_isolated,
                "candidate_knowledges": merged_candidates,
                "operators": operators,
                "weakpoints": weakpoints,
            }
            output = context.work_dir / "integration_domain_spine.json"
            atomic_write_json(output, artifact)
            # A checkpoint carries only the artifacts its own stage produced, so
            # the repaired proposal has to be republished as a Step 3-shaped
            # artifact for Step 4 — otherwise Step 4's checkpoint no longer holds
            # the Step 3 proposal at all.  Step 4 takes the latest artifact of
            # this kind, which is therefore this one.
            aligned = {
                **{key: value for key, value in proposals.items()
                   if key not in {"operators", "weakpoints", "candidate_knowledges"}},
                "operators": operators,
                "weakpoints": weakpoints,
                "candidate_knowledges": merged_candidates,
                # Same-meaning pairs found by the ungrouped audit, so Step 4's
                # mechanical merge (which keeps the O/E/non-OE category guard)
                # folds them instead of leaving two nodes for one proposition.
                "equivalence_pairs": synonym_pairs,
                "spine_alignment": {
                    "domain_conclusion_count": len(domain_ids),
                    "domain_merge_count": len(merges),
                    "cluster_link_count": len(links),
                    "unlinked_conclusion_count": len(unlinked),
                    "unlinked_conclusion_ids": unlinked,
                    "ungrouped_conclusion_count": len(ungrouped),
                    "isolated_conclusion_count": len(still_isolated),
                    "isolated_conclusion_ids": still_isolated,
                    "spine_artifact_id": None,  # filled in below, from the spine bytes
                },
            }
            aligned_output = context.work_dir / "integration_step3_proposals_aligned.json"
            findings: list[Finding] = []
            for merge in merges:
                findings.append(Finding(
                    "STEP3B_DOMAIN_CONCLUSION_MERGED", "info",
                    f"Merged {len(merge['merged_qids'])} paraphrases into {merge['representative_qid']}: "
                    f"{merge['merged_qids']}"))
            for link in links:
                findings.append(Finding(
                    "STEP3B_PACKAGE_CONCLUSION_RELINKED", "info",
                    f"Linked {link['id']} to {link['domain_id']} as {link['relation']}"))
            for qid in unlinked:
                findings.append(Finding(
                    "STEP3B_PACKAGE_CONCLUSION_UNLINKED", "warning",
                    f"{qid} supports no domain conclusion after the retry and stays unlinked"))
            for row in located:
                if row["match"] == "synonym":
                    findings.append(Finding(
                        "STEP3B_UNGROUPED_CONCLUSION_SYNONYM", "info",
                        f"{row['id']} reached no relation but says the same proposition as "
                        f"{row['target']}: {row['reason']}"))
                elif row["match"] == "supports":
                    findings.append(Finding(
                        "STEP3B_UNGROUPED_CONCLUSION_SUPPORTS", "info",
                        f"{row['id']} reached no relation but supports {row['target']}: {row['reason']}"))
            for qid in still_isolated:
                findings.append(Finding(
                    "STEP3B_CONCLUSION_ISOLATED", "warning",
                    f"{qid} reached no relation, is not a restatement, and supports no domain "
                    f"conclusion: it is a genuine gap in this integration"))

            # Publish the spine first so the aligned proposal can name its exact
            # byte-for-byte hash; the audit trail then survives a checkpoint.
            spine_digest = hashlib.sha256(output.read_bytes()).hexdigest()
            aligned["spine_alignment"]["spine_artifact_id"] = f"sha256:{spine_digest}"
            atomic_write_json(aligned_output, aligned)

            return StageResult(
                "succeeded",
                artifacts=[
                    ArtifactDraft(output, SPINE_SCHEMA, "application/json", {
                        "schema_name": SPINE_SCHEMA, "schema_version": SPINE_VERSION, "step": STEP,
                        "domain_conclusion_count": len(domain_ids),
                        "domain_merge_count": len(merges),
                        "cluster_link_count": len(links),
                        "unlinked_conclusion_count": len(unlinked),
                        "ungrouped_conclusion_count": len(ungrouped),
                        "isolated_conclusion_count": len(still_isolated),
                        "synonym_pair_count": len(synonym_pairs),
                    }),
                    ArtifactDraft(aligned_output, PROPOSAL_KIND, "application/json", {
                        "schema_name": PROPOSAL_SCHEMA, "schema_version": PROPOSAL_VERSION,
                        "step": 3, "spine_aligned": True,
                        "domain_conclusion_count": len(domain_ids),
                        "domain_merge_count": len(merges),
                        "cluster_link_count": len(links),
                        "unlinked_conclusion_count": len(unlinked),
                    }),
                ],
                findings=findings,
                metadata={
                    "step": STEP,
                    "domain_conclusion_count": len(domain_ids),
                    "domain_merge_count": len(merges),
                    "domain_merged_away": len(absorbed),
                    "cluster_link_count": len(links),
                    "unlinked_conclusion_count": len(unlinked),
                    "ungrouped_conclusion_count": len(ungrouped),
                    "synonym_pair_count": len(synonym_pairs),
                    "isolated_conclusion_count": len(still_isolated),
                    "isolated_conclusion_ids": still_isolated[:20],
                    "link_retry_candidate_count": len(integration_owned),
                },
            )
        except (Step1InputError, ValueError, KeyError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            return StageResult("failed", findings=[Finding("STEP3B_FAILED", "error", str(exc))])


__all__ = ["Step3bAlignDomainSpinePlugin", "SPINE_SCHEMA", "SPINE_VERSION", "PROPOSAL_VERSION"]
