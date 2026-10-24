"""Stage 4: merge paper packages and add the question-led answer layer."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import Config, Runtime, resolve_runtime
from .util import AriadneError, REPO, harness_env, python_bin, read_json, run, stage_log, write_json

KINDS = {
    "gaia.ir.json": "gaia.ir",
    "formalization.json": "formalization",
    "knowledge.index.json": "knowledge.index",
}


def _selection(packages: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, str]], list[str]]:
    artifacts: list[dict[str, str]] = []
    selection: list[str] = []
    for package in packages:
        base = Path(str(package["package_dir"]))
        for filename, kind in KINDS.items():
            key = {"gaia.ir": "gaia_ir", "formalization": "formalization", "knowledge.index": "knowledge_index"}[kind]
            path = Path(str(package.get(key) or base / filename))
            if not path.is_file():
                if kind != "gaia.ir":
                    continue  # Gaia IR is required; authoring context/index are optional.
                raise AriadneError(f"missing {path}; single stage incomplete")
            artifacts.append({"path": str(path), "kind": kind, "media_type": "application/json"})
        ir_path = Path(str(package.get("gaia_ir") or base / "gaia.ir.json"))
        ir = read_json(ir_path)
        name = f"{ir.get('namespace')}:{ir.get('package_name')}"
        if not ir.get("namespace") or not ir.get("package_name"):
            raise AriadneError(f"no package identity in {ir_path}")
        selection.append(name)
    return artifacts, selection


def build_pipeline(stage_dir: Path, question: str, question_en: str,
                   domain: str, max_roots: int, selection: list[str], *,
                   mode: str = "bootstrap", scope_domain: str | None = None) -> Path:
    source = REPO / "pipelines" / "merge" / "pipeline.question.json"
    config = read_json(source)
    for stage in config.get("stages") or []:
        name = stage.get("name")
        if name == "step0_select_scope":
            stage["options"] = {
                "mode": mode,
                "scope": {"domain": scope_domain or domain, "selection": selection},
            }
        elif name == "step6_question_answer_layer":
            options = dict(stage.get("options") or {})
            options.update({
                "question_zh": question,
                "question_en": question_en,
                "domain": domain,
                "max_roots": max_roots,
            })
            stage["options"] = options
    path = stage_dir / "pipeline.json"
    write_json(path, config)
    return path


def run_question_pipeline(
    packages: Iterable[Mapping[str, Any]],
    stage_dir: Path,
    *,
    question: str,
    question_en: str,
    domain: str,
    max_roots: int,
    runtime: Runtime | None = None,
    mode: str = "bootstrap",
    selection: list[str] | None = None,
    scope_domain: str | None = None,
) -> dict[str, Any]:
    if runtime is None:
        runtime = resolve_runtime(Config.load())
    runtime.apply_env()
    python = runtime.python or python_bin()
    stage_dir.mkdir(parents=True, exist_ok=True)
    artifacts, identities = _selection(packages)
    selection = identities if selection is None else selection
    if len(identities) < 2 and mode == "bootstrap":
        raise AriadneError("merge needs at least two paper packages; retrieve more papers")
    manifest = stage_dir / "inputs.json"
    write_json(manifest, {"artifacts": artifacts})
    config = build_pipeline(stage_dir, question, question_en, domain, max_roots, selection,
                            mode=mode, scope_domain=scope_domain)
    log = stage_log(stage_dir, "merge")
    print(f"[merge] {len(selection)} packages", flush=True)
    init = run(
        [python, "-m", "pipeline_harness", "init", "--pipeline", str(config),
         "--inputs", str(manifest), "--runs-root", str(stage_dir / "runs")],
        env=harness_env(), log=log, timeout=600,
    )
    lines = [line.strip() for line in init.stdout.splitlines() if line.strip()]
    if not lines:
        raise AriadneError("pipeline_harness init returned no run directory")
    run_dir = Path(lines[-1])
    run(
        [python, "-m", "pipeline_harness", "run", "--run", str(run_dir)],
        env=harness_env(), log=log, timeout=14400,
    )
    if read_json(run_dir / "run.json").get("status") != "succeeded":
        raise AriadneError(f"merge did not succeed: {run_dir}")
    run([python, "-m", "pipeline_harness", "check", "--run", str(run_dir)],
        env=harness_env(), log=log, timeout=600)
    view = run(
        [python, "-m", "pipeline_harness", "view", "--run", str(run_dir)],
        env=harness_env(), log=log, timeout=1200,
    )
    view_path = run_dir / "views" / "view_model.json"
    if view.returncode != 0 or not view_path.is_file():
        raise AriadneError(f"merge view failed (log {log})")
    return {
        "run_dir": str(run_dir),
        "view_model": str(view_path),
        "answer_layer_artifact": str(run_dir / "views" / "view_model.json"),
        "selection": selection,
    }


def extract_answer_layer(view_model: Path, dest_dir: Path) -> tuple[Path, Path]:
    view = read_json(view_model)
    answer = view.get("answer_layer") or (view.get("metadata") or {}).get("answer_layer")
    if not answer:
        raise AriadneError(
            "view model has no answer layer; Step 6 did not produce question.answer_layer"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    answer_path = dest_dir / "answer_layer.json"
    view_path = dest_dir / "domain_view_question.json"
    write_json(answer_path, answer)
    write_json(view_path, view)
    return answer_path, view_path
