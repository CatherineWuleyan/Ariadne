"""One operand set, one relation type.

Step 3 judges a candidate pair inside *every* Step 2 group that contains it, and
those groups overlap: the same two propositions can come back as ``conjunction``
from one group and ``disjunction`` (or ``equivalence``, or ``contradiction``)
from another.  Two things let both verdicts survive:

* ``_validate_operator`` checks one operator in isolation, so each verdict is
  individually valid;
* ``_stable_id`` puts the type in the operator id, so the conflicting verdicts
  never collide in the id- or JSON-based deduplication.

The viewer then draws one edge per operator, so the rendered graph carries
parallel edges whose ``semantic_type`` values are mutually exclusive.  That is an
assertion the relation gate never made, and no reader can resolve it.

``equivalence``, ``contradiction``, ``conjunction`` and ``disjunction`` are all
statements *about one operand set*, so at most one of them can hold.  When an
operand set was typed more than once it has no single decided relation, and the
conflict is not resolved by precedence: picking a winner would invent a relation
the gate never uniquely asserted.  The whole operand set is dropped and reported
so the run stays deterministic and the upstream typing error is observable.

The same rule is applied to the *rendered* arms, because a k-ary operator is
drawn as a star from its first operand: two different operand sets can collapse
onto one drawn pair with mutually exclusive labels.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from pipeline_harness.models import JSONDict

#: Operator types that make a claim about the operand set as a whole rather than
#: about one operand following from the others.  Two different members cannot
#: both hold for the same operand set.
EXCLUSIVE_OPERATOR_TYPES = frozenset({"equivalence", "contradiction", "conjunction", "disjunction"})


def operand_key(operator: Mapping[str, Any]) -> frozenset[str]:
    """The unordered operand set an operator is about."""
    return frozenset(str(item) for item in operator.get("variables") or [])


def reconcile_operator_types(
    operators: Iterable[Mapping[str, Any]],
) -> tuple[list[JSONDict], list[JSONDict]]:
    """Drop every operand set that the gate typed more than once.

    Returns the surviving operators in their original order, and the conflicting
    operand sets as ``{"variables": [...], "types": [...], "operator_ids": [...]}``
    sorted by operand set.  Operators with fewer than two operands (``negation``)
    and malformed operators pass through untouched: they cannot conflict, and
    dropping them here would hide a different defect.
    """
    items = [dict(item) for item in operators]
    groups: dict[frozenset[str], list[int]] = defaultdict(list)
    for index, item in enumerate(items):
        key = operand_key(item)
        if len(key) >= 2:
            groups[key].append(index)

    dropped: set[int] = set()
    conflicts: list[JSONDict] = []
    for key, indexes in groups.items():
        types = sorted({str(items[index].get("type") or "") for index in indexes})
        if len(types) > 1:
            dropped.update(indexes)
            conflicts.append({
                "variables": sorted(key),
                "types": types,
                "operator_ids": sorted(str(items[index].get("id") or "") for index in indexes),
            })

    kept: list[JSONDict] = []
    seen: set[tuple[frozenset[str], str]] = set()
    for index, item in enumerate(items):
        if index in dropped:
            continue
        # Two spellings of one operator (same operand set, same type, same id)
        # are one operator; the first occurrence wins so the order is stable.
        key = operand_key(item)
        identity = (key, str(item.get("id") or ""))
        if len(key) >= 2 and identity in seen:
            continue
        seen.add(identity)
        kept.append(item)
    conflicts.sort(key=lambda conflict: conflict["variables"])
    return kept, conflicts


def reconcile_rendered_arms(
    arms: Iterable[Sequence[str]],
) -> tuple[list[tuple[str, str, str]], list[JSONDict]]:
    """Drop drawn ``(source, target, label)`` arms whose pair is contradictory.

    Only the exclusive operator types are checked: ``deduction`` / ``abduction`` /
    ``infer`` describe how one operand follows from the others and may sit on the
    same pair as an operator.  A pair carrying two *different* exclusive labels is
    dropped whole, so a stale artifact cannot render a self-contradictory edge.
    """
    items = [(str(arm[0]), str(arm[1]), str(arm[2])) for arm in arms]
    groups: dict[frozenset[str], list[int]] = defaultdict(list)
    for index, (source, target, label) in enumerate(items):
        if label in EXCLUSIVE_OPERATOR_TYPES:
            groups[frozenset({source, target})].append(index)

    dropped: set[int] = set()
    conflicts: list[JSONDict] = []
    for key, indexes in groups.items():
        labels = sorted({items[index][2] for index in indexes})
        if len(labels) > 1:
            dropped.update(indexes)
            conflicts.append({"pair": sorted(key), "labels": labels})

    kept: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, arm in enumerate(items):
        if index in dropped or arm in seen:
            continue
        seen.add(arm)
        kept.append(arm)
    conflicts.sort(key=lambda conflict: conflict["pair"])
    return kept, conflicts


__all__ = [
    "EXCLUSIVE_OPERATOR_TYPES",
    "operand_key",
    "reconcile_operator_types",
    "reconcile_rendered_arms",
]
