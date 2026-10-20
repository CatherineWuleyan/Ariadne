"""Merge-specific Step 5 adapter.

The merge input manifest may carry each paper's source formalization for
provenance, while Step 4 emits the single integration formalization that must
be compiled.  The generic compiler requires one formalization, so this thin
adapter scopes its input view to the Step 4 product and delegates all compile
and audit behavior to the official harness plugin.
"""
from __future__ import annotations

from dataclasses import replace

from pipeline_harness.domain.compiler import Step5CompileGaiaIRPlugin
from pipeline_harness.plugins import StageContext, StageResult


class Step5CompileMergedGaiaIRPlugin(Step5CompileGaiaIRPlugin):
    """Compile only the formalization produced by merge Step 4."""

    def run(self, context: StageContext) -> StageResult:
        merge_refs = [
            ref for ref in context.inputs
            if ref.kind == "formalization"
            and ref.producer_stage == "step4_formalize_integration"
        ]
        if len(merge_refs) != 1:
            raise ValueError(
                "merge Step 5 requires exactly one Step 4 integration formalization; "
                f"found {len(merge_refs)}"
            )
        scoped = replace(context, inputs=[
            ref for ref in context.inputs
            if ref.kind != "formalization" or ref in merge_refs
        ])
        return super().run(scoped)


__all__ = ["Step5CompileMergedGaiaIRPlugin"]
