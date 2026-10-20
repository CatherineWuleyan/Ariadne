"""Render the question viewer and the all-chains Markdown for a view model."""
from __future__ import annotations

import shutil
from pathlib import Path

from .util import REPO, python_bin, run, stage_log


def render_outputs(view_model: Path, answer_layer: Path, output_dir: Path,
                   *, log_dir: Path | None = None) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = log_dir or output_dir
    html = output_dir / "domain_viewer_question.html"
    markdown = output_dir / "ALL_CHAINS_TO_ROOTS.md"
    run(
        [python_bin(), str(REPO / "pipelines" / "merge" / "pipeline_merge_question" / "render_question_viewer.py"),
         "--view-model", str(view_model), "--out", str(html)],
        log=stage_log(log_dir, "render_viewer"), timeout=300,
    )
    run(
        [python_bin(), str(REPO / "pipelines" / "merge" / "pipeline_merge_question" / "export_chains_markdown.py"),
         "--answer-layer", str(answer_layer), "--view", str(view_model), "--out", str(markdown)],
        log=stage_log(log_dir, "render_chains"), timeout=300,
    )
    return {"html": str(html), "markdown": str(markdown)}


def install_outputs(rendered: dict[str, str], run_dir: Path) -> dict[str, str]:
    """Place the two primary deliverables at the run root as well."""
    out = {}
    for key, source in rendered.items():
        dest = run_dir / Path(source).name
        shutil.copyfile(source, dest)
        out[key] = str(dest)
    return out
