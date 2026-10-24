"""Controls that ask whether the link judge measures support or just topic.

Three arms, each cheap and each answering a different question:

* ``reproduce`` -- re-judge the same pool at the same temperature. Measures
  provider noise, not model bias.
* ``representation`` -- same claims, but the judge sees the full claim text,
  its paper title, and one-hop neighbours instead of a 300-character summary.
  If links flip, the judgment was an artifact of what was shown.
* ``negation`` -- the same evidence against a negated candidate. A judge doing
  entailment drops the support; a judge doing topical matching keeps it.
* ``controls`` -- inject established-irrelevant claims (in-domain off-topic and
  cross-domain) into the pool. Anything but ``none`` is a false positive.

Nothing here modifies a view or a stored answer layer. The module is built for
the ``ariadne controls`` command; ``preview`` needs no model call at all.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .util import AriadneError, REPO, read_json, write_json

SCHEMA = "ariadne.controls"
SCHEMA_VERSION = "0.1.0"
REPRESENTATION_CAP = 1200
NEIGHBOUR_COUNT = 3

# Two negations per candidate: a direct denial of the route, and the opposite
# route stated as the recommendation. Mechanical "not" would be a weaker test.
NEGATIONS: list[dict[str, str]] = [
    {
        "code": "R1-N1", "target": "R1", "kind": "direct_denial",
        "statement": (
            "Aircraft simulation design cannot be organized around a single integrated "
            "conceptual-design and simulation-optimization environment: the design stages "
            "are too loosely coupled for one toolchain and one optimizer to cover them."
        ),
    },
    {
        "code": "R1-N2", "target": "R1", "kind": "opposite_route",
        "statement": (
            "Aircraft simulation design should be carried out with separate, loosely coupled "
            "discipline tools and manual hand-off, instead of an integrated environment that "
            "shares one optimizer and one design objective."
        ),
    },
    {
        "code": "R2-N1", "target": "R2", "kind": "direct_denial",
        "statement": (
            "Nested multidisciplinary optimization is not usable for conceptual aircraft "
            "design: coupled structural and aerodynamic constraints cannot be satisfied "
            "inside a single nested formulation, so the approach does not converge."
        ),
    },
    {
        "code": "R2-N2", "target": "R2", "kind": "opposite_route",
        "statement": (
            "Conceptual aircraft design should optimize disciplines sequentially rather than "
            "through nested multidisciplinary optimization levels, because sequential "
            "optimization avoids the cost of a monolithic coupled problem."
        ),
    },
    {
        "code": "R3-N1", "target": "R3", "kind": "direct_denial",
        "statement": (
            "Aircraft simulation cannot be formulated as a coupled nonlinear "
            "differential-algebraic six-degree-of-freedom model: the parameters are not "
            "identifiable from available data, so a full formulation is unusable."
        ),
    },
    {
        "code": "R3-N2", "target": "R3", "kind": "opposite_route",
        "statement": (
            "Aircraft simulation should be formulated as a linearized or reduced-order model "
            "rather than a full nonlinear six-degree-of-freedom differential-algebraic system, "
            "because the nonlinear terms are unnecessary for the intended flight envelope."
        ),
    },
    {
        "code": "R4-N1", "target": "R4", "kind": "direct_denial",
        "statement": (
            "Simulation-model fidelity cannot be improved by identifying aerodynamic deltas "
            "from flight-test or doublet data: that identification is ill-posed, and the "
            "remaining disagreement comes from the wind-tunnel database instead."
        ),
    },
    {
        "code": "R4-N2", "target": "R4", "kind": "opposite_route",
        "statement": (
            "Simulation-model fidelity should be improved by re-measuring the aerodynamic "
            "database directly, not by identifying delta models from flight-test or doublet "
            "data and patching the existing database."
        ),
    },
    {
        "code": "R5-N1", "target": "R5", "kind": "direct_denial",
        "statement": (
            "Aircraft simulation design cannot be validated by real-time "
            "hardware-in-the-loop testing before flight test, because the platform fidelity "
            "is too low for conclusions to transfer to the aircraft."
        ),
    },
    {
        "code": "R5-N2", "target": "R5", "kind": "opposite_route",
        "statement": (
            "Aircraft simulation should be validated by piloted flight test or a full-flight "
            "simulator rather than by ground hardware-in-the-loop testing with integrated "
            "control and propulsion hardware."
        ),
    },
]


def native_imports() -> Any:
    for extra in (REPO, REPO / "agent", REPO / "pipelines/single_paper", REPO / "pipelines/merge"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    from pipeline_merge_question import question_layer

    return question_layer


def _node_text(node: Mapping[str, Any]) -> str:
    details = node.get("details") or {}
    return str(node.get("summary") or details.get("content") or details.get("english") or "")


def _code(node: Mapping[str, Any]) -> str:
    details = node.get("details") or {}
    return str(node.get("label") or details.get("code") or node.get("id") or "")


def _is_claim(node: Mapping[str, Any]) -> bool:
    return (node.get("kind") or "claim") != "answer_root"


def candidate_roots(answer_layer: Mapping[str, Any]) -> list[dict[str, Any]]:
    roots = answer_layer.get("roots") or []
    if not roots:
        raise AriadneError("answer layer has no roots to test")
    return [dict(root) for root in roots]


# Vetted negatives. "Zero lexical overlap" is a filter, not proof of irrelevance:
# a claim about braking dynamics really does bear on the six-degree-of-freedom
# formulation, so it cannot serve as a negative. The list below keeps only claims
# that no candidate could defensibly use, with the reason recorded.
IN_DOMAIN_CONTROL_CODES = (
    ("P79", "bank-change timing of a morphing vehicle; not one of the five routes"),
    ("P95", "top-speed limit of a personal air vehicle; design result, not a method"),
    ("P121", "axis-tilt command integration; control-law detail, not a route"),
    ("P234", "rotor figure of merit in hover; performance result"),
    ("P236", "hover and transition power of one vehicle; performance result"),
    ("P255", "propeller non-dimensionalisation convention"),
    ("P292", "route fuel burn for three missions; operational, not methodological"),
)

_EXCLUDED = {
    "P3": "tooling that could support R1",
    "P69": "fuel-consumption delta could bound R1's cost objective",
    "P83": "longitudinal dynamics equation could support R3",
    "P94": "trim solutions could support R3",
    "P96": "trim behaviour could support R3",
    "P115": "vehicle demonstration could be read as validation",
    "P139": "genetic-algorithm search could support R2",
    "P205": "closed-loop tracking could support R5",
    "P208": "wing sizing under load cases supports R2",
    "P213": "displacement constraint under load cases supports R2",
    "P226": "monolithic minimisation is optimisation practice for R2",
    "P227": "duplicate of P226",
    "P286": "in-flight parameter estimation supports R4",
}


def _control_entry(prefix: str, index: int, text: str, code: str) -> dict[str, Any]:
    """A pool entry that looks like any other claim: no tier in the id or fields."""
    return {
        "id": f"knowledge:papers:{prefix}{index:02d}::claim_1",
        "code": code,
        "paper": f"{prefix}{index:02d}",
        "layer": "claims",
        "degree": 0,
        "seed": False,
        "content": text[:REPRESENTATION_CAP],
    }


def in_domain_controls(view: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Vetted off-topic claims from the same corpus. Metadata stays out of the pool."""
    by_code = {_code(node): node for node in view.get("nodes") or [] if _is_claim(node)}
    entries: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    for index, (source_code, why) in enumerate(IN_DOMAIN_CONTROL_CODES, start=1):
        node = by_code.get(source_code)
        if node is None:
            continue
        entry = _control_entry("paper_ctl", index, _node_text(node), f"CTL{index:02d}")
        entries.append(entry)
        meta.append({"id": entry["id"], "tier": "in_domain",
                     "source_code": source_code, "why": why})
    return entries, meta


def cross_domain_controls(physics_view: Mapping[str, Any]
                          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Claims from a different field: they cannot bear on an aircraft candidate."""
    entries: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    index = 0
    for node in physics_view.get("nodes") or []:
        if not _is_claim(node):
            continue
        details = node.get("details") or {}
        text = str(details.get("english") or details.get("full_english") or "")
        if len(text) < 120:
            continue
        index += 1
        entry = _control_entry("paper_xc", index, text, f"XD{index:02d}")
        entries.append(entry)
        meta.append({"id": entry["id"], "tier": "cross_domain",
                     "source_code": _code(node),
                     "why": "condensed-matter claim; outside aircraft simulation"})
        if index >= 20:
            break
    return entries, meta


def expanded_pool(view: Mapping[str, Any], root: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The normal pool, but each claim carries full text, paper title and neighbours."""
    question_layer = native_imports()
    pool = question_layer.candidate_pool(view, root)
    by_id = {str(node.get("id")): node for node in view.get("nodes") or []}
    adjacency: dict[str, set[str]] = {}
    for edge in view.get("edges") or []:
        source, target = str(edge.get("source")), str(edge.get("target"))
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
    paper_titles: dict[str, str] = {}
    for node in view.get("nodes") or []:
        paper = question_layer.primary_paper_of_node(node)
        details = node.get("details") or {}
        title = str(details.get("paper_title") or (node.get("metadata") or {}).get("paper_title") or "")
        if paper and title and paper not in paper_titles:
            paper_titles[paper] = title
    for item in pool:
        node = by_id.get(str(item["id"])) or {}
        item["content"] = _node_text(node)[:REPRESENTATION_CAP]
        title = paper_titles.get(str(item.get("paper") or ""), "")
        if title:
            # Only present when the view actually carries titles; an empty field
            # in the prompt is noise, not context.
            item["paper_title"] = title
        neighbours = []
        for neighbour_id in sorted(adjacency.get(str(item["id"]), set()))[:NEIGHBOUR_COUNT]:
            neighbour = by_id.get(neighbour_id)
            if neighbour is None or not _is_claim(neighbour):
                continue
            neighbours.append({"code": _code(neighbour), "content": _node_text(neighbour)[:300]})
        item["neighbours"] = neighbours
    return pool


def preview(view: Mapping[str, Any], candidates: list[Mapping[str, Any]],
            *, physics_view: Mapping[str, Any] | None = None) -> str:
    """Everything the controls will send, rendered without any model call."""
    question_layer = native_imports()
    lines = ["# Control constructions", ""]
    root = candidates[0]
    baseline = question_layer.candidate_pool(view, root)
    swapped = expanded_pool(view, root)
    longest = max(swapped, key=lambda item: len(str(item.get("content") or "")))
    original = next((item for item in baseline if item["id"] == longest["id"]), None)
    lines += [
        "## A. Representation swap", "",
        f"- pool entry count is unchanged ({len(baseline)}); only the fields change",
        f"- `content`: 300-char summary -> full claim text, capped at {REPRESENTATION_CAP}",
        f"- added: `neighbours` (up to {NEIGHBOUR_COUNT} one-hop claims, 300 chars each), "
        "plus `paper_title` when the view carries one",
        "- prompt template and candidate set unchanged, so the only variable is what the judge can read",
        "",
        f"Example (`{longest['code']}`, the longest claim in this pool):", "",
        f"- before: {len(str((original or {}).get('content') or ''))} chars of claim text, nothing else",
        f"- after: {len(str(longest['content']))} chars of claim text + "
        f"{len(longest.get('neighbours') or [])} neighbours", "",
        "```json",
        json.dumps({k: v for k, v in {
            "code": longest["code"],
            "content": str(longest["content"]),
            "paper_title": longest.get("paper_title"),
            "neighbours": longest.get("neighbours"),
        }.items() if v is not None}, ensure_ascii=False)[:1200],
        "```", "",
    ]
    lines += ["## B. Negated candidates", "",
              "Two per candidate: a direct denial of the route, and the opposite route stated",
              "as the recommendation. The evidence set is identical to the baseline run.", ""]
    for item in NEGATIONS:
        lines.append(f"- **{item['code']}** ({item['kind']}, against {item['target']}): {item['statement']}")
    lines.append("")
    controls, control_meta = in_domain_controls(view)
    cross, cross_meta = (cross_domain_controls(physics_view)
                         if physics_view is not None else ([], []))
    lines += ["## C. Negative controls", "",
              f"- in-domain: {len(controls)} claims, hand-vetted as irrelevant to all five "
              "routes (zero lexical overlap was the filter, then each was read)",
              f"- cross-domain: {len(cross)} claims from condensed-matter physics",
              "- injected as ordinary pool entries: ids are `paper_ctlNN`, no tier field, "
              "no marker; the mapping to their source is kept only in the report", ""]
    for entry, item in zip(controls + cross, control_meta + cross_meta):
        lines.append(f"- `{entry['id']}` = {item['tier']} / {item['source_code']}: "
                     f"{entry['content'][:100]}")
    if _EXCLUDED:
        lines += ["", "Excluded after reading (they could bear on a route):", ""]
        for code, why in _EXCLUDED.items():
            lines.append(f"- {code}: {why}")
    lines.append("")
    return "\n".join(lines)


ALL_ARMS = ("reproduce", "representation", "negation", "controls", "stability")


def run(view_path: Path, answer_layer_path: Path, *, call_json: Callable[[str], Any],
        out: Path | None = None, physics_view_path: Path | None = None,
        in_domain: int = 20, cross_domain: int = 10,
        arms: Iterable[str] | None = None, repeat: int = 5,
        probe: str | None = None) -> dict[str, Any]:
    """Run all four arms. ``call_json`` is injected so it can be tested offline."""
    question_layer = native_imports()
    view = read_json(Path(view_path))
    answer_layer = read_json(Path(answer_layer_path))
    candidates = candidate_roots(answer_layer)
    question = {"zh": str((answer_layer.get("question") or {}).get("zh") or ""),
                "en": str((answer_layer.get("question") or {}).get("en") or "")}
    baseline_links = {
        str(root["code"]): [dict(link) for link in (root.get("links") or [])]
        for root in candidates
    }

    def judge(roots: list[Mapping[str, Any]], working_view: Mapping[str, Any]):
        return question_layer.link_roots(working_view, question, roots, call_json=call_json)

    selected = set(ALL_ARMS if arms is None else arms)
    unknown = selected - set(ALL_ARMS)
    if unknown:
        raise AriadneError(f"unknown arm(s): {', '.join(sorted(unknown))}")
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "arms_requested": sorted(selected),
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "view": str(Path(view_path).resolve()),
        "answer_layer": str(Path(answer_layer_path).resolve()),
        "temperature": 0,
        "roots": [str(root["code"]) for root in candidates],
        "arms": {},
    }

    # Arm 1: same pool, same temperature. Reproducibility, not accuracy.
    # The negation and controls arms compare against this baseline, so it runs
    # whenever either of them is requested.
    if not selected:
        raise AriadneError("select at least one arm")
    needs_baseline = bool(selected & {"reproduce", "negation", "controls", "representation"})
    reproduced: dict[str, list[dict[str, str]]] = {}
    if needs_baseline:
        reproduced, _ = judge(candidates, view)
    if needs_baseline:
        report["arms"]["reproduce"] = {
            code: _compare_links(baseline_links[code], reproduced.get(code, []))
            for code in baseline_links
        }

    if "representation" in selected:
      # Arm 2: same claims, richer representation.
      swapped_pools = {str(c["code"]): expanded_pool(view, c) for c in candidates}
      swapped_links: dict[str, list[dict[str, str]]] = {}
      for root in candidates:
          code = str(root["code"])
          response = call_json(question_layer.build_link_prompt(
              question, root, swapped_pools[code]))
          swapped_links[code] = question_layer._normalize_links(
              response, root, swapped_pools[code])
      report["arms"]["representation"] = {
          code: _compare_links(reproduced.get(code, []), swapped_links.get(code, []))
          for code in swapped_links
      }

    # Arm 3: negated candidates against the same evidence.
    by_target: dict[str, list[dict[str, str]]] = {}
    for item in NEGATIONS:
        by_target.setdefault(item["target"], []).append(item)
    negation_rows = []
    for root in (candidates if "negation" in selected else []):
        for item in by_target.get(str(root["code"]), []):
            # The negated candidate must look exactly like a normal one: the
            # statement is the manipulation, the identity is not. A code of
            # "R1-N1" or an id containing "negation" would hand the judge the
            # answer, so every arm reuses the ordinary root identity.
            negated = dict(root)
            negated.update({
                "id": str(root.get("id") or "answer:r1"),
                "code": str(root.get("code") or "R1"),
                "statement": item["statement"],
                "title_en": item["statement"][:64],
                "title_zh": item["statement"][:24],
            })
            # Same corpus, same view; only the candidate statement changes.
            pool = question_layer.candidate_pool(view, negated)
            response = call_json(question_layer.build_link_prompt(question, negated, pool))
            links = question_layer._normalize_links(response, negated, pool)
            counts = _count_relations(links)
            base = _count_relations(reproduced.get(str(root["code"]), []))
            negation_rows.append({
                "negation": item["code"],
                "target": str(root["code"]),
                "kind": item["kind"],
                "support_before": base["support"],
                "support_after": counts["support"],
                "oppose_before": base["oppose"],
                "oppose_after": counts["oppose"],
                "bounds_after": counts["bounds"],
                "support_survived": counts["support"] > 0,
                "statement": item["statement"],
            })
    report["arms"]["negation"] = negation_rows

    # Arm 4: established-irrelevant claims inside the pool.
    if "controls" not in selected:
        controls, control_meta = [], []
    else:
        controls, control_meta = in_domain_controls(view)
    if controls and physics_view_path is not None and Path(physics_view_path).is_file():
        extra, extra_meta = cross_domain_controls(read_json(Path(physics_view_path)))
        controls += extra
        control_meta += extra_meta
    control_ids = {str(item["id"]) for item in controls}
    control_tier = {str(item["id"]): item["tier"] for item in control_meta}
    control_rows = []
    for root in (candidates if controls else []):
        code = str(root["code"])
        pool = question_layer.candidate_pool(view, root) + controls
        response = call_json(question_layer.build_link_prompt(question, root, pool))
        links = question_layer._normalize_links(response, root, pool)
        hits = [link for link in links if str(link["claim_id"]) in control_ids]
        real = [link for link in links if str(link["claim_id"]) not in control_ids]
        control_rows.append({
            "root": code,
            "controls_injected": len(controls),
            "control_links": len(hits),
            "control_relations": {rel: sum(1 for l in hits if l["relation"] == rel)
                                  for rel in ("support", "oppose", "bounds")},
            "control_links_by_tier": {
                tier: sum(1 for l in hits if control_tier.get(str(l["claim_id"])) == tier)
                for tier in ("in_domain", "cross_domain")
            },
            "real_links": len(real),
            "real_links_baseline": len(reproduced.get(code, [])),
            "false_positive_rate": round(len(hits) / max(1, len(controls)), 3),
            "control_examples": [
                {"claim_id": link["claim_id"], "relation": link["relation"],
                 "tier": control_tier.get(str(link["claim_id"])),
                 "reason": link["reason"]} for link in hits[:5]
            ],
        })
    report["arms"]["controls"] = control_rows

    # Arm 5: the same prompt, repeated. Temperature 0 is not a promise of one
    # answer, so measure the variance directly instead of assuming it away.
    if "stability" in selected:
        if probe:
            target = next((item for item in NEGATIONS if item["code"] == probe), None)
            if target is None:
                raise AriadneError(f"unknown stability probe: {probe}")
        else:
            target = next((item for item in NEGATIONS
                           if item["target"] == report["roots"][0]), NEGATIONS[0])
        root = next(r for r in candidates if str(r["code"]) == target["target"])
        probe = dict(root)
        probe.update({
            "statement": target["statement"],
            "title_en": target["statement"][:64],
            "title_zh": target["statement"][:24],
        })
        probe_pool = question_layer.candidate_pool(view, probe)
        probe_prompt = question_layer.build_link_prompt(question, probe, probe_pool)
        runs = []
        for index in range(max(1, int(repeat))):
            links = question_layer._normalize_links(
                call_json(probe_prompt), probe, probe_pool)
            counts = _count_relations(links)
            runs.append({"run": index + 1, **counts, "total": len(links)})
        supports = [row["support"] for row in runs]
        report["arms"]["stability"] = {
            "probe": {
                "against": target["target"],
                "negation": target["code"],
                "statement": target["statement"],
            },
            "repeat": len(runs),
            "runs": runs,
            "support_counts": supports,
            "support_outcomes": sorted(set(supports)),
            "support_range": [min(supports), max(supports)],
            "polarity_flips": sum(
                1 for before, after in zip(supports, supports[1:]) if (before > 0) != (after > 0)
            ),
        }
    report["summary"] = _summary(report)

    if out is not None:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / "controls.json", report)
        (out / "controls.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def _count_relations(links: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"support": 0, "oppose": 0, "bounds": 0}
    for link in links:
        relation = str(link.get("relation") or "")
        if relation in counts:
            counts[relation] += 1
    return counts


def _compare_links(before: Iterable[Mapping[str, Any]],
                   after: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    before_map = {str(l["claim_id"]): str(l["relation"]) for l in before}
    after_map = {str(l["claim_id"]): str(l["relation"]) for l in after}
    kept = {cid: rel for cid, rel in before_map.items() if after_map.get(cid) == rel}
    changed = {cid: {"before": rel, "after": after_map.get(cid)}
               for cid, rel in before_map.items()
               if cid in after_map and after_map[cid] != rel}
    dropped = [cid for cid in before_map if cid not in after_map]
    added = [cid for cid in after_map if cid not in before_map]
    union = set(before_map) | set(after_map)
    return {
        "before": len(before_map),
        "after": len(after_map),
        "kept": len(kept),
        "changed": len(changed),
        "dropped": len(dropped),
        "added": len(added),
        "jaccard": round(len(set(before_map) & set(after_map)) / max(1, len(union)), 3),
        "changed_examples": dict(list(changed.items())[:5]),
        "dropped_examples": dropped[:5],
    }


def _summary(report: Mapping[str, Any]) -> dict[str, Any]:
    arms = report.get("arms") or {}
    reproduce = arms.get("reproduce") or {}
    representation = arms.get("representation") or {}
    negations = arms.get("negation") or []
    controls = arms.get("controls") or []
    return {
        "reproduce_jaccard_mean": round(
            sum(v["jaccard"] for v in reproduce.values()) / max(1, len(reproduce)), 3),
        "representation_kept": sum(v["kept"] for v in representation.values()),
        "representation_changed_or_dropped": sum(
            v["changed"] + v["dropped"] for v in representation.values()),
        "representation_before": sum(v["before"] for v in representation.values()),
        "negations_total": len(negations),
        "negations_with_surviving_support": sum(1 for row in negations if row["support_survived"]),
        "control_false_positive_rate": round(
            sum(row["control_links"] for row in controls)
            / max(1, sum(row["controls_injected"] for row in controls)), 3),
        "control_links_total": sum(row["control_links"] for row in controls),
        "controls_injected_per_root": controls[0]["controls_injected"] if controls else 0,
        "stability_support_counts": (arms.get("stability") or {}).get("support_counts"),
        "stability_polarity_flips": (arms.get("stability") or {}).get("polarity_flips"),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") or {}
    arms = report.get("arms") or {}
    lines = [
        "# Link-judge controls",
        "",
        f"- view: `{report.get('view')}`",
        f"- temperature: {report.get('temperature')} (pinned at every call site)",
        f"- roots: {', '.join(report.get('roots') or [])}",
        "",
        "## Summary",
        "",
        f"- reproduction: mean link Jaccard `{summary.get('reproduce_jaccard_mean')}` "
        "over the same pool",
        f"- representation swap: `{summary.get('representation_changed_or_dropped')}` of "
        f"`{summary.get('representation_before')}` links changed or disappeared",
        f"- negation: `{summary.get('negations_with_surviving_support')}` of "
        f"`{summary.get('negations_total')}` negated candidates still received support",
        f"- controls: `{summary.get('control_links_total')}` links to "
        f"`{summary.get('controls_injected_per_root')}` injected irrelevant claims "
        f"(false-positive rate `{summary.get('control_false_positive_rate')}`)",
        "",
        "## Reproduction",
        "",
        "| root | before | after | kept | changed | dropped | jaccard |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for code, row in (arms.get("reproduce") or {}).items():
        lines.append(f"| {code} | {row['before']} | {row['after']} | {row['kept']} | "
                     f"{row['changed']} | {row['dropped']} | {row['jaccard']} |")
    lines += ["", "## Representation swap", "",
              "| root | before | after | kept | changed | dropped | jaccard |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for code, row in (arms.get("representation") or {}).items():
        lines.append(f"| {code} | {row['before']} | {row['after']} | {row['kept']} | "
                     f"{row['changed']} | {row['dropped']} | {row['jaccard']} |")
    lines += ["", "## Negation", "",
              "| negation | target | support before -> after | oppose after | survived |",
              "| --- | --- | --- | ---: | --- |"]
    for row in arms.get("negation") or []:
        lines.append(f"| {row['negation']} | {row['target']} | "
                     f"{row['support_before']} -> {row['support_after']} | "
                     f"{row['oppose_after']} | {row['support_survived']} |")
    lines += ["", "## Negative controls", "",
              "| root | injected | control links | support / oppose / bounds | FP rate | real vs baseline |",
              "| --- | ---: | ---: | --- | ---: | --- |"]
    for row in arms.get("controls") or []:
        relations = row["control_relations"]
        lines.append(f"| {row['root']} | {row['controls_injected']} | {row['control_links']} | "
                     f"{relations['support']} / {relations['oppose']} / {relations['bounds']} | "
                     f"{row['false_positive_rate']} | {row['real_links']} vs {row['real_links_baseline']} |")
    stability = arms.get("stability") or {}
    if stability:
        lines += ["", "## Stability (same prompt, repeated)", "",
                  f"- probe: `{stability['probe']['negation']}` against "
                  f"{stability['probe']['against']}, repeated {stability['repeat']} times",
                  f"- support counts: {stability['support_counts']}",
                  f"- range: {stability['support_range']}, polarity changes between "
                  f"consecutive runs: {stability['polarity_flips']}",
                  "", "| run | support | oppose | bounds | total |",
                  "| ---: | ---: | ---: | ---: | ---: |"]
        for row in stability["runs"]:
            lines.append(f"| {row['run']} | {row['support']} | {row['oppose']} | "
                         f"{row['bounds']} | {row['total']} |")
    lines.append("")
    return "\n".join(lines)
