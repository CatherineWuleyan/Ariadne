"""Run the single-paper pipeline and export its package for merging."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import Config, Runtime, resolve_runtime
from .util import (
    AriadneError,
    REPO,
    harness_env,
    map_ordered,
    parallel_workers,
    python_bin,
    run,
    stage_log,
    write_json,
)


def _run_dir(stdout: str) -> str:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise AriadneError("pipeline_harness init returned no run directory")
    return lines[-1]


def run_one(paper: Mapping[str, Any], stage_dir: Path, run_root: Path,
            python: str) -> dict[str, Any]:
    pid = str(paper["lkm_id"])
    claims = (run_root / "02_paper2ir" / paper["claims_final"]).resolve() if not Path(paper["claims_final"]).is_absolute() else Path(paper["claims_final"])
    text = (run_root / "02_paper2ir" / paper["text"]).resolve() if not Path(paper["text"]).is_absolute() else Path(paper["text"])
    if not claims.is_file() or not text.is_file():
        raise AriadneError(f"{pid}: missing claims/text for single stage")
    manifest = stage_dir / "manifests" / f"{pid}.json"
    write_json(manifest, {"artifacts": [
        {"path": str(text), "kind": "source.paper_text", "media_type": "text/markdown"},
        {"path": str(claims), "kind": "source.claims_final", "media_type": "application/json"},
    ]})
    exporter = REPO / "pipelines" / "single_paper" / "export_artifacts.py"
    if not exporter.is_file():
        raise AriadneError(
            f"single exporter not found: {exporter}; the public checkout is incomplete"
        )
    log = stage_log(stage_dir, f"single_{pid}")
    print(f"[single] {paper.get('title', pid)[:70]}", flush=True)
    init = run(
        [python, "-m", "pipeline_harness", "init",
         "--pipeline", str(REPO / "pipelines" / "single_paper" / "pipeline.json"),
         "--inputs", str(manifest), "--runs-root", str(stage_dir / "runs")],
        env=harness_env(), log=log, timeout=600,
    )
    run_dir = _run_dir(init.stdout)
    run(
        [python, "-m", "pipeline_harness", "run", "--run", run_dir],
        env=harness_env(), log=log, timeout=7200,
    )
    export = run(
        [python, str(exporter),
         run_dir, pid, "--out", str(stage_dir / "export")],
        log=log, timeout=600,
    )
    base = stage_dir / "export" / "graph_single" / pid
    required = ["gaia.ir.json", "formalization.json", "knowledge.index.json"]
    missing = [name for name in required if not (base / name).is_file()]
    if missing:
        raise AriadneError(f"{pid}: single export missing {missing} (log {log})")
    print(f"  exported {base}", flush=True)
    return {
        "lkm_id": pid,
        "title": paper.get("title"),
        "package_dir": str(base),
        "gaia_ir": str(base / "gaia.ir.json"),
        "formalization": str(base / "formalization.json"),
        "knowledge_index": str(base / "knowledge.index.json"),
    }


def run_all(papers: Iterable[Mapping[str, Any]], stage_dir: Path, run_root: Path,
            runtime: Runtime | None = None, workers: int | None = None
            ) -> list[dict[str, Any]]:
    if runtime is None:
        runtime = resolve_runtime(Config.load())
    runtime.apply_env()
    stage_dir.mkdir(parents=True, exist_ok=True)
    python = runtime.python or python_bin()
    # Each paper gets its own harness run directory and its own export folder,
    # so the papers can be processed concurrently.
    ordered = list(papers)
    workers = parallel_workers(workers, ceiling=len(ordered))
    if workers > 1 and len(ordered) > 1:
        print(f"[single] {len(ordered)} papers, {workers} in parallel", flush=True)
    results, errors = map_ordered(
        lambda paper: run_one(paper, stage_dir, run_root, python), ordered, workers
    )
    if errors:
        # Unlike paper2ir, merge cannot proceed with a missing package, so the
        # first failure still stops the stage -- with the paper it belongs to.
        paper, exc = errors[0]
        raise AriadneError(f"{paper.get('lkm_id')}: {exc}")
    write_json(stage_dir / "packages.json", {"packages": results})
    return results
