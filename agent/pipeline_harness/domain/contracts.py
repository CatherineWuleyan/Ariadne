from __future__ import annotations

import hashlib
import json
from typing import Any


SCHEMA_VERSION = "1.0.0"
VALIDATOR_VERSION = "1.0.0"
FORMALIZATION_SNAPSHOT_SCHEMA = "gaia.formalization.snapshot"
FORMALIZATION_SNAPSHOT_KIND = "formalization.snapshot"
INVALID_FORMALIZATION_SNAPSHOT_KIND = "formalization.snapshot.invalid"
VALIDATION_SCHEMA = "gaia.formalization.validation"
VALIDATION_KIND = "formalization.validation"

STEP_NAMES = {
    1: "step1_extract_evidence",
    2: "step2_normalize_claims",
    3: "step3_analyze_reasoning",
    4: "step4_formalize_reasoning",
    5: "step5_compile_gaia_ir",
}

KNOWLEDGE_TYPES = {"claim", "note", "question", "composition", "setting", "context"}
STRATEGY_FORMS = {"coarse", "formal"}
OPERATOR_TYPES = {
    "implication", "negation", "conjunction", "disjunction",
    "equivalence", "contradiction", "complement",
}
NON_REASONING_LINK_TYPES = {
    "source", "split_from",
    "registry_relation", "scope_limit", "related", "supersedes",
    "subproblem_of", "imported_relation",
}
SOURCE_LOCATOR_TYPES = {"markdown_span", "json_pointer", "figure_region"}


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
