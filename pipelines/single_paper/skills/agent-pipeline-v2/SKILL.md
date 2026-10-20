---
name: agent-pipeline-v2
description: Plan, implement, validate, or review independent Gaia Pipeline Harness workflows and stages while preserving existing Runner, ArtifactRef, checkpoint, and formalization contracts. Use when the user asks to create or change the new Agent Pipeline V2 plugin, add a pipeline stage, configure a workflow, or verify a Harness run.
---

# Agent Pipeline V2

This plugin governs a separate Pipeline Harness workflow. It does not replace or modify the original pipeline.

## Existing contracts

Read before implementing:

- `<repo>/agent/README.md` for architecture and workflow assembly.
- `<repo>/agent/pipeline_harness/plugins.py` for `StagePlugin`, `StageContext`, and `StageResult`.
- `<repo>/agent/pipeline_harness/domain/tools.py` for `DomainTool`.

Reuse the existing runner, artifact store, checkpoint lifecycle, artifact kinds, and status values. Pipeline configuration is an assembly graph, not runtime state.

## Step 1 input contract

The standalone Step 1 pipeline is declared in `pipeline.step1.json` and uses `inputs/manifest.step1.json` as its manifest shape. It requires exactly one `source.paper_text` Markdown input and one approved `source.claims_final` JSON input. The importer creates the single existing `input.bundle` artifact; it stores both frozen inputs under `sources`.

`claims_final.json` must contain `claim`, `note`, and `relation` arrays. Each claim or note has a unique positive `number`, non-empty `conclusion`, and non-empty `text`. Step 1 writes every non-relation item into the top-level `knowledges` map, where the map key is the Knowledge ID. `is_pure_data=true` uses type `observation_claim` and enters `graph.nodes`; other claims and notes use type `claim` / `note`. Each relation becomes one non-reasoning hyperedge: all `connects` values except the final value are `sources`, and the final value is `target`; it is never split into pairwise links. Every relation endpoint must be a graph node; otherwise the stage fails. The Viewer may project this hyperedge through a dashed, display-only relation node; that node is never persisted in `formalization.json`. `paper_text.md` remains in `input.bundle.sources` and is split into tagged-paragraph `workflow.source_anchors` with Markdown line spans.

The local `compiler_projection.py` is not an official Gaia component. It projects only `graph.nodes` into the existing compiler-facing authoring shape; the official Gaia compiler and `validate_local_graph` remain the sole authority for `gaia.ir` output.

## Step 2 contract

`pipeline.step2.json` extends the Step 1 assembly with mechanical paragraph anchoring, text-grounded experiment extraction, and an interactive human gate. Existing Knowledge is matched to the highest token-similarity paper paragraph. Experiment candidates come from Markdown image names and contain each matching paragraph plus one paragraph before and after; insufficient evidence expands that window mechanically before retrying.

The semantic tool uses model `deepseek-v4-flash` and may cite only the supplied paragraph anchors. It returns complete S/A/B/M/R/U values, splits observations from one image only when at least two of S/A/B/M/R differ, and writes English canonical text under `claim_O0*` Knowledge IDs. `observation_proposal` stays outside `graph.nodes`. The developer Viewer renders it with a dashed node boundary. At the command-line gate, `y` rewrites only the selected IDs using the supplied advice; `n` changes every proposal to `observation_claim` and adds it to `graph.nodes`.

`observation_proposal` and `observation_claim` are local authoring types, not official Gaia IR types. The compiler projection excludes proposals and maps approved `observation_claim` nodes to official `claim` Knowledge. It must never project an unreviewed proposal.

## Workflow

1. State the requested pipeline scope and identify the existing stage/tool contracts it can reuse.
2. Prefer deterministic validation and existing Gaia/LKM tools. Use Bohrium LKM v2 for remote read operations, Gaia for formalization/package work, gaia-research for research orchestration, and Paper2tools for workflow or reasoning-chain retrieval when applicable.
3. Keep contract delta at zero. Do not add artifact kinds, persisted states, schema fields, checkpoint rules, or replacement tool wrappers without explicit, item-specific user approval.
4. Implement stage code only as `StagePlugin.run(context) -> StageResult`; load it through the existing `module:object` pipeline configuration mechanism.
5. Verify with the Harness test suite and applicable deterministic validation commands. Report changed files, reused contracts/tools, contract delta, and verification results.

## Boundaries

- Do not modify the original `agent/` pipeline unless the user asks explicitly.
- Do not treat local Paper Graph IDs as global LKM `gcn_` IDs.
- Do not treat ranking scores as probabilities or Gaia priors.
- Treat official Gaia compiler output and validation as authoritative for Gaia IR.
- External writes, including LKM feedback, require the user's explicit authorization.
