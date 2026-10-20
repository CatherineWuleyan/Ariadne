"""Offline demo: run the question layer on the frozen aircraft graph."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config, Runtime, resolve_runtime
from .render import install_outputs, render_outputs
from .util import AriadneError, DEFAULT_CONFIG, REPO, new_run_dir, python_bin, read_json, run, stage_log, write_json


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def run_demo(out: Path | None = None, config_path: Path | None = None,
             runtime: Runtime | None = None) -> dict[str, Any]:
    if runtime is None:
        runtime = resolve_runtime(Config.load())
    runtime.apply_env()
    config_path = config_path or DEFAULT_CONFIG
    config = read_json(config_path)
    question = str(config["question_zh"])
    run_dir = new_run_dir(question, out)
    stage = run_dir / "04_question"
    stage.mkdir(parents=True, exist_ok=True)
    view = _resolve(config["view"])
    roles = _resolve(config["edge_roles"])
    fixture = _resolve(config["fixture"])
    for label, path in (("view", view), ("edge_roles", roles), ("fixture", fixture)):
        if not path.is_file():
            raise AriadneError(f"demo {label} not found: {path}")
    log = stage_log(run_dir, "demo_question_layer")
    print(f"[demo] frozen graph: {view.name}", flush=True)
    run(
        [runtime.python or python_bin(), str(REPO / "pipelines" / "merge" / "pipeline_merge_question" / "run_question_layer.py"),
         "--view", str(view), "--edge-roles", str(roles), "--fixture", str(fixture),
         "--out", str(stage),
         "--question-zh", question, "--question-en", config["question_en"],
         "--domain", config["domain"], "--max-roots", str(config.get("max_roots", 5))],
        log=log, timeout=600,
    )
    answer = stage / "answer_layer.json"
    view_model = stage / "domain_view_question.json"
    if not answer.is_file() or not view_model.is_file():
        raise AriadneError(f"demo question layer did not produce outputs (log {log})")
    rendered = render_outputs(view_model, answer, stage, log_dir=run_dir)
    installed = install_outputs(rendered, run_dir)
    write_json(run_dir / "run.json", {
        "product": "ariadne",
        "mode": "demo",
        "question": {"zh": question, "en": config["question_en"], "domain": config["domain"]},
        "config": str(config_path),
        "runtime": runtime.summary(),
        "outputs": {
            "viewer": installed["html"],
            "chains": installed["markdown"],
            "answer_layer": str(stage / "answer_layer.json"),
            "view_model": str(stage / "domain_view_question.json"),
            "evaluation": str(stage / "evaluation.md"),
        },
        "note": "Offline frozen-aircraft demo; no LKM retrieval or LLM call.",
    })
    print(f"\nAriadne demo complete\n  viewer:   {installed['html']}\n  chains:   {installed['markdown']}", flush=True)
    return {"run_dir": str(run_dir), "viewer": installed["html"], "chains": installed["markdown"]}
