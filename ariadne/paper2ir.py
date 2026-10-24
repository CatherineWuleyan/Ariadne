"""Stage 2: paper graph -> claims_final via the repo's paper2ir pipeline.

The 14 steps are not reimplemented.  A run-local copy of
``pipelines/clean_claims`` is created (scripts only, no old data), its own
``run_full_pipeline.STEPS`` list is imported, and ``run_step`` is called exactly
as the maintained runner does.  This keeps the stage isolated and resumable.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import Config, Runtime, resolve_runtime
from .util import AriadneError, REPO, python_bin, read_json, run, stage_log, write_json

EMPTY_INSTANCE_OUTPUTS = frozenset({
    "instance_target_positions.json",
    "claim_instance_associations.json",
})
STEP_RETRIES = 3
RETRY_DELAYS = (10, 30, 60)
TRANSIENT_MARKERS = (
    "connectionreseterror", "connection reset by peer", "connection aborted",
    "remotedisconnected", "incompleteread", "timeout", "timed out",
    "urlerror", "temporary failure", "http error 429", "http error 500",
    "http error 502", "http error 503", "http error 504", "eof occurred",
    "broken pipe",
)
IGNORE = shutil.ignore_patterns(
    "data", "__pycache__", "*.pyc", ".DS_Store", "*.log", "tests", "test_*.py",
)


def _copy_pipeline(dest: Path) -> Path:
    source = REPO / "pipelines" / "clean_claims"
    if not source.is_dir():
        raise AriadneError(f"paper2ir pipeline not found: {source}")
    dest = dest.resolve()
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, ignore=IGNORE)
    return dest


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location("ariadne_paper2ir_core", path)
    if spec is None or spec.loader is None:
        raise AriadneError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _find_cached_claims(pid: str, *, inputs: Path | None = None,
                        pipeline: Path | None = None,
                        current_model: tuple[str, str] | None = None) -> Path | None:
    """Find claims; production callers must match source bytes and pipeline code.

    An ID identifies a paper, not a particular version of its text or graph.
    Legacy entries without the inputs/code needed to establish equality miss.
    """
    def usable(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= 10:
            return False
        if inputs is None:
            return True
        try:
            read_json(path)
            if current_model is not None:
                prior = read_json(path.parents[3] / "run.json")
                model = (prior.get("runtime") or {}).get("model") or {}
                if prior.get("status") != "complete" or (model.get("model"), model.get("base_url")) != current_model:
                    return False
            if any((path.parent / name).read_bytes() != (inputs / name).read_bytes()
                   for name in ("graph.json", "ocr.md")):
                return False
            if pipeline is not None:
                previous = path.parents[2] / "pipeline"
                for source in pipeline.rglob("*"):
                    relative = source.relative_to(pipeline)
                    if source.is_file() and "data" not in relative.parts and "__pycache__" not in relative.parts:
                        if (previous / relative).read_bytes() != source.read_bytes():
                            return False
            return True
        except (OSError, ValueError):
            return False
    runs = REPO / "outputs" / "ariadne_runs"
    if runs.is_dir():
        paths = sorted(
            runs.glob(f"*/02_paper2ir/data/{pid}/claims_final.json"),
            key=lambda item: item.stat().st_mtime, reverse=True,
        )
        for path in paths:
            if usable(path):
                return path
    legacy = REPO / "pipelines" / "clean_claims" / "data" / pid / "claims_final.json"
    if usable(legacy):
        return legacy
    return None


def _is_transient(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in TRANSIENT_MARKERS)


def _run_step(script: Path, pid: str, pipeline_dir: Path, python: str,
              log: Path) -> Any:
    proc = None
    fallback_model = os.environ.get("ARIADNE_FALLBACK_MODEL", "deepseek-v4-flash")
    for attempt in range(STEP_RETRIES + 1):
        env = dict(os.environ, PYTHONPATH=str(pipeline_dir))
        if attempt > 0 and fallback_model:
            # A stalled/overloaded primary model should not consume every retry.
            env["DEEPSEEK_MODEL"] = fallback_model
        proc = run(
            [python, str(script), pid],
            cwd=pipeline_dir, env=env,
            log=log, timeout=7200, check=False,
        )
        if proc.returncode == 0:
            return proc
        tail = (proc.stderr or proc.stdout or "").strip()[-1200:]
        if attempt < STEP_RETRIES and _is_transient(tail):
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            print(
                f"  transient failure; retrying in {delay}s "
                f"(attempt {attempt + 1}/{STEP_RETRIES + 1})",
                flush=True,
            )
            time.sleep(delay)
            continue
        return proc
    return proc


def run_one(paper: Mapping[str, Any], stage_dir: Path, pipeline_dir: Path,
            module: Any, run_root: Path, python: str, *, reuse: bool = True) -> dict[str, Any]:
    pid = str(paper["lkm_id"])
    stage_dir = stage_dir.resolve()
    pipeline_dir = pipeline_dir.resolve()
    run_root = run_root.resolve()
    paper_dir = stage_dir / "data" / pid
    paper_dir.mkdir(parents=True, exist_ok=True)
    for name in ("graph.json", "ocr.md"):
        source = Path(str(paper.get("graph_path") if name == "graph.json" else paper.get("ocr_path")))
        if not source.is_absolute():
            source = run_root / source
        if not source.is_file():
            raise AriadneError(f"{pid}: missing {name} at {source}")
        shutil.copyfile(source, paper_dir / name)

    log = stage_log(stage_dir, f"paper2ir_{pid}")
    claims = paper_dir / "claims_final.json"
    if reuse and not (claims.is_file() and claims.stat().st_size > 10):
        cached = _find_cached_claims(pid, inputs=paper_dir, pipeline=pipeline_dir,
                                     current_model=(os.environ.get("DEEPSEEK_MODEL", ""),
                                                    os.environ.get("DEEPSEEK_BASE_URL", "")))
        if cached is not None:
            shutil.copyfile(cached, claims)
            print(
                f"[paper2ir] {paper.get('title', pid)[:70]} "
                f"(cached claims from {cached.parent.parent.parent.parent.name})",
                flush=True,
            )
    if claims.is_file() and claims.stat().st_size > 10:
        print(f"[paper2ir] {paper.get('title', pid)[:70]} (reusing claims_final.json)",
              flush=True)
        return {
            "lkm_id": pid,
            "title": paper.get("title"),
            "claims_final": str(claims.relative_to(stage_dir)),
            "graph": str((paper_dir / "graph.json").relative_to(stage_dir)),
            "text": str((paper_dir / "ocr.md").relative_to(stage_dir)),
        }
    print(f"[paper2ir] {paper.get('title', pid)[:70]}", flush=True)
    for index, (script_rel, expected_output, _needs_api) in enumerate(module.STEPS, start=1):
        script = (pipeline_dir / script_rel).resolve()
        expected = paper_dir / expected_output
        # Keep the child's output in the product log, not in the terminal.
        proc = _run_step(script, pid, pipeline_dir, python, log)
        if proc.returncode == 0 and expected.is_file():
            print(f"  [{index:02d}/{len(module.STEPS)}] {script_rel} ok", flush=True)
            continue
        if expected_output in EMPTY_INSTANCE_OUTPUTS and proc.returncode == 0:
            expected.write_text("[]\n", encoding="utf-8")
            print(f"  [{index:02d}/{len(module.STEPS)}] {script_rel} empty -> []", flush=True)
            continue
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise AriadneError(f"{pid}: paper2ir stopped at {script_rel} (see {log})\n{tail}")
    claims = paper_dir / "claims_final.json"
    if not claims.is_file():
        raise AriadneError(f"{pid}: paper2ir finished without claims_final.json")
    return {
        "lkm_id": pid,
        "title": paper.get("title"),
        "claims_final": str(claims.relative_to(stage_dir)),
        "graph": str((paper_dir / "graph.json").relative_to(stage_dir)),
        "text": str((paper_dir / "ocr.md").relative_to(stage_dir)),
    }


def run_all(papers: Iterable[Mapping[str, Any]], stage_dir: Path, run_root: Path,
            runtime: Runtime | None = None, min_papers: int = 2, reuse: bool = True
            ) -> list[dict[str, Any]]:
    if runtime is None:
        runtime = resolve_runtime(Config.load())
    runtime.apply_env()
    stage_dir.mkdir(parents=True, exist_ok=True)
    pipeline_dir = _copy_pipeline(stage_dir / "pipeline")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise AriadneError(
            "No OpenAI-compatible model key is configured. Run "
            "`ariadne config init`, set DEEPSEEK_API_KEY, or use `ariadne demo`."
        )
    os.environ.setdefault("DEEPSEEK_MODEL", "deepseek-v4-flash")
    # Large paper2ir prompts can exceed the historical 180s read timeout.
    os.environ.setdefault("DEEPSEEK_TIMEOUT_SECONDS", "600")
    python = runtime.python or python_bin()
    # The 14 step scripts each derive ``PROJECT_ROOT/data`` from their own
    # location.  Keep the run's data in the stage dir but expose it at the
    # pipeline copy's expected ``data/`` path via a symlink, otherwise the
    # imported STEPS list and the step scripts disagree about where to read.
    data_dir = stage_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    pipeline_data = pipeline_dir / "data"
    if not pipeline_data.exists():
        pipeline_data.symlink_to(data_dir, target_is_directory=True)
    module = _load_module(pipeline_dir / "run_full_pipeline.py")
    module.PROJECT_ROOT = pipeline_dir
    module.DATA_DIR = pipeline_data
    results = []
    failures: list[dict[str, Any]] = []
    for paper in papers:
        try:
            args = (paper, stage_dir, pipeline_dir, module, run_root, python)
            results.append(run_one(*args) if reuse else run_one(*args, reuse=False))
        except AriadneError as exc:
            failures.append({
                "lkm_id": paper.get("lkm_id"),
                "title": paper.get("title"),
                "error": str(exc)[:2000],
            })
            print(
                f"[paper2ir] skipped {paper.get('lkm_id')}: "
                f"{str(exc).splitlines()[0][:180]}",
                flush=True,
            )
    write_json(
        stage_dir / "papers.json",
        {"papers": results, "failures": failures},
    )
    if len(results) < int(min_papers):
        raise AriadneError(
            f"paper2ir produced {len(results)} usable paper(s); "
            f"need at least {min_papers}. Failures: "
            f"{stage_dir / 'papers.json'}"
        )
    return results
