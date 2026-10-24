"""Two ways to ask the same question, measured against each other.

The current link judge gets one candidate and 44 claims, and returns at most ten
links. Its reasoning trace shows it treats that as a selection task, and the
selection is what changes between identical runs.

This module asks the question differently and lets the caller compare:

* ``pairwise``  -- one claim and one candidate per call: does this claim bear on
  this candidate? No budget, no competing context.
* ``claim_all`` -- one claim and every candidate per call: which candidates does
  this claim bear on? Candidates compete, so a claim cannot support a statement
  and its negation without the judge noticing.
* ``candidate_all`` -- one candidate and every claim per call, with no link cap.
  The incumbent layout minus the budget.

All three return the same shape, so the comparison is about the protocol only:

    {(candidate_code, claim_id): {"relation": ..., "reason": ...}}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .util import AriadneError, REPO, map_ordered, read_json, write_json

SCHEMA = "ariadne.link_protocols"
SCHEMA_VERSION = "0.1.0"
RELATIONS = ("support", "oppose", "bounds")
CLAIM_CAP = 300

RUBRIC = """A claim can bear on a candidate in exactly one way:
- support: the claim states a method, result, or design fact that directly supports this route. Same topic alone is not enough; the reason must name what the claim contributes.
- oppose: the claim reports a result that is incompatible with the route under compatible conditions. Do not infer opposition from a different aircraft, dataset, model, or condition.
- bounds: the claim gives a condition, scope limit, or assumption that constrains when the route holds.
- none: topical only, not enough information, or the claim argues for a different recommendation."""


def native_imports() -> Any:
    for extra in (REPO, REPO / "agent", REPO / "pipelines/single_paper", REPO / "pipelines/merge"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    from pipeline_merge_question import question_layer

    return question_layer


def claim_entry(node: Mapping[str, Any], question_layer: Any) -> dict[str, str]:
    details = node.get("details") or {}
    text = str(node.get("summary") or details.get("content") or "")
    return {
        "id": str(node.get("id")),
        "code": question_layer.node_code(node),
        "content": text[:CLAIM_CAP],
    }


def pair_prompt(claim: Mapping[str, str], candidate: Mapping[str, Any]) -> str:
    return f"""Judge ONE claim against ONE candidate route.

CANDIDATE {candidate['code']}: {candidate['statement']}

{RUBRIC}

CLAIM {claim['code']}: {claim['content']}

Return JSON only:
{{"claim_id": "{claim['id']}", "relation": "support|oppose|bounds|none", "reason": "one short sentence naming what the claim contributes"}}"""


def claim_all_prompt(claim: Mapping[str, str], candidates: list[Mapping[str, Any]]) -> str:
    routes = [
        {"code": str(item["code"]), "statement": str(item["statement"])}
        for item in candidates
    ]
    return f"""Judge ONE claim against EVERY candidate route at once.

{RUBRIC}

Candidates compete: if the claim argues for one route, it does not support the
route that says the opposite. A claim may bear on more than one candidate only
when it genuinely bears on each; otherwise return the one it bears on.

CLAIM {claim['code']}: {claim['content']}

CANDIDATES={json.dumps(routes, ensure_ascii=False, sort_keys=True)}

Return JSON only, with one entry per candidate you can justify (an empty list is allowed):
{{"claim_id": "{claim['id']}", "links": [{{"candidate": "R1", "relation": "support|oppose|bounds", "reason": "..."}}]}}"""


def candidate_all_prompt(candidate: Mapping[str, Any], claims: list[Mapping[str, str]]) -> str:
    routes = [{"id": c["id"], "code": c["code"], "content": c["content"]} for c in claims]
    return f"""Judge EVERY claim against ONE candidate route. There is no link budget.

CANDIDATE {candidate['code']}: {candidate['statement']}

{RUBRIC}

Decide every claim listed below. Do not stop at a fixed number, and do not skip a
claim because it is probably irrelevant: give it none.

CLAIMS={json.dumps(routes, ensure_ascii=False, sort_keys=True)}

Return JSON only:
{{"candidate": "{candidate['code']}", "decisions": [{{"claim_id": "...", "relation": "support|oppose|bounds|none", "reason": "..."}}]}}"""


def _relation(value: Any) -> str | None:
    relation = str(value or "").strip().lower()
    return relation if relation in RELATIONS else None


def parse_pair(response: Any, claim: Mapping[str, str],
               candidate: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    relation = _relation(response.get("relation") if isinstance(response, Mapping) else None)
    if relation is None:
        return {}
    return {(str(candidate["code"]), claim["id"]): {
        "relation": relation,
        "reason": str((response or {}).get("reason") or "")[:300],
    }}


def parse_claim_all(response: Any, claim: Mapping[str, str],
                    candidates: list[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    allowed = {str(item["code"]): str(item["id"]) for item in candidates}
    raw = response.get("links") if isinstance(response, Mapping) else None
    picked: dict[str, dict[str, str]] = {}
    for item in raw or []:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("candidate") or item.get("candidate_id") or "")
        relation = _relation(item.get("relation"))
        if code not in allowed or relation is None:
            continue
        # One relation per (claim, candidate); a second answer for the same pair
        # is not a decision, so it is dropped rather than resolved by order.
        key = (code, claim["id"])
        if key in picked:
            picked.pop(key)
            continue
        picked[key] = {"relation": relation,
                       "reason": str(item.get("reason") or "")[:300]}
    return picked


def parse_candidate_all(response: Any, candidate: Mapping[str, Any],
                        claims: list[Mapping[str, str]]) -> dict[str, dict[str, str]]:
    allowed = {claim["id"] for claim in claims}
    raw = response.get("decisions") if isinstance(response, Mapping) else None
    picked: dict[str, dict[str, str]] = {}
    for item in raw or []:
        if not isinstance(item, Mapping):
            continue
        claim_id = str(item.get("claim_id") or "")
        relation = _relation(item.get("relation"))
        if claim_id not in allowed or relation is None:
            continue
        picked[(str(candidate["code"]), claim_id)] = {
            "relation": relation,
            "reason": str(item.get("reason") or "")[:300],
        }
    return picked


def run_protocol(protocol: str, view: Mapping[str, Any], candidates: list[Mapping[str, Any]],
                 claims: list[Mapping[str, str]], *, call_json: Callable[[str], Any],
                 workers: int | None = None) -> dict[str, dict[str, str]]:
    """Run one protocol over the same claim set and candidate set."""
    if protocol == "pairwise":
        jobs = [(candidate, claim) for candidate in candidates for claim in claims]

        def one(job: tuple[Mapping[str, Any], Mapping[str, str]]):
            candidate, claim = job
            response = call_json(pair_prompt(claim, candidate))
            return parse_pair(response, claim, candidate)

    elif protocol == "claim_all":
        jobs = list(claims)

        def one(job: Mapping[str, str]):
            response = call_json(claim_all_prompt(job, candidates))
            return parse_claim_all(response, job, candidates)

    elif protocol == "candidate_all":
        jobs = list(candidates)

        def one(job: Mapping[str, Any]):
            response = call_json(candidate_all_prompt(job, claims))
            return parse_candidate_all(response, job, claims)

    else:
        raise AriadneError(f"unknown protocol: {protocol}")

    results, failures = map_ordered(one, jobs, workers)
    merged: dict[str, dict[str, str]] = {}
    for chunk in results:
        merged.update(chunk)
    if failures:
        print(f"[{protocol}] {len(failures)} call(s) failed; decisions from the rest are kept",
              flush=True)
    return merged


def baseline_links(answer_layer: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    links: dict[str, dict[str, str]] = {}
    for root in answer_layer.get("roots") or []:
        code = str(root.get("code"))
        for link in root.get("links") or []:
            relation = _relation(link.get("relation"))
            if relation:
                links[(code, str(link.get("claim_id")))] = {"relation": relation}
    return links


def evaluate(decisions: Mapping[str, Mapping[str, str]],
             baseline: Mapping[str, Mapping[str, str]],
             controls: Iterable[str], candidates: Iterable[str]) -> dict[str, Any]:
    """Recall against the stored links, and false positives on vetted negatives.

    There is no human gold here, so neither number is an accuracy. They are the
    two things that can be checked without one: does the protocol still find what
    the current one found, and does it hand support to claims that cannot support
    anything.
    """
    control_ids = set(controls)
    baseline_support = {key for key, value in baseline.items() if value["relation"] == "support"}
    found_support = {key for key, value in decisions.items() if value["relation"] == "support"}
    recovered = baseline_support & found_support
    control_hits = {(code, cid) for (code, cid) in found_support if cid in control_ids}
    return {
        "decisions": len(decisions),
        "support": len(found_support),
        "oppose": sum(1 for v in decisions.values() if v["relation"] == "oppose"),
        "bounds": sum(1 for v in decisions.values() if v["relation"] == "bounds"),
        "baseline_support": len(baseline_support),
        "recall_vs_baseline": round(len(recovered) / max(1, len(baseline_support)), 3),
        "new_support_not_in_baseline": len(found_support - baseline_support),
        "controls": len(control_ids),
        "control_false_positives": len(control_hits),
        "control_false_positive_rate": round(len(control_hits) / max(1, len(control_ids)), 3),
    }


def agreement(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    shared = set(left) | set(right)
    same = {key for key in shared
            if (left.get(key) or {}).get("relation") == (right.get(key) or {}).get("relation")}
    return {
        "pairs": len(shared),
        "same_relation": len(same),
        "agreement": round(len(same) / max(1, len(shared)), 3),
        "only_left": len(set(left) - set(right)),
        "only_right": len(set(right) - set(left)),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Link protocols compared",
        "",
        f"- view: `{report.get('view')}`",
        f"- claims sampled: {report.get('claim_count')}, candidates: {report.get('candidate_count')}",
        f"- workers: {report.get('workers')}",
        "",
        "## Recall and false positives",
        "",
        "| protocol | calls | decisions | support | recall vs baseline | new support | control FP |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, row in (report.get("protocols") or {}).items():
        stats = row["stats"]
        lines.append(
            f"| {name} | {row['calls']} | {stats['decisions']} | {stats['support']} | "
            f"{stats['recall_vs_baseline']} | {stats['new_support_not_in_baseline']} | "
            f"{stats['control_false_positives']}/{stats['controls']} |"
        )
    lines += ["", "## Agreement between protocols", "",
              "| pair | shared pairs | same relation | agreement |", "| --- | ---: | ---: | ---: |"]
    for name, row in (report.get("agreement") or {}).items():
        lines.append(f"| {name} | {row['pairs']} | {row['same_relation']} | {row['agreement']} |")
    lines.append("")
    return "\n".join(lines)


def resolve_codes(view: Mapping[str, Any], codes: Iterable[str]) -> list[str]:
    """Map display codes (P79) to node ids, so a control list can be re-used."""
    wanted = {str(code) for code in codes}
    question_layer = native_imports()
    found: list[str] = []
    for node in view.get("nodes") or []:
        if (node.get("kind") or "claim") == "answer_root":
            continue
        if question_layer.node_code(node) in wanted:
            found.append(str(node.get("id")))
    return found


def run(view_path: Path, answer_layer_path: Path, *, call_json: Callable[[str], Any],
        protocols: Iterable[str], sample: list[str] | None = None,
        controls: Iterable[str] | None = None,
        limit_claims: int = 20, workers: int | None = 8,
        out: Path | None = None) -> dict[str, Any]:
    question_layer = native_imports()
    view = read_json(Path(view_path))
    answer_layer = read_json(Path(answer_layer_path))
    candidates = [dict(root) for root in (answer_layer.get("roots") or [])]
    if not candidates:
        raise AriadneError("answer layer has no roots")

    by_id = {str(node.get("id")): node for node in view.get("nodes") or []
            if (node.get("kind") or "claim") != "answer_root"}
    control_ids = [cid for cid in (controls or []) if cid in by_id]
    if sample:
        missing = [cid for cid in sample if cid not in by_id]
        if missing:
            raise AriadneError(f"sample ids not in the view: {missing[:3]}")
        # An explicit sample is used as given: silently truncating it to
        # limit_claims made a 311-claim run report numbers from 20 claims.
        chosen = list(sample)
    else:
        baseline = baseline_links(answer_layer)
        linked = sorted({cid for (_, cid) in baseline})
        unlinked = sorted(set(by_id) - set(linked))
        budget = max(1, limit_claims - len(control_ids))
        take_linked = linked[: max(1, budget // 2)]
        take_unlinked = unlinked[: max(1, budget - len(take_linked))]
        chosen = (take_linked + take_unlinked)[:budget]
    chosen = list(dict.fromkeys(chosen + control_ids))
    claims = [claim_entry(by_id[cid], question_layer) for cid in chosen]

    baseline = baseline_links(answer_layer)
    baseline_sample = {key: value for key, value in baseline.items()
                       if key[1] in set(chosen)}

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "view": str(Path(view_path).resolve()),
        "answer_layer": str(Path(answer_layer_path).resolve()),
        "claim_count": len(claims),
        "candidate_count": len(candidates),
        "workers": workers,
        "control_ids": control_ids,
        "protocols": {},
    }
    decisions: dict[str, dict[str, Mapping[str, str]]] = {}
    for protocol in protocols:
        found = run_protocol(protocol, view, candidates, claims, call_json=call_json,
                             workers=workers)
        decisions[protocol] = found
        calls = (len(claims) * len(candidates) if protocol == "pairwise"
                 else len(claims) if protocol == "claim_all" else len(candidates))
        report["protocols"][protocol] = {
            "calls": calls,
            "stats": evaluate(found, baseline_sample, controls=control_ids,
                              candidates=[str(c["code"]) for c in candidates]),
            "decisions": {f"{code}::{cid}": value for (code, cid), value in found.items()},
        }
    pairs = [("pairwise", "claim_all"), ("pairwise", "candidate_all"),
             ("claim_all", "candidate_all")]
    report["agreement"] = {
        f"{left} vs {right}": agreement(decisions.get(left, {}), decisions.get(right, {}))
        for left, right in pairs
        if left in decisions and right in decisions
    }
    report["sample"] = [{"id": claim["id"], "code": claim["code"]} for claim in claims]

    if out is not None:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / "protocols.json", report)
        (out / "protocols.md").write_text(render_markdown(report), encoding="utf-8")
    return report
