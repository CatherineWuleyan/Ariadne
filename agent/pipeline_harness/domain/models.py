"""Compatibility facade; import new code from contracts, validation, or indexing."""

from .contracts import (
    FORMALIZATION_SNAPSHOT_KIND,
    FORMALIZATION_SNAPSHOT_SCHEMA,
    INVALID_FORMALIZATION_SNAPSHOT_KIND,
    SCHEMA_VERSION,
    STEP_NAMES,
    VALIDATION_KIND,
    VALIDATION_SCHEMA,
    VALIDATOR_VERSION,
    canonical_hash,
)
from .indexing import build_knowledge_index, validate_knowledge_index
from .validation import (
    build_validation_report,
    collect_snapshot_findings,
    make_validation_finding,
    validate_snapshot,
    validate_source_anchor,
)

__all__ = [
    "FORMALIZATION_SNAPSHOT_KIND", "FORMALIZATION_SNAPSHOT_SCHEMA",
    "INVALID_FORMALIZATION_SNAPSHOT_KIND", "SCHEMA_VERSION", "STEP_NAMES",
    "VALIDATION_KIND", "VALIDATION_SCHEMA", "VALIDATOR_VERSION",
    "build_knowledge_index", "build_validation_report", "canonical_hash",
    "collect_snapshot_findings", "make_validation_finding",
    "validate_knowledge_index", "validate_snapshot", "validate_source_anchor",
]
