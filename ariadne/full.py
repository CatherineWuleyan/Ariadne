"""End-to-end Ariadne run: question -> LKM -> paper2ir -> single -> merge -> HTML/MD."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import __version__, merge, paper2ir, retrieval, single
from .config import (
    Config,
    Runtime,
    bohr_status,
    bohr_version,
    python_version,
    resolve_runtime,
)
from .render import install_outputs, render_outputs
from .util import (
    AriadneError,
    REPO,
    file_digest,
    git_state,
    new_run_dir,
    read_json,
    write_json,
)

REQUIRED_FILES = frozenset({
    "agent/pipeline_harness/__init__.py",
    "pipelines/clean_claims/run_full_pipeline.py",
    "pipelines/clean_claims/step1_process_conclusions/step1a_pipeline.py",
    "pipelines/single_paper/pipeline.json",
    "pipelines/merge/pipeline.question.json",
    "pipelines/merge/pipeline_merge_question/run_question_layer.py",
    "pipelines/merge/pipeline_merge_question/render_question_viewer.py",
    "pipelines/merge/pipeline_merge_question/export_chains_markdown.py",
    "pipelines/merge/pipeline_merge_question/question_layer.py",
    "pipelines/single_paper/export_artifacts.py",
})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _missing_files() -> list[str]:
    return sorted(
        rel for rel in REQUIRED_FILES if not (REPO / rel).is_file()
    )


def _preflight(runtime: Runtime, paper_spec: Path | None) -> dict[str, Any]:
    """Fail before any billable call or run-directory creation."""
    missing = _missing_files()
    if missing:
        raise AriadneError(
            "The checkout is missing required pipeline files:\n  - "
            + "\n  - ".join(missing)
            + "\nUse a full clone of https://github.com/CatherineWuleyan/ariadne."
        )
    python = Path(runtime.python)
    if not python.is_file():
        raise AriadneError(
            f"configured Python does not exist: {runtime.python}\n"
            "Set `runtime.python` via `ariadne config set runtime.python <path>` "
            "or the ARIADNE_PYTHON environment variable."
        )
    if not runtime.model_api_key:
        message = (
            "No model API key is configured. Run `./run.sh config init`, "
            "set ARIADNE_MODEL_API_KEY / DEEPSEEK_API_KEY, or use `ariadne demo`."
        )
        if runtime.anthropic_key_present:
            message += (
                "\nANTHROPIC_API_KEY is set, but the bundled paper2ir/single/merge "
                "stages require an OpenAI-compatible endpoint. Set a DeepSeek or "
                "OpenAI-compatible key instead."
            )
        raise AriadneError(message)
    details: dict[str, Any] = {
        "python_version": python_version(runtime.python),
        "model_key_source": runtime.sources.get("model_api_key", "missing"),
        "bohr_version": bohr_version(runtime),
    }
    needs_live_lkm = paper_spec is None
    if paper_spec is not None:
        if not paper_spec.is_file():
            raise AriadneError(f"--papers file not found: {paper_spec}")
        needs_live_lkm = _spec_needs_lkm(paper_spec)[0]
    if needs_live_lkm:
        if not runtime.bohr_bin:
            raise AriadneError(
                "bohr CLI not found. Install it with `npm install -g bohr-cli`, "
                "run `ariadne auth bohr`, or use --papers with local files."
            )
        status = bohr_status(runtime)
        details["bohr"] = status
        if not status.get("logged_in"):
            profile = runtime.bohr_profile or "(active profile)"
            raise AriadneError(
                f"Bohr is not logged in for profile {profile}. Run "
                "`./run.sh auth bohr`, or use --papers with local "
                "graph/text files.\n"
                + str(status.get("error") or "")
            )
    else:
        details["bohr"] = {"used": False, "reason": "offline paper spec"}
    return details


def _spec_needs_lkm(paper_spec: Path | None) -> tuple[bool, int]:
    if paper_spec is None:
        return True, 0
    records = retrieval.read_spec(paper_spec)
    remote = sum(1 for record in records if not record.get("graph_path"))
    return bool(remote), remote


def _retrieval_options(runtime: Runtime, *, max_queries: int | None = None,
                       candidate_limit: int | None = None,
                       use_query_planner: bool | None = None,
                       use_lkm_extract: bool | None = None,
                       max_extractions: int | None = None,
                       cross_check: bool | None = None,
                       min_corroboration: int | None = None,
                       external_discovery: bool | None = None
                       ) -> dict[str, Any]:
    config = runtime.config
    return {
        "max_queries": int(
            max_queries if max_queries is not None
            else config.get("retrieval.queries", 5)
        ),
        "per_query": int(config.get("retrieval.per_query", 8)),
        "candidate_limit": int(
            candidate_limit if candidate_limit is not None
            else config.get("retrieval.candidate_limit", 16)
        ),
        "min_papers": int(config.get("retrieval.min_papers", 2)),
        "min_graph_nodes": int(config.get("retrieval.min_graph_nodes", 3)),
        "min_relevance": float(config.get("retrieval.min_relevance", 0.2)),
        "use_query_planner": bool(
            config.get("retrieval.use_query_planner", True)
            if use_query_planner is None else use_query_planner
        ),
        "use_lkm_extract": bool(
            config.get("retrieval.use_lkm_extract", True)
            if use_lkm_extract is None else use_lkm_extract
        ),
        "max_extractions": int(
            max_extractions if max_extractions is not None
            else config.get("retrieval.max_extractions", 5)
        ),
        "cross_check": bool(
            config.get("retrieval.cross_check", True)
            if cross_check is None else cross_check
        ),
        "cross_check_sources": str(
            config.get(
                "retrieval.cross_check_sources",
                retrieval.DEFAULT_CROSS_SOURCES,
            )
        ),
        "min_corroboration": int(
            min_corroboration if min_corroboration is not None
            else config.get("retrieval.min_corroboration", 1)
        ),
        "external_discovery": bool(
            config.get("retrieval.external_discovery", False)
            if external_discovery is None else external_discovery
        ),
        "external_candidates": int(
            config.get("retrieval.external_candidates", 4)
        ),
    }


def _confirm_spend(runtime: Runtime, top_k: int, needs_lkm: bool,
                   remote: int, fulltext: bool,
                   options: Mapping[str, Any]) -> None:
    if not needs_lkm or runtime.auto_approve_spend:
        return
    if remote:
        calls = remote
        extraction_calls = 0
        detail = f"{remote} graph call(s)"
    else:
        calls = int(options["max_queries"]) + int(options["candidate_limit"]) * 2
        extraction_calls = (
            int(options["max_extractions"]) if options["use_lkm_extract"] else 0
        )
        detail = (
            f"up to {options['max_queries']} search queries + "
            f"up to {options['candidate_limit']} first-pass graph probes "
            f"(extended only if needed)"
        )
        if extraction_calls:
            detail += f" + up to {extraction_calls} LKM graph extraction(s)"
    cost = calls * 0.05 + extraction_calls * 1.0
    print(
        "\n[spend] This run makes billable Bohrium LKM calls: "
        f"up to ~{cost:.2f} CNY, before the monthly 1,000-call free quota. "
        + detail + "."
    )
    if extraction_calls:
        print("[spend] LKM extraction is 1.00 CNY per paper (0.10 CNY on a cache hit).")
    if fulltext:
        print("[spend] PDF parsing, when an arXiv id is available, may add charges.")
    if not sys.stdin.isatty():
        raise AriadneError(
            "Billable LKM calls need explicit approval. Re-run with `--yes`, "
            "or set `bohr.auto_approve_spend=true` in the Ariadne config."
        )
    answer = input("Continue? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        raise AriadneError("cancelled before any billable call")


def _print_plan(question: str, runtime: Runtime, top_k: int,
                paper_spec: Path | None, max_roots: int, fulltext: bool,
                options: Mapping[str, Any]) -> None:
    needs_lkm, remote = _spec_needs_lkm(paper_spec)
    print("\nAriadne plan")
    print(f"  question:  {question}")
    print(f"  config:    {runtime.config.path}")
    print(f"  model:     {runtime.model} @ {runtime.model_base_url}")
    if needs_lkm:
        if remote:
            print(f"  retrieval: offline spec with {remote} LKM graph call(s)")
            calls = remote
        else:
            planner = "LLM" if options["use_query_planner"] else "deterministic"
            print(
                f"  retrieval: {planner} queries up to {options['max_queries']}, "
                f"{options['per_query']} results/query, "
                f"graph-probe up to {options['candidate_limit']} candidates, "
                f"select {top_k}"
            )
            calls = int(options["max_queries"]) + int(options["candidate_limit"]) * 2
            extraction_calls = (
                int(options["max_extractions"]) if options["use_lkm_extract"] else 0
            )
            cost = calls * 0.05 + extraction_calls * 1.0
            print(f"  billable:  up to ~{cost:.2f} CNY "
                  f"({calls} LKM calls + {extraction_calls} extractions)")
            if options.get("cross_check"):
                print(
                    "  cross-check: "
                    f"{options.get('cross_check_sources') or '(none)'}, "
                    f">= {options.get('min_corroboration', 1)} independent "
                    "source(s), "
                    + (
                        f"discover up to {options.get('external_candidates', 4)}"
                        if options.get("external_discovery") else "no discovery"
                    )
                )
    else:
        print(f"  retrieval: offline spec {paper_spec}")
    print(f"  stages:    retrieval -> paper2ir -> single -> merge -> render")
    print(f"  answers:   up to {max_roots} candidate roots")
    print(f"  fulltext:  {'on' if fulltext else 'off (graph-derived text)'}")
    print("  no files were written (--dry-run)")


def _reproducibility(runtime: Runtime) -> dict[str, Any]:
    """Record what produced this run, and what is deliberately not pinned."""
    return {
        "code": {"version": __version__, **git_state()},
        "model": {
            "model": runtime.model,
            "merge_model": runtime.merge_model,
            "base_url": runtime.model_base_url,
            "temperature": None,
            "temperature_note": (
                "The bundled steps do not pin a temperature, so two runs on the "
                "same commit are different samples, not reruns."
            ),
        },
        "python": python_version(runtime.python),
        "inputs": {},
        "prompt_versions": {},
    }


def _input_digests(papers: list[Mapping[str, Any]], run_dir: Path) -> dict[str, Any]:
    """Hash the exact graph and text each paper was processed from."""
    per_paper: dict[str, dict[str, str | None]] = {}
    for paper in papers:
        pid = str(paper.get("lkm_id") or "")
        paths = {}
        for key, field in (("graph_sha256", "graph_path"), ("text_sha256", "ocr_path")):
            value = paper.get(field)
            path = Path(str(value)) if value else None
            if path is not None and not path.is_absolute():
                path = run_dir / path
            paths[key] = file_digest(path) if path is not None else None
        per_paper[pid] = paths
    combined = hashlib.sha256(
        json.dumps(per_paper, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return {"papers": per_paper, "corpus_sha256": combined}


def run_full(
    question: str,
    *,
    out: Path | None = None,
    top_k: int = 8,
    paper_spec: Path | None = None,
    question_en: str | None = None,
    domain: str | None = None,
    max_roots: int = 5,
    runtime: Runtime | None = None,
    yes: bool = False,
    fulltext: bool = True,
    dry_run: bool = False,
    max_queries: int | None = None,
    candidate_limit: int | None = None,
    use_query_planner: bool | None = None,
    use_lkm_extract: bool | None = None,
    max_extractions: int | None = None,
    cross_check: bool | None = None,
    min_corroboration: int | None = None,
    external_discovery: bool | None = None,
) -> dict[str, Any]:
    if runtime is None:
        runtime = resolve_runtime(Config.load(), yes=yes)
    runtime.apply_env()
    question_en = question_en or question
    domain = domain or "the domain named by the question"
    retrieval_options = _retrieval_options(
        runtime, max_queries=max_queries, candidate_limit=candidate_limit,
        use_query_planner=use_query_planner, use_lkm_extract=use_lkm_extract,
        max_extractions=max_extractions, cross_check=cross_check,
        min_corroboration=min_corroboration,
        external_discovery=external_discovery,
    )

    details = _preflight(runtime, paper_spec)
    needs_lkm, remote = _spec_needs_lkm(paper_spec)
    if dry_run:
        _print_plan(question, runtime, top_k, paper_spec, max_roots, fulltext,
                    retrieval_options)
        return {
            "dry_run": True,
            "question": question,
            "runtime": runtime.summary(),
            "retrieval": retrieval_options,
            "preflight": details,
        }
    _confirm_spend(runtime, top_k, needs_lkm, remote, fulltext, retrieval_options)

    run_dir = new_run_dir(question, out)
    record: dict[str, Any] = {
        "product": "ariadne",
        "version": __version__,
        "mode": "full",
        "status": "running",
        "started_at": _utcnow(),
        "question": {"zh": question, "en": question_en, "domain": domain},
        "runtime": runtime.summary(),
        "retrieval": retrieval_options,
        "preflight": details,
        "reproducibility": _reproducibility(runtime),
        "stages": {},
        "outputs": {},
    }
    write_json(run_dir / "run.json", record)
    print(f"\nAriadne run: {run_dir}\n", flush=True)

    def stage(name: str, action: Callable[[], Any], **payload: Any) -> Any:
        started = time.monotonic()
        print(f"[{name}] start", flush=True)
        value = action()
        elapsed = round(time.monotonic() - started, 1)
        record["stages"][name] = {**payload, "seconds": elapsed}
        write_json(run_dir / "run.json", record)
        print(f"[{name}] done in {elapsed:.1f}s", flush=True)
        return value

    try:
        # 1. Retrieve papers and LKM graphs/text.
        papers = stage(
            "01_retrieval",
            lambda: retrieval.retrieve(
                question, run_dir, top_k=top_k, paper_spec=paper_spec,
                runtime=runtime, fulltext=fulltext,
                question_en=question_en, domain=domain,
                **retrieval_options,
            ),
            papers=0,
        )
        record["stages"]["01_retrieval"]["papers"] = len(papers)
        record["reproducibility"]["inputs"] = _input_digests(papers, run_dir)
        record["stages"]["01_retrieval"]["path"] = str(
            run_dir / "01_retrieval" / "papers.json"
        )
        print(f"[retrieval] {len(papers)} papers", flush=True)
        if len(papers) < 2:
            raise AriadneError(
                "Ariadne needs at least two papers; increase --top-k or add --papers"
            )

        # 2. paper2ir: LKM graph -> claims_final.
        claims = stage(
            "02_paper2ir",
            lambda: paper2ir.run_all(
                papers, run_dir / "02_paper2ir", run_dir,
                runtime=runtime, min_papers=int(retrieval_options["min_papers"]),
            ),
            papers=0,
        )
        record["stages"]["02_paper2ir"]["papers"] = len(claims)
        record["stages"]["02_paper2ir"]["path"] = str(
            run_dir / "02_paper2ir" / "papers.json"
        )
        print(f"[paper2ir] {len(claims)} papers complete", flush=True)

        # 3. single: per-paper Gaia IR.
        packages = stage(
            "03_single",
            lambda: single.run_all(claims, run_dir / "03_single", run_dir,
                                   runtime=runtime),
            packages=0,
        )
        record["stages"]["03_single"]["packages"] = len(packages)
        record["stages"]["03_single"]["path"] = str(
            run_dir / "03_single" / "packages.json"
        )
        print(f"[single] {len(packages)} packages complete", flush=True)

        # 4. merge + Step 6 answer roots/links.
        merged = stage(
            "04_merge",
            lambda: merge.run_question_pipeline(
                packages, run_dir / "04_merge",
                question=question, question_en=question_en, domain=domain,
                max_roots=max_roots, runtime=runtime,
            ),
            run_dir="",
        )
        answer, view_model = merge.extract_answer_layer(
            Path(merged["view_model"]), run_dir / "05_outputs"
        )
        layer = read_json(answer)
        generation = layer.get("generation") or {}
        record["reproducibility"]["prompt_versions"] = {
            "question_answer": generation.get("prompt_version"),
            "question_answer_model": generation.get("model"),
        }
        roots = len(layer.get("roots") or [])
        record["stages"]["04_merge"]["run_dir"] = merged["run_dir"]
        record["stages"]["04_merge"]["roots"] = roots
        print(f"[merge] {roots} answer roots", flush=True)

        # 5. Deliverables.
        rendered = stage(
            "05_render",
            lambda: render_outputs(view_model, answer, run_dir / "05_outputs",
                                   log_dir=run_dir),
            outputs=[],
        )
        installed = install_outputs(rendered, run_dir)
        record["outputs"] = {
            "viewer": installed["html"],
            "chains": installed["markdown"],
            "answer_layer": str(answer),
            "view_model": str(view_model),
        }
        record["stages"]["05_render"]["outputs"] = list(record["outputs"])
        record["status"] = "complete"
        record["finished_at"] = _utcnow()
        write_json(run_dir / "run.json", record)
    except KeyboardInterrupt:
        # Ctrl-C is not an Exception, so without this a cancelled run stays
        # "running" forever and no later reader can tell it apart from a live one.
        record["status"] = "interrupted"
        record["finished_at"] = _utcnow()
        write_json(run_dir / "run.json", record)
        print(f"\nAriadne interrupted; state saved in {run_dir / 'run.json'}", flush=True)
        raise
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = str(exc)[:4000]
        record["finished_at"] = _utcnow()
        write_json(run_dir / "run.json", record)
        print(f"\nAriadne failed; state saved in {run_dir / 'run.json'}", flush=True)
        raise

    print(
        f"\nAriadne complete\n  viewer:   {installed['html']}\n"
        f"  chains:   {installed['markdown']}",
        flush=True,
    )
    return {
        "run_dir": str(run_dir),
        "viewer": installed["html"],
        "chains": installed["markdown"],
        "answer_layer": str(answer),
        "view_model": str(view_model),
    }
