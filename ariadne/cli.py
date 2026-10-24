"""Ariadne command line: config, auth, doctor, demo, ask, version."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    AGENT_ENV,
    Config,
    Runtime,
    bohr_status,
    bohr_version,
    file_mode,
    mask_secret,
    mode_is_private,
    python_version,
    resolve_runtime,
)
from .demo import run_demo
from .full import REQUIRED_FILES, run_full
from .sources import DEFAULT_SOURCE_LIST
from .util import AriadneError, DEFAULT_CONFIG, REPO, VENV_PY, read_json, run, run_interactive


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #

def _marker(ok: bool, *, missing: str = "--") -> str:
    return "OK" if ok else missing


def _print_kv(label: str, value: Any, *, ok: bool | None = None) -> None:
    prefix = f"  [{_marker(bool(ok)):7}] " if ok is not None else "  "
    print(f"{prefix}{label}: {value}")


def _redacted(value: str) -> str:
    return mask_secret(value)


def _config_summary(runtime: Runtime) -> dict[str, Any]:
    return {
        "path": str(runtime.config.path),
        "exists": runtime.config.exists,
        "private": mode_is_private(runtime.config.path) if runtime.config.exists else None,
        "mode": (
            oct(file_mode(runtime.config.path) or 0)
            if runtime.config.exists else None
        ),
        "runtime": runtime.summary(),
        "retrieval": _retrieval_config(runtime.config),
    }


def _retrieval_config(config: Config) -> dict[str, Any]:
    return {
        "use_query_planner": bool(config.get("retrieval.use_query_planner", True)),
        "queries": int(config.get("retrieval.queries", 5)),
        "per_query": int(config.get("retrieval.per_query", 8)),
        "candidate_limit": int(config.get("retrieval.candidate_limit", 16)),
        "min_papers": int(config.get("retrieval.min_papers", 2)),
        "min_graph_nodes": int(config.get("retrieval.min_graph_nodes", 3)),
        "min_relevance": float(config.get("retrieval.min_relevance", 0.2)),
        "use_lkm_extract": bool(config.get("retrieval.use_lkm_extract", True)),
        "max_extractions": int(config.get("retrieval.max_extractions", 3)),
        "cross_check": bool(config.get("retrieval.cross_check", True)),
        "cross_check_sources": str(
            config.get(
                "retrieval.cross_check_sources",
                DEFAULT_SOURCE_LIST,
            )
        ),
        "min_corroboration": int(config.get("retrieval.min_corroboration", 1)),
        "external_discovery": bool(
            config.get("retrieval.external_discovery", False)
        ),
        "external_candidates": int(
            config.get("retrieval.external_candidates", 4)
        ),
    }


def _print_config_summary(runtime: Runtime) -> None:
    summary = runtime.summary()
    config = summary["config"]
    model = summary["model"]
    bohr = summary["bohr"]
    print("Ariadne configuration\n")
    _print_kv("file", config["path"], ok=config["exists"])
    if config["exists"]:
        private = mode_is_private(runtime.config.path)
        _print_kv("permissions", oct(file_mode(runtime.config.path) or 0),
                  ok=private)
    print("\nModel")
    _print_kv("provider", model["provider"])
    _print_kv("base_url", model["base_url"])
    _print_kv("model", model["model"])
    _print_kv("merge_model", model["merge_model"])
    _print_kv(
        "api_key",
        _redacted(runtime.model_api_key) if runtime.model_key_present else "missing",
        ok=runtime.model_key_present,
    )
    _print_kv("key source", model["api_key_source"])
    print("\nBohr")
    _print_kv("cli", bohr["cli"] or "not found", ok=bool(bohr["cli"]))
    _print_kv("profile", bohr["profile"] or "(active)")
    _print_kv("host", bohr["host"])
    _print_kv(
        "access_key",
        "<configured>" if bohr["access_key_present"] else "not in Ariadne config",
        ok=bohr["access_key_present"],
    )
    _print_kv("auto_approve_spend", bohr["auto_approve_spend"])
    _print_kv("payment_source", bohr["payment_source"] or "(default)")
    print("\nRuntime")
    _print_kv("python", runtime.python)
    _print_kv("config precedence", "CLI > env > config file > agent/.env")
    retrieval = _retrieval_config(runtime.config)
    print("\nRetrieval")
    _print_kv("query planner", "on" if retrieval["use_query_planner"] else "off")
    _print_kv("queries / per_query",
              f"{retrieval['queries']} / {retrieval['per_query']}")
    _print_kv("graph probes (first pass)", retrieval["candidate_limit"])
    _print_kv("min papers / min nodes",
              f"{retrieval['min_papers']} / {retrieval['min_graph_nodes']}")
    _print_kv("min relevance", retrieval["min_relevance"])
    _print_kv("LKM extract fallback",
              f"on (max {retrieval['max_extractions']})"
              if retrieval["use_lkm_extract"] else "off")
    _print_kv(
        "independent cross-check",
        f"on ({retrieval['cross_check_sources']}; min "
        f"{retrieval['min_corroboration']} source(s))"
        if retrieval["cross_check"] else "off",
    )
    _print_kv(
        "external discovery",
        f"on (max {retrieval['external_candidates']})"
        if retrieval["external_discovery"] else "off",
    )


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #

def _doctor(runtime: Runtime) -> dict[str, Any]:
    demo = read_json(DEFAULT_CONFIG)
    demo_files = {
        "view": REPO / demo["view"],
        "edge_roles": REPO / demo["edge_roles"],
        "fixture": REPO / demo["fixture"],
    }
    pipeline_files = {rel: REPO / rel for rel in sorted(REQUIRED_FILES)}
    python_ok = Path(runtime.python).is_file()
    python_ver = python_version(runtime.python) if python_ok else ""
    demo_ready = all(path.is_file() for path in demo_files.values())
    pipeline_ready = all(path.is_file() for path in pipeline_files.values())
    model_ready = runtime.model_key_present
    bohr_cli = bool(runtime.bohr_bin)
    status = bohr_status(runtime) if bohr_cli else {
        "available": False, "logged_in": False, "error": "bohr CLI not installed",
    }
    lkm_ready = bohr_cli and bool(status.get("logged_in"))
    offline_ready = pipeline_ready and model_ready and python_ok
    config_private = (
        mode_is_private(runtime.config.path) if runtime.config.exists else None
    )
    return {
        "version": __version__,
        "repo": str(REPO),
        "config": {
            "path": str(runtime.config.path),
            "exists": runtime.config.exists,
            "private": config_private,
            "mode": (
                oct(file_mode(runtime.config.path) or 0)
                if runtime.config.exists else None
            ),
        },
        "python": {
            "path": runtime.python,
            "exists": python_ok,
            "version": python_ver,
            "source": runtime.sources.get("python", "default"),
        },
        "model": {
            "provider": runtime.config.get("model.provider", "deepseek"),
            "base_url": runtime.model_base_url,
            "model": runtime.model,
            "merge_model": runtime.merge_model,
            "api_key_present": model_ready,
            "api_key_source": runtime.sources.get("model_api_key", "missing"),
            "anthropic_key_present": runtime.anthropic_key_present,
        },
        "bohr": {
            "cli": runtime.bohr_bin,
            "version": bohr_version(runtime) if bohr_cli else "",
            "profile": runtime.bohr_profile,
            "access_key_present": bool(runtime.bohr_access_key),
            "access_key_source": runtime.sources.get("bohr_access_key", "missing"),
            "status": status,
        },
        "demo": {
            "ready": demo_ready,
            "files": {name: path.is_file() for name, path in demo_files.items()},
        },
        "retrieval": _retrieval_config(runtime.config),
        "pipelines": {
            "ready": pipeline_ready,
            "files": {rel: path.is_file() for rel, path in pipeline_files.items()},
            "missing": [rel for rel, path in pipeline_files.items() if not path.is_file()],
        },
        "readiness": {
            "demo": demo_ready,
            "full_with_papers": offline_ready,
            "full_with_lkm": offline_ready and lkm_ready,
        },
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    runtime = resolve_runtime(config, agent_env_path=AGENT_ENV)
    checks = _doctor(runtime)
    if args.json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return 0

    print(f"Ariadne doctor {__version__}\n")
    print("Config")
    _print_kv("file", checks["config"]["path"], ok=checks["config"]["exists"])
    if checks["config"]["exists"]:
        _print_kv("permissions", checks["config"]["mode"],
                  ok=bool(checks["config"]["private"]))
    else:
        print("        (optional for demo; run `ariadne config init` for full mode)")
    print("\nEnvironment")
    _print_kv("repo", checks["repo"], ok=True)
    _print_kv("python", f"{checks['python']['path']}"
              + (f" ({checks['python']['version']})" if checks["python"]["version"] else ""),
              ok=checks["python"]["exists"])
    print("\nModel")
    _print_kv("endpoint", f"{checks['model']['model']} @ {checks['model']['base_url']}")
    _print_kv("api key",
              (f"{_redacted(runtime.model_api_key)} "
               f"(source: {checks['model']['api_key_source']})")
              if checks["model"]["api_key_present"] else "missing",
              ok=checks["model"]["api_key_present"])
    if checks["model"]["anthropic_key_present"] and not checks["model"]["api_key_present"]:
        print("        ANTHROPIC_API_KEY alone is not supported by the bundled stages.")
    retrieval = checks["retrieval"]
    print("\nRetrieval")
    _print_kv("query planner", "on" if retrieval["use_query_planner"] else "off",
              ok=True)
    _print_kv("queries / per_query",
              f"{retrieval['queries']} / {retrieval['per_query']}")
    _print_kv("first-pass graph probes", retrieval["candidate_limit"])
    _print_kv("min papers / min nodes",
              f"{retrieval['min_papers']} / {retrieval['min_graph_nodes']}")
    _print_kv("min relevance", retrieval["min_relevance"])
    _print_kv("LKM extract fallback",
              f"on (max {retrieval['max_extractions']})"
              if retrieval["use_lkm_extract"] else "off")
    _print_kv(
        "independent cross-check",
        f"on ({retrieval['cross_check_sources']}; min "
        f"{retrieval['min_corroboration']} source(s))"
        if retrieval["cross_check"] else "off",
        ok=bool(retrieval["cross_check"]),
    )
    _print_kv(
        "external discovery",
        f"on (max {retrieval['external_candidates']})"
        if retrieval["external_discovery"] else "off",
    )
    print("\nBohr")
    _print_kv("cli", checks["bohr"]["cli"] or "not found", ok=bool(checks["bohr"]["cli"]))
    if checks["bohr"]["version"]:
        _print_kv("version", checks["bohr"]["version"], ok=True)
    status = checks["bohr"]["status"]
    _print_kv(
        "auth",
        f"profile={status.get('profile') or '(active)'} "
        f"logged_in={status.get('logged_in')}",
        ok=bool(status.get("logged_in")),
    )
    if not status.get("logged_in") and status.get("error"):
        print(f"        {status['error'][:160]}")
    print("\nPipelines")
    for rel, present in checks["pipelines"]["files"].items():
        _print_kv(rel, present, ok=present)
    print("\nDemo")
    for name, present in checks["demo"]["files"].items():
        _print_kv(name, present, ok=present)
    readiness = checks["readiness"]
    offline_missing = []
    if not checks["python"]["exists"]:
        offline_missing.append("Python")
    if not checks["model"]["api_key_present"]:
        offline_missing.append("model key")
    if not checks["pipelines"]["ready"]:
        offline_missing.append("pipeline files")
    lkm_missing = list(offline_missing)
    if not status.get("logged_in"):
        lkm_missing.append("Bohr login")
    print("\nReadiness")
    _print_kv("demo (offline)", "ready" if readiness["demo"] else "missing files",
              ok=readiness["demo"])
    _print_kv("full with --papers",
              "ready" if readiness["full_with_papers"] else "needs " + ", ".join(offline_missing),
              ok=readiness["full_with_papers"])
    _print_kv("full with live LKM",
              "ready" if readiness["full_with_lkm"] else "needs " + ", ".join(lkm_missing),
              ok=readiness["full_with_lkm"])
    print("\nNext")
    if not checks["config"]["exists"]:
        print("  ./run.sh config init")
    if checks["bohr"]["cli"] and not status.get("logged_in"):
        print("  ./run.sh auth bohr")
    print("  ./run.sh demo")
    print("  ./run.sh ask \"your question\" --top-k 2 --yes")
    return 0


# --------------------------------------------------------------------------- #
# demo / ask
# --------------------------------------------------------------------------- #

def cmd_demo(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    runtime = resolve_runtime(config, agent_env_path=AGENT_ENV)
    runtime.apply_env()
    result = run_demo(Path(args.out) if args.out else None, runtime=runtime)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_support(args: argparse.Namespace) -> int:
    from .support import build_candidate, run_support

    runtime = resolve_runtime(Config.load(args.config), agent_env_path=AGENT_ENV)
    candidate = build_candidate(
        args.candidate, code=args.code, title_zh=args.title_zh,
        title_en=args.title_en, scope=args.scope,
    )
    question = {"zh": args.question, "en": args.question} if args.question else None
    expand_with = None
    if args.expand:
        base = Path(args.runs_dir) if args.runs_dir else (
            Path(args.out) if args.out else Path("outputs/support_runs")
        )
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        expand_with = {
            "run_dir": base / f"expand_{stamp}",
            "max_papers": args.expand,
            "query": args.query,
            "yes": args.yes,
        }
    report = run_support(
        Path(args.view), candidate, question=question,
        out=Path(args.out) if args.out else None, runtime=runtime,
        expand_with=expand_with,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        links = report["links"]
        print(f"\ncandidate {candidate['code']}: {report['status']}")
        print(f"  support / oppose / bounds: {len(links['support'])} / "
              f"{len(links['oppose'])} / {len(links['bounds'])}")
        print(f"  verdict: {report['verdict']['class']} — {report['verdict']['label']}")
        scope = report["scope"]
        print(f"  scope: {scope['node_count']} nodes, {scope['edge_count']} edges, "
              f"{scope['claims_offered_to_judge']} claims judged")
        retrieval = scope.get("retrieval")
        if scope.get("searched") and isinstance(retrieval, dict):
            print(f"  searched LKM: yes — query {retrieval['query']!r}, "
                  f"{retrieval['papers_used']}/{retrieval['papers_requested']} papers, "
                  f"+{retrieval['nodes_added']} nodes, "
                  f"~{retrieval['estimated_cost_cny']} CNY")
        else:
            print("  searched LKM: no — absence here is not absence in the literature")
        if report["next"]["retrieval_recommended"]:
            print(f"  next: {report['next']['how']}")
        if args.out:
            print(f"\n  report: {Path(args.out) / 'support.md'}")
    return 0


def cmd_controls(args: argparse.Namespace) -> int:
    from . import controls as controls_module

    view = read_json(Path(args.view))
    answer_layer = read_json(Path(args.answer_layer))
    roots = controls_module.candidate_roots(answer_layer)
    if args.preview:
        physics = read_json(Path(args.physics_view)) if args.physics_view else None
        print(controls_module.preview(view, roots, physics_view=physics))
        return 0

    from .support import native_imports

    question_layer, _ = native_imports()
    runtime = resolve_runtime(Config.load(args.config), agent_env_path=AGENT_ENV)
    runtime.apply_env()
    arms = {item.strip() for item in str(args.arms).split(",") if item.strip()}
    baseline = len(roots) if arms & {"reproduce", "negation", "controls", "representation"} else 0
    extra = 0
    if "representation" in arms:
        extra += len(roots)
    if "negation" in arms:
        extra += 2 * len(roots)
    if "controls" in arms:
        extra += len(roots)
    if "stability" in arms:
        extra += int(args.repeat)
    print(f"controls: arms {', '.join(sorted(arms))} | {baseline + extra} model calls "
          f"at temperature 0", flush=True)
    report = controls_module.run(
        Path(args.view), Path(args.answer_layer),
        call_json=question_layer._post_json,
        out=Path(args.out) if args.out else None,
        physics_view_path=Path(args.physics_view) if args.physics_view else None,
        arms=arms,
        repeat=int(args.repeat),
        probe=args.probe,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(f"  reproduction (same pool): mean link Jaccard "
              f"{summary['reproduce_jaccard_mean']}")
        print(f"  representation: {summary['representation_changed_or_dropped']} of "
              f"{summary['representation_before']} links changed or disappeared")
        print(f"  negation: {summary['negations_with_surviving_support']} of "
              f"{summary['negations_total']} still received support")
        print(f"  controls: {summary['control_links_total']} links to "
              f"{summary['controls_injected_per_root']} irrelevant claims "
              f"(FP rate {summary['control_false_positive_rate']})")
        if args.out:
            print(f"\n  report: {Path(args.out) / 'controls.md'}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    overrides = {
        "model_api_key": args.api_key,
        "model_base_url": args.base_url,
        "model": args.model,
        "merge_model": args.merge_model,
        "bohr_profile": args.bohr_profile,
        "bohr_access_key": args.bohr_access_key,
        "payment_source": args.payment_source,
        "python": args.python,
    }
    runtime = resolve_runtime(
        config, overrides=overrides, yes=args.yes, agent_env_path=AGENT_ENV
    )
    runtime.apply_env()
    if args.mode == "demo":
        if args.library or args.documents:
            raise AriadneError("--library and --documents require --mode full")
        demo = read_json(DEFAULT_CONFIG)
        if args.question.strip() != demo["question_zh"]:
            print("demo uses the frozen aircraft graph; the configured question is: "
                  f"{demo['question_zh']}", file=sys.stderr)
            print("Use --mode full for a new question.", file=sys.stderr)
        result = run_demo(Path(args.out) if args.out else None, runtime=runtime)
    else:
        library_options = {}
        runner = run_full
        if args.library or args.documents:
            from .library import run_library
            runner = run_library
            library_options = {
                "library": Path(args.library).expanduser() if args.library else None,
                "documents": [Path(p).expanduser() for p in (args.documents or [])],
            }
        result = runner(
            args.question,
            out=Path(args.out) if args.out else None,
            top_k=args.top_k,
            paper_spec=Path(args.papers) if args.papers else None,
            question_en=args.question_en,
            domain=args.domain,
            max_roots=args.max_roots,
            runtime=runtime,
            yes=args.yes,
            fulltext=not args.no_fulltext,
            dry_run=args.dry_run,
            max_queries=args.queries,
            candidate_limit=args.candidate_limit,
            use_query_planner=args.use_query_planner,
            use_lkm_extract=args.use_lkm_extract,
            max_extractions=args.max_extractions,
            cross_check=args.cross_check,
            min_corroboration=args.min_corroboration,
            external_discovery=args.external_discovery,
            **library_options,
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def _prompt_text(label: str, current: str, *, secret: bool = False) -> str:
    shown = _redacted(current) if secret and current else current
    suffix = f" [{shown}]" if shown else " [leave blank to keep]"
    prompt = f"{label}{suffix}: "
    value = getpass.getpass(prompt) if secret else input(prompt)
    value = value.strip()
    return value or current


def _apply_config_flags(config: Config, args: argparse.Namespace) -> None:
    mapping = {
        "model.api_key": getattr(args, "api_key", None),
        "model.base_url": getattr(args, "base_url", None),
        "model.model": getattr(args, "model", None),
        "model.merge_model": getattr(args, "merge_model", None),
        "bohr.access_key": getattr(args, "bohr_access_key", None),
        "bohr.profile": getattr(args, "bohr_profile", None),
        "runtime.python": getattr(args, "python", None),
        "bohr.payment_source": getattr(args, "payment_source", None),
    }
    for key, value in mapping.items():
        if value not in (None, ""):
            config.set(key, value)
    auto = getattr(args, "auto_approve_spend", None)
    if auto is not None:
        config.set("bohr.auto_approve_spend", auto)


def cmd_config_init(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    interactive = not args.no_input and sys.stdin.isatty()

    if config.exists and not args.force and interactive:
        answer = input(f"{config.path} exists; update it? [Y/n] ").strip().lower()
        if answer in {"n", "no"}:
            print("unchanged")
            return 0

    _apply_config_flags(config, args)

    if interactive:
        print(f"Configuring Ariadne ({config.path})")
        print("Press Enter to keep the current value.\n")
        api_key = _prompt_text(
            "Model API key (DeepSeek/OpenAI-compatible)",
            config.get("model.api_key"), secret=True,
        )
        if api_key:
            config.set("model.api_key", api_key)
        base_url = _prompt_text(
            "Model base URL", config.get("model.base_url")
        )
        if base_url != config.get("model.base_url"):
            config.set("model.base_url", base_url)
        model = _prompt_text("Model name", config.get("model.model"))
        if model != config.get("model.model"):
            config.set("model.model", model)
        merge_model = _prompt_text(
            "Merge/question model", config.get("model.merge_model")
        )
        if merge_model != config.get("model.merge_model"):
            config.set("model.merge_model", merge_model)
        print("\nBohrium LKM (optional for demo; required for live retrieval)")
        access_key = _prompt_text(
            "Bohr access key", config.get("bohr.access_key"), secret=True
        )
        if access_key:
            config.set("bohr.access_key", access_key)
        profile_default = config.get("bohr.profile") or (
            "ariadne" if config.get("bohr.access_key") else ""
        )
        profile = _prompt_text("Bohr profile", profile_default)
        config.set("bohr.profile", profile)
        python = _prompt_text(
            "Python for pipeline subprocesses",
            config.get("runtime.python") or runtime_python_default(),
        )
        if python and python != runtime_python_default():
            config.set("runtime.python", python)
        approve = input(
            "Auto-approve billable LKM calls (--yes) for this config? [y/N] "
        ).strip().lower()
        config.set("bohr.auto_approve_spend", approve in {"y", "yes"})

    if not interactive and not args.no_input:
        print("stdin is not a TTY; writing values from flags only", file=sys.stderr)

    if not config.exists and not any(
        config.get(key) for key in ("model.api_key", "bohr.access_key")
    ) and not _has_flag_values(args):
        raise AriadneError(
            "nothing to configure; pass --api-key (and optionally "
            "--bohr-access-key), or run interactively in a terminal"
        )

    path = config.save()
    print(f"\nWrote {path} (mode 600)")
    runtime = resolve_runtime(config, agent_env_path=AGENT_ENV)
    _print_config_summary(runtime)

    if args.login_bohr:
        status = _auth_bohr(
            runtime,
            profile=args.bohr_profile or config.get("bohr.profile"),
            access_key=args.bohr_access_key or config.get("bohr.access_key"),
            device=False,
            no_input=args.no_input,
        )
        print(f"\nBohr login: {'OK' if status.get('logged_in') else 'FAILED'} "
              f"(profile={status.get('profile') or '?'})")
    else:
        print("\nNext: ./run.sh doctor")
        print("      ./run.sh auth bohr   # live LKM")
        print("      ./run.sh demo")
    return 0


def _has_flag_values(args: argparse.Namespace) -> bool:
    return any(
        getattr(args, name, None) not in (None, "")
        for name in (
            "api_key", "base_url", "model", "merge_model",
            "bohr_access_key", "bohr_profile", "python", "payment_source",
            "auto_approve_spend",
        )
    )


def runtime_python_default() -> str:
    return str(VENV_PY) if VENV_PY.is_file() else sys.executable


def cmd_config_set(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    value = config.set(args.key, args.value)
    path = config.save()
    shown = _redacted(value) if args.key in {"model.api_key", "bohr.access_key"} else value
    print(f"{args.key} = {shown}\nwrote {path}")
    return 0


def cmd_config_unset(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    config.unset(args.key)
    path = config.save()
    print(f"{args.key} reset to default\nwrote {path}")
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    value = config.get(args.key)
    if args.key in {"model.api_key", "bohr.access_key"}:
        value = _redacted(str(value)) if value else ""
    if args.json:
        print(json.dumps({"key": args.key, "value": value}, ensure_ascii=False))
    else:
        print(value)
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    runtime = resolve_runtime(config, agent_env_path=AGENT_ENV)
    if args.json:
        print(json.dumps(_config_summary(runtime), ensure_ascii=False, indent=2))
    else:
        _print_config_summary(runtime)
    return 0


def cmd_config_path(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    print(config.path)
    return 0


def _test_model(runtime: Runtime) -> dict[str, Any]:
    if not runtime.model_key_present:
        raise AriadneError("no model API key configured")
    url = runtime.model_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": runtime.model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 2,
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {runtime.model_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
        parsed = json.loads(body)
        choice = ((parsed.get("choices") or [{}])[0].get("message") or {}).get("content")
        return {
            "ok": True, "http": 200, "model": runtime.model,
            "endpoint": url, "reply": str(choice or "")[:40],
        }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return {"ok": False, "http": exc.code, "endpoint": url, "error": detail}
    except Exception as exc:  # noqa: BLE001 - report, do not leak the key
        return {"ok": False, "http": 0, "endpoint": url, "error": str(exc)[:500]}


def cmd_config_test(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    runtime = resolve_runtime(config, agent_env_path=AGENT_ENV)
    test_api = args.api or not (args.api or args.bohr)
    test_bohr = args.bohr or not (args.api or args.bohr)
    result: dict[str, Any] = {}
    if test_api:
        result["model"] = _test_model(runtime)
        if args.json is False:
            print(f"model: {'OK' if result['model']['ok'] else 'FAILED'} "
                  f"({result['model'].get('http')}) {result['model'].get('endpoint')}")
            if not result["model"]["ok"]:
                print(f"  {result['model'].get('error', '')[:300]}")
    if test_bohr:
        status = bohr_status(runtime)
        result["bohr"] = status
        if args.json is False:
            print(f"bohr: {'OK' if status.get('logged_in') else 'FAILED'} "
                  f"profile={status.get('profile') or '(active)'}")
            if not status.get("logged_in"):
                print(f"  {status.get('error', '')[:300]}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(
        item.get("ok", item.get("logged_in", False))
        for item in result.values()
    ) else 1


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #

def _auth_bohr(
    runtime: Runtime,
    *,
    profile: str | None,
    access_key: str | None,
    device: bool,
    no_input: bool,
) -> dict[str, Any]:
    if not runtime.bohr_bin:
        raise AriadneError(
            "bohr CLI not found. Install it with `npm install -g bohr-cli` "
            "and retry, or use --papers with local files."
        )
    selected = profile or runtime.bohr_profile or "ariadne"
    key = access_key or runtime.bohr_access_key
    if key:
        print(f"Logging in to Bohr profile {selected!r} with an access key...")
        command = [
            runtime.bohr_bin, "auth", "login", "--ak", key, "--ak-only",
            "--profile", selected, "--no-interactive", "-o", "json",
        ]
        proc = run(command, check=False, timeout=120)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-600:]
            raise AriadneError(
                f"bohr auth login failed for profile {selected!r}. "
                "Check the access key, or run `bohr auth login` manually.\n" + tail
            )
        runtime.config.set("bohr.access_key", key)
        runtime.config.set("bohr.profile", selected)
        runtime.config.save()
    else:
        if no_input or not sys.stdin.isatty():
            raise AriadneError(
                "no access key configured; pass --access-key or run "
                "`ariadne auth bohr --device` from an interactive terminal"
            )
        command = [runtime.bohr_bin, "auth", "login", "--profile", selected]
        if device:
            command.append("--device")
        print("Follow the Bohr login instructions (browser or device code)...")
        code = run_interactive(command)
        if code != 0:
            raise AriadneError(f"bohr auth login exited with {code}")
        runtime.config.set("bohr.profile", selected)
        runtime.config.save()
    return bohr_status(runtime, profile=selected)


def cmd_auth_bohr(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    runtime = resolve_runtime(config, agent_env_path=AGENT_ENV)
    if args.check:
        status = bohr_status(runtime, profile=args.profile)
    else:
        status = _auth_bohr(
            runtime,
            profile=args.profile,
            access_key=args.access_key,
            device=args.device,
            no_input=args.no_input,
        )
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(f"bohr: {'logged in' if status.get('logged_in') else 'not logged in'}")
        print(f"  profile: {status.get('profile') or '(active)'}")
        print(f"  auth method: {status.get('auth_method') or '-'}")
        print(f"  credential: {status.get('credential_source') or '-'}")
        if status.get("error") and not status.get("logged_in"):
            print(f"  error: {status['error'][:300]}")
    return 0 if status.get("logged_in") else 1


def cmd_auth_status(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    runtime = resolve_runtime(config, agent_env_path=AGENT_ENV)
    status = bohr_status(runtime)
    payload = {
        "bohr": status,
        "model": {
            "api_key_present": runtime.model_key_present,
            "api_key_source": runtime.sources.get("model_api_key", "missing"),
            "base_url": runtime.model_base_url,
            "model": runtime.model,
        },
        "config": {
            "path": str(runtime.config.path),
            "exists": runtime.config.exists,
        },
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_config_summary(runtime)
        print("\nBohr status")
        print(f"  logged_in: {status.get('logged_in')}")
        print(f"  profile: {status.get('profile') or '(active)'}")
        print(f"  auth_method: {status.get('auth_method') or '-'}")
        print(f"  credential_source: {status.get('credential_source') or '-'}")
        if status.get("error"):
            print(f"  error: {status['error'][:300]}")
    return 0 if status.get("logged_in") else 1


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def _add_config_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", help="Ariadne config file (default: ~/.config/ariadne/config.json)",
    )


def _add_model_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-key", help="override model API key for this run")
    parser.add_argument("--base-url", help="override OpenAI-compatible base URL")
    parser.add_argument("--model", help="override model name")
    parser.add_argument("--merge-model", help="override merge/question model name")
    parser.add_argument("--bohr-profile", help="override Bohr CLI profile")
    parser.add_argument("--bohr-access-key", help="override Bohr access key for this run")
    parser.add_argument(
        "--payment-source", choices=["photon", "balance"],
        help="Bohr payment channel for billable LKM calls",
    )
    parser.add_argument("--python", help="override Python for pipeline subprocesses")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ariadne",
        description="Ariadne — question-led domain reasoning graphs.",
    )
    parser.add_argument("--version", action="version", version=f"ariadne {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="ask a question end-to-end (or run the frozen demo)")
    ask.add_argument("question", help="domain question, in Chinese or English")
    ask.add_argument("--mode", choices=["full", "demo"], default="full",
                     help="full: LKM -> paper2ir -> single -> merge; demo: frozen aircraft graph")
    ask.add_argument("--out", help="output run directory")
    ask.add_argument("--top-k", type=int, default=8,
                     help="number of papers to select; not answer roots (default 8)")
    ask.add_argument("--papers", help="offline paper spec (JSON) instead of LKM search")
    ask.add_argument("--library", help="personal knowledge library directory; reuse evidence before LKM search")
    ask.add_argument("--documents", nargs="+", metavar="PATH", action="extend",
                     help="selected PDF/Markdown files to upload to LKM (Markdown is converted to PDF)")
    ask.add_argument("--question-en", help="English question for the answer layer")
    ask.add_argument("--domain", help="domain label for root generation")
    ask.add_argument("--max-roots", type=int, default=5, help="candidate answers")
    ask.add_argument("--yes", action="store_true",
                     help="approve billable Bohr LKM calls for this run")
    ask.add_argument("--no-fulltext", action="store_true",
                     help="skip PDF parsing and use graph-derived text")
    ask.add_argument("--dry-run", action="store_true",
                     help="show the plan and preflight, write nothing")
    ask.add_argument("--queries", type=int,
                     help="max LKM queries for automatic retrieval (default: config)")
    ask.add_argument("--candidate-limit", type=int,
                     help="max candidate graphs to probe before selection")
    ask.add_argument("--no-query-planner", dest="use_query_planner",
                     action="store_false", default=None,
                     help="disable the LLM query planner; use deterministic queries")
    ask.add_argument("--no-lkm-extract", dest="use_lkm_extract",
                     action="store_false", default=None,
                     help="do not build graphs with bohr lkm extract")
    ask.add_argument("--max-extractions", type=int,
                     help="max papers to graph via bohr lkm extract")
    ask.add_argument("--no-cross-check", dest="cross_check",
                     action="store_false", default=None,
                     help="skip verification against independent literature sources")
    ask.add_argument("--min-corroboration", type=int,
                     help="independent sources needed to prefer a paper (default: config)")
    ask.add_argument("--external-discovery", dest="external_discovery",
                     action="store_true", default=None,
                     help="also consider papers found by independent sources but missed by LKM")
    _add_model_overrides(ask)
    _add_config_flag(ask)
    ask.add_argument("--json", action="store_true")
    ask.set_defaults(func=cmd_ask)

    demo = sub.add_parser("demo", help="run the frozen aircraft example offline")
    demo.add_argument("--out", help="output run directory")
    _add_config_flag(demo)
    demo.add_argument("--json", action="store_true")
    demo.set_defaults(func=cmd_demo)

    support = sub.add_parser(
        "support",
        help="link one new candidate to an existing graph, without retrieving",
    )
    support.add_argument("--view", required=True, help="frozen view model to judge against")
    support.add_argument("--candidate", required=True, help="the candidate statement")
    support.add_argument("--code", default="C1", help="short id for the candidate (default C1)")
    support.add_argument("--title-zh", default="", help="optional Chinese title")
    support.add_argument("--title-en", default="", help="optional English title")
    support.add_argument("--scope", default="", help="when the candidate is meant to apply")
    support.add_argument("--question", default="", help="question the candidate answers")
    support.add_argument(
        "--expand", nargs="?", type=int, const=4, default=None, metavar="N",
        help="also search LKM for related papers (default 4) and judge their graph nodes; "
             "the overlay applies to this judgment only and is not written into the view",
    )
    support.add_argument("--query", default=None, help="override the expansion query")
    support.add_argument("--yes", action="store_true", help="approve billable LKM calls for --expand")
    support.add_argument("--runs-dir", default=None, help="where expansion logs and graphs go")
    support.add_argument("--out", help="write support.json and support.md here")
    _add_config_flag(support)
    support.add_argument("--json", action="store_true")
    support.set_defaults(func=cmd_support)

    controls = sub.add_parser(
        "controls",
        help="run the link-judge controls (reproduce, representation, negation, negatives)",
    )
    controls.add_argument("--view", required=True, help="view model the links were judged on")
    controls.add_argument("--answer-layer", required=True, help="answer layer with stored links")
    controls.add_argument("--physics-view", help="second view used for cross-domain negatives")
    controls.add_argument("--arms", default="reproduce,negation",
                          help="comma list of reproduce,representation,negation,controls "
                               "(default reproduce,negation)")
    controls.add_argument("--probe", help="negation code the stability arm repeats, e.g. R5-N1")
    controls.add_argument("--repeat", type=int, default=5,
                          help="how many times the stability arm repeats one prompt")
    controls.add_argument("--preview", action="store_true",
                          help="print the constructions only; no model call")
    controls.add_argument("--out", help="write controls.json and controls.md here")
    _add_config_flag(controls)
    controls.add_argument("--json", action="store_true")
    controls.set_defaults(func=cmd_controls)

    doctor = sub.add_parser("doctor", help="check local prerequisites and configuration")
    _add_config_flag(doctor)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    cfg = sub.add_parser("config", help="inspect or edit the Ariadne configuration")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)

    init = cfg_sub.add_parser("init", help="create or update the config file")
    init.add_argument("--api-key", help="model API key")
    init.add_argument("--base-url", help="OpenAI-compatible base URL")
    init.add_argument("--model", help="model name")
    init.add_argument("--merge-model", help="merge/question model name")
    init.add_argument("--bohr-access-key", help="Bohr access key")
    init.add_argument("--bohr-profile", help="Bohr CLI profile")
    init.add_argument("--python", help="Python for pipeline subprocesses")
    init.add_argument("--payment-source", choices=["photon", "balance"])
    init.add_argument("--auto-approve-spend", action=argparse.BooleanOptionalAction,
                      default=None, help="approve billable LKM calls by default")
    init.add_argument("--no-input", action="store_true",
                      help="do not prompt; use flags only")
    init.add_argument("--force", action="store_true",
                      help="update an existing file without asking")
    init.add_argument("--login-bohr", action="store_true",
                      help="log in to Bohr after saving")
    _add_config_flag(init)
    init.set_defaults(func=cmd_config_init)

    setter = cfg_sub.add_parser("set", help="set one configuration key")
    setter.add_argument("key", help="dotted key, e.g. model.api_key")
    setter.add_argument("value")
    _add_config_flag(setter)
    setter.set_defaults(func=cmd_config_set)

    unset = cfg_sub.add_parser("unset", help="reset one key to its default")
    unset.add_argument("key")
    _add_config_flag(unset)
    unset.set_defaults(func=cmd_config_unset)

    getter = cfg_sub.add_parser("get", help="print one key (secrets masked)")
    getter.add_argument("key")
    getter.add_argument("--json", action="store_true")
    _add_config_flag(getter)
    getter.set_defaults(func=cmd_config_get)

    show = cfg_sub.add_parser("show", help="show effective configuration")
    show.add_argument("--json", action="store_true")
    _add_config_flag(show)
    show.set_defaults(func=cmd_config_show)

    path = cfg_sub.add_parser("path", help="print the config file path")
    _add_config_flag(path)
    path.set_defaults(func=cmd_config_path)

    test = cfg_sub.add_parser("test", help="test the model endpoint and/or Bohr login")
    test.add_argument("--api", action="store_true", help="test the model endpoint")
    test.add_argument("--bohr", action="store_true", help="test the Bohr login")
    test.add_argument("--json", action="store_true")
    _add_config_flag(test)
    test.set_defaults(func=cmd_config_test)

    auth = sub.add_parser("auth", help="authenticate with Bohr or inspect auth state")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    login = auth_sub.add_parser("bohr", help="log in to Bohr (access key or browser/device)")
    login.add_argument("--access-key", help="Bohr access key")
    login.add_argument("--profile", help="Bohr CLI profile (default: ariadne)")
    login.add_argument("--device", action="store_true",
                       help="use the device-code flow for headless machines")
    login.add_argument("--no-input", action="store_true",
                       help="never prompt; require --access-key")
    login.add_argument("--check", action="store_true",
                       help="only check the current login")
    login.add_argument("--json", action="store_true")
    _add_config_flag(login)
    login.set_defaults(func=cmd_auth_bohr)

    status = auth_sub.add_parser("status", help="show model and Bohr auth state")
    status.add_argument("--json", action="store_true")
    _add_config_flag(status)
    status.set_defaults(func=cmd_auth_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except AriadneError as exc:
        print(f"\nAriadne failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
