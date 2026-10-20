"""Claim-level hygiene applied before cross-paper relation judging.

Three defects are visible in the real per-paper output and none of them is
detected by any schema check, because each artifact is individually well formed:

1. **Pipeline pointers leak into claim text.**  A repaired claim can read
   "... is more than sufficient to explain the heat-capacity observation in
   claim 26. [Referenced claim 26: In bulk NbSe2, ...]".  The pointer ``claim 26``
   means nothing outside the run, and the pasted text duplicates a claim that is
   already its own node.

2. **The same claim is materialized twice.**  Measured on arxiv 2501.08867:
   ``claim_18`` and ``claim_19`` are byte-identical, 298 characters each.  Two
   ids for one proposition is exactly the "same claim counted twice" defect that
   makes a support count meaningless.

3. **Predicate-less fragments are carried as claims.**  "a superconducting
   critical temperature $T_c \\approx 6$ K" has no assertion to be true or false,
   so no relation can hold between it and anything.

Subsumption is judged on the *distinctive* content, not on whole-sentence
similarity: claims about one paper share a long boilerplate preamble ("Density
functional theory calculations using the experimentally validated 4H_a crystal
structure yield an electronic structure with ..."), and whole-sentence Jaccard
then ranks two genuinely different properties as near-duplicates.  Removing the
shared tokens first keeps those apart and still catches a true duplicate, whose
residual is empty.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Mapping

from pipeline_harness.models import JSONDict

#: ``[Referenced claim 26: ...]`` / ``... in claim 26`` and friends.
#: ``[Referenced claim 26: ...]`` / ``[引用的 claim 22：...]`` and friends.  The
#: pipeline writes these in both languages, and the Chinese form survives a regex
#: that only knows the English one -- measured on the 7-paper graph, 50 of 645
#: translated summaries still carried a pointer.
_REF_BLOCK = re.compile(
    r"\[\s*(?:referenced|所引|引用(?:的)?|参见|见|参考)\s*(?:claim|断言|命题|note|注)\s*\d*\s*[:：]?[^\]]*\]",
    re.I,
)
_REF_POINTER = re.compile(
    r"\s*(?:as|in|see|cf\.?|如|见|参见|参考|所引)\s*(?:claim|断言|命题|note|注)\s*_?\d+\s*(?:所述)?",
    re.I,
)
_REF_TRAILING = re.compile(r"\s*\[[^\]]*\b(?:claim|断言|命题)[^\]]*\]\s*$", re.I)

_WORD = re.compile(r"[A-Za-z][A-Za-z\-]{3,}")
#: Numbers must be kept: in this corpus a claim's distinctive content is usually
#: its value.  Without them "B_t evaluates to 154.3 T" looks like a restatement
#: of the formula that defines B_t, and the two get merged.
_NUM = re.compile(r"\d+(?:\.\d+)?")
_STOP = frozenset("""that this with from they their which when where have been being
than then them these those such only also more most less least other others into
over under between among across rather study studies show shows shown using used
based results result however moreover further paper claims claim observed
observation observations""".split())

#: A claim must predicate something.  Bare noun phrases and measurement labels
#: ("a superconducting critical temperature Tc ~ 6 K", "energy-dispersive X-ray
#: spectroscopy of Nb:Se = 0.97(3):2") carry no assertion.
_PREDICATE = re.compile(
    r"\b(is|are|was|were|be|been|being|has|have|had|can|may|might|must|should|could|"
    r"will|would|yield\w*|show\w*|establish\w*|exceed\w*|match\w*|provid\w*|giv\w+|gave|"
    r"found|observ\w+|measur\w+|extract\w*|assess\w*|confirm\w*|acts?|protect\w*|evaluat\w*|"
    r"report\w*|indicat\w*|suggest\w*|demonstrat\w*|result\w*|lead\w*|aris\w*|correspond\w*|"
    r"equal\w*|reach\w*|remain\w*|appear\w*|reflect\w*|represent\w*|consist\w*|contain\w*|"
    r"vari\w*|increas\w*|decreas\w*|reduc\w*|enhanc\w*|suppress\w*|driv\w*|drove|govern\w*|"
    r"impli\w*|follow\w*|predict\w*|support\w*|contradict\w*|explain\w*|interpret\w*|"
    r"attribute\w*|set\w*|defin\w*|identif\w*|determin\w*|depend\w*|differ\w*|scale\w*)\b",
    re.I,
)


def strip_pipeline_pointers(text: str) -> str:
    """Remove internal cross-reference bookkeeping from a claim's text."""
    cleaned = _REF_BLOCK.sub(" ", str(text))
    cleaned = _REF_POINTER.sub(" ", cleaned)
    cleaned = _REF_TRAILING.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # A dangling connector is left behind when the pointer was mid-sentence.
    cleaned = re.sub(r"\s+(?:in|from|to|of|see|cf\.?|如|见|参见)\s*$", "", cleaned, flags=re.I)
    # A stripped block can leave an unmatched closing bracket behind.
    if cleaned.count("[") < cleaned.count("]"):
        cleaned = re.sub(r"\s*\]+\s*$", "", cleaned)
    if cleaned.count("[") > cleaned.count("]"):
        # The block's opening bracket can outlive the removal above.
        cleaned = re.sub(r"\[\s*[:：]?\s*", "", cleaned, count=1)
    cleaned = re.sub(r"\[\s*[:：]?\s*\]", "", cleaned)
    return cleaned.strip()


def is_proposition(text: str, *, min_words: int = 6) -> bool:
    """True when the text asserts something that could be true or false."""
    words = _WORD.findall(str(text))
    if len(words) < min_words:
        return False
    return bool(_PREDICATE.search(str(text)))


def content_tokens(text: str) -> frozenset[str]:
    stripped = re.sub(r"\$[^$]*\$", " ", str(text))
    stripped = re.sub(r"\\[A-Za-z]+", " ", stripped)
    words = {w for w in _WORD.findall(stripped) if w.lower() not in _STOP}
    return frozenset(words | set(_NUM.findall(stripped)))


def normalized_text(text: str) -> str:
    return " ".join(sorted(content_tokens(text)))


#: A labelled physical quantity, e.g. ``T_c^{onset}``, ``T_c^zero``,
#: ``B_{c2}^{\parallel}``, ``T_BKT``, ``B_{c2}^{\perp}``.  The base symbol is the
#: physics (a critical temperature, an upper critical field); the modifier is the
#: *which one* (onset / zero-resistance / in-plane / out-of-plane).
_QUANTITY = re.compile(
    r"\$?\\?([A-Za-z](?:_\{?[A-Za-z0-9]+\}?)?)"
    r"(?:\^\{?([A-Za-z\\]+)\}?|_\{?([A-Za-z]{2,})\}?)?\$?"
)


def quantity_labels(text: str) -> dict[str, set[str]]:
    """Map a base symbol to the modifiers the text attaches to it.

    ``"T_c^onset ≈ 6.61 K"`` yields ``{"t": {"onset"}}``; ``"T_c^zero ≈ 2.40 K"``
    yields ``{"t": {"zero"}}``.  Only recognised physics symbols are returned, so
    ordinary prose does not produce spurious labels.
    """
    found: dict[str, set[str]] = defaultdict(set)
    lowered = str(text)
    for match in re.finditer(
        r"\b([TBEH])[_^]?\{?((?:c2?|BKT|onset|zero|\|\||perp|\\parallel|\\perp))\}?"
        r"(?:\^?\{?([A-Za-z\\|]+)\}?)?", lowered, re.I):
        base = match.group(1).lower()
        first = (match.group(2) or "").lower().strip("\\|{}")
        second = (match.group(3) or "").lower().strip("\\|{}")
        modifier = first if first in {"onset", "zero", "parallel", "perp", "bkt"} else second
        if modifier in {"onset", "zero", "parallel", "perp"}:
            found[base].add(modifier)
    # `T_c^{onset}` style: the modifier sits in the superscript of a `_c` base.
    for match in re.finditer(r"([TBE])_\{?(c2?|BKT)\}?\^\{?([A-Za-z\\|]+)\}?", lowered, re.I):
        modifier = match.group(3).lower().strip("\\|{}")
        if modifier in {"onset", "zero", "parallel", "perp"}:
            found[match.group(1).lower()].add(modifier)

    # Prose modifiers carry the same information when a paper writes
    # "zero-resistance critical temperature" or "experimentally measured onset
    # temperature" instead of a subscript.  Without this pass the one wrong link
    # (the zero-resistance value offered as evidence about the onset value) is
    # invisible, because the conclusion names its quantity in words rather than
    # in a subscript.
    # The base symbol alone is an anchor even when no modifier is recognised:
    # "for T_c ~ 6 K" is still a claim about a critical temperature, and dropping
    # the unqualified base made the one genuinely related pair in the corpus look
    # like it shared nothing.
    if re.search(r"critical temperature|transition temperature|T_\{?c\}", lowered, re.I):
        found.setdefault("t", set())
        if re.search(r"zero[\s\-]?resistance|T_\{?c\}?\^?\{?zero", lowered, re.I):
            found["t"].add("zero")
        if re.search(r"\bonset\b", lowered, re.I):
            found["t"].add("onset")
    if re.search(r"critical field|B_\{?c2|H_\{?c2", lowered, re.I):
        found.setdefault("b", set())
        if re.search(r"in[\s\-]?plane|parallel|\\parallel|\|\|", lowered, re.I):
            found["b"].add("parallel")
        if re.search(r"perpendicular|out[\s\-]?of[\s\-]?plane|orthogonal|\\perp", lowered, re.I):
            found["b"].add("perp")
    return dict(found)


def quantity_conflict(left: str, right: str) -> tuple[str, set[str], set[str]] | None:
    """Report a premise/conclusion pair that measures different quantities.

    Measured failure this guards: paper2ir linked ``T_c^zero ≈ 2.40 K`` as
    evidence for a claim about agreement with the *onset* temperature
    ``T_c^onset ≈ 6.61 K``.  The link is well formed and the numbers are real,
    but the premise measures the other transition temperature, so it cannot
    support that conclusion.  A pair is flagged only when both sides name a
    quantity of the same base symbol and their modifier sets do not intersect,
    which keeps ordinary paraphrase out.
    """
    a, b = quantity_labels(left), quantity_labels(right)
    for base in sorted(set(a) & set(b)):
        if not (a[base] & b[base]):
            return base, a[base], b[base]
    return None


def shares_anchor(left: str, right: str, *, min_shared_terms: int = 4) -> bool:
    """True when two propositions are about the same named thing.

    A ``conjunction`` of two claims that share nothing but a subject area asserts
    only that two unrelated facts are both true, which is vacuous.  Measured
    failure this guards: ``B_c2^perp ~ 5.5 T`` conjoined with ``T_c^zero ~ 2.40 K``.
    Sharing is accepted on any of three concrete anchors -- the same labelled
    quantity (both about a critical temperature), the same numeric value, or
    enough shared distinctive vocabulary that the claims are about one object.
    The vocabulary bound is deliberately high because a shared material name
    alone ("NbSe2") is a subject area, not an object.
    """
    if set(quantity_labels(left)) & set(quantity_labels(right)):
        return True
    a, b = content_tokens(left), content_tokens(right)
    numeric_a = {token for token in a if token[:1].isdigit()}
    numeric_b = {token for token in b if token[:1].isdigit()}
    if numeric_a & numeric_b:
        return True
    shared_words = {token for token in (a & b) if not token[:1].isdigit()}
    return len(shared_words) >= min_shared_terms


def is_subsumed(left: str, right: str, *, max_residual: int = 0) -> bool:
    """True when ``left`` adds nothing over ``right``.

    The default is strict containment (residual 0).  A looser bound was measured
    against real output and absorbed distinct claims: "establish strong
    anisotropy of the upper critical field" has only two content words that the
    sibling claim about the in-plane critical field does not share, but it is a
    different assertion, not a restatement.
    """
    a, b = content_tokens(left), content_tokens(right)
    if not a or not b:
        return False
    return len(a - b) <= max_residual


def apply_hygiene(records: Mapping[str, JSONDict]) -> tuple[dict[str, JSONDict], list[JSONDict]]:
    """Clean claim text, drop non-propositions, and collapse duplicates.

    Duplicates are collapsed **within one Paper Package only**: the same
    proposition asserted by two different papers is independent corroboration and
    is exactly what a cross-paper graph is for.  The surviving id is the one with
    the longer text, so the more specific wording wins, and ties break on the id
    so the result is deterministic.

    Returns the cleaned record map and the merges that were applied.
    """
    cleaned: dict[str, JSONDict] = {}
    for qid, record in records.items():
        content = strip_pipeline_pointers(record["content"])
        if not is_proposition(content):
            continue
        cleaned[qid] = {**record, "content": content}

    by_package: dict[str, list[str]] = defaultdict(list)
    for qid, record in cleaned.items():
        by_package[str(record["package"])].append(qid)

    dropped: dict[str, str] = {}
    merges: list[JSONDict] = []
    for package in sorted(by_package):
        qids = sorted(by_package[package], key=lambda q: (-len(cleaned[q]["content"]), q))
        survivors: list[str] = []
        for qid in qids:
            keeper = None
            for candidate in survivors:
                if is_subsumed(cleaned[qid]["content"], cleaned[candidate]["content"]):
                    keeper = candidate
                    break
            if keeper is None:
                survivors.append(qid)
            else:
                dropped[qid] = keeper
                merges.append({
                    "package": package,
                    "representative_qid": keeper,
                    "merged_qid": qid,
                    "reason": "distinctive content fully contained in the representative",
                })

    for qid in dropped:
        cleaned.pop(qid, None)
    return cleaned, merges


__all__ = [
    "apply_hygiene",
    "content_tokens",
    "is_proposition",
    "is_subsumed",
    "normalized_text",
    "quantity_conflict",
    "quantity_labels",
    "shares_anchor",
    "strip_pipeline_pointers",
]
