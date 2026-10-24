"""Personal-library orchestration over existing paper packages and merge stages.

The manifest is a rebuildable catalogue of immutable run artifacts, not another
knowledge graph. Paper knowledge remains owned by its compiled Gaia package.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import __version__, merge, paper2ir, retrieval, single
from .config import Config, Runtime, resolve_runtime
from .full import _confirm_spend, _preflight, _retrieval_options, _utcnow
from .render import install_outputs, render_outputs
from .util import AriadneError, REPO, harness_env, new_run_dir, read_json, run, stage_log, write_json


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(runtime: Runtime) -> str:
    """Invalidate reuse when semantic code, compiler, prompts or model change."""
    hasher = hashlib.sha256()
    for folder in (REPO / "ariadne/ariadne", REPO / "pipelines/clean_claims",
                   REPO / "pipelines/single_paper", REPO / "pipelines/merge",
                   REPO / "agent/pipeline_harness"):
        for path in sorted(folder.rglob("*")):
            if (path.is_file() and path.suffix in {".py", ".json", ".txt", ".md", ".toml", ".yaml", ".yml"}
                    and not path.name.startswith("test_")
                    and not {"data", "__pycache__", "tests"}.intersection(path.relative_to(folder).parts)):
                hasher.update(str(path.relative_to(REPO)).encode())
                hasher.update(path.read_bytes())
    hasher.update((REPO / "weekly_reports/export_single_artifacts.py").read_bytes())
    hasher.update(json.dumps({"model": runtime.model, "merge_model": runtime.merge_model,
                              "base_url": runtime.model_base_url}, sort_keys=True).encode())
    compiler = run([runtime.python, "-c",
                    "from importlib.metadata import version; print(version('gaia-lang'))"],
                   timeout=30)
    hasher.update(compiler.stdout.strip().encode())
    return hasher.hexdigest()


def artifact(path: Path, kind: str) -> dict:
    path = path.resolve()
    return {"path": str(path), "kind": kind, "sha256": digest(path)}


def verified(ref: dict) -> bool:
    try:
        return digest(Path(ref["path"])) == ref["sha256"]
    except (OSError, KeyError, TypeError):
        return False


def load_library(path: Path | None) -> dict:
    if path is None or not (path / "manifest.json").exists():
        return {"version": 1, "papers": [], "artifacts": []}
    payload = read_json(path / "manifest.json")
    if payload.get("version") != 1 or not isinstance(payload.get("papers"), list) or not isinstance(payload.get("artifacts"), list):
        raise AriadneError(f"unsupported library manifest: {path / 'manifest.json'}")
    return payload


@contextmanager
def library_lock(path: Path):
    """Fail visibly on concurrent writers; never replace someone else's lock."""
    path.mkdir(parents=True, exist_ok=True)
    lock = path / ".write-lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise AriadneError(f"library is being updated: {path}; retry after the other run finishes") from exc
    try:
        yield
    finally:
        lock.rmdir()


def publish(path: Path, papers: list[dict], view: Path, bindings: dict[str, str], code: str,
            expected_revision: str | None = None, integrations: list[dict] | None = None) -> None:
    """Atomic catalogue update only after the run and its deliveries succeed."""
    with library_lock(path):
        manifest = path / "manifest.json"
        revision = digest(manifest) if manifest.exists() else None
        if revision != expected_revision:
            raise AriadneError("library changed during this run; retry against its new version; no catalogue was overwritten")
        current = load_library(path)
        by_id = {item["lkm_id"]: item for item in current["papers"]}
        replacements = {p["source_path"]: p["lkm_id"] for p in papers if p.get("source_path")}
        by_id = {pid: p for pid, p in by_id.items() if not p.get("source_path")
                 or replacements.get(p["source_path"], pid) == pid}
        by_id.update({item["lkm_id"]: item for item in papers})
        current["papers"] = list(by_id.values())
        ref = artifact(view, "view_model")
        ref.update(bindings=bindings, fingerprint=code, integration_packages=integrations or [])
        current["artifacts"].append(ref)
        fd, temporary = tempfile.mkstemp(dir=path, prefix=".manifest-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(current, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path / "manifest.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def package_identity(package: dict) -> str:
    ir = read_json(Path(package["gaia_ir"]))
    return f"{ir['namespace']}:{ir['package_name']}"


def available_papers(catalogue: dict, code: str, runtime: Runtime) -> list[dict]:
    usable = [p for p in catalogue["papers"] if p.get("fingerprint") == code
              and p.get("artifacts") and all(verified(a) for a in p["artifacts"])]
    if usable:
        # Use the existing official-validator boundary, not JSON shape checks.
        paths = [p["package"]["gaia_ir"] for p in usable]
        check = run([runtime.python, "-c",
                     "import json,sys; from pipeline_harness.domain.compiler import _validate_with_official_gaia; "
                     "[_validate_with_official_gaia(json.load(open(p))) for p in sys.argv[1:]]",
                     *paths], env=harness_env(), timeout=120, check=False)
        if check.returncode:
            raise AriadneError("library package failed official Gaia validation; original library was preserved")
    return usable


def strip_answers(view: dict) -> dict:
    result = copy.deepcopy(view)
    old = {str(n["id"]) for n in result.get("nodes", [])
           if n.get("kind") == "answer_root" or n.get("layer") == "answer"}
    old.update(str(r["id"]) for r in (result.get("answer_layer") or {}).get("roots", []))
    result["nodes"] = [n for n in result.get("nodes", []) if str(n["id"]) not in old]
    result["edges"] = [e for e in result.get("edges", [])
                       if str(e.get("source")) not in old and str(e.get("target")) not in old]
    result["search_documents"] = [d for d in result.get("search_documents", [])
                                  if not old.intersection(map(str, d.get("refs", [])))]
    result.pop("answer_layer", None)
    if isinstance(result.get("metadata"), dict):
        result["metadata"].pop("answer_layer", None)
    return result


def combine_views(views: list[dict]) -> dict:
    result: dict = strip_answers(views[0]) if views else {"nodes": [], "edges": [], "search_documents": []}
    result["search_documents"] = []
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    for raw in views:
        view = strip_answers(raw)
        for node in view.get("nodes", []):
            nid = str(node["id"])
            if nid in nodes and nodes[nid].get("summary") != node.get("summary"):
                raise AriadneError(f"conflicting library node versions: {nid}")
            nodes[nid] = node
        for edge in view.get("edges", []):
            eid = str(edge["id"])
            def semantics(e):
                return (e.get("source"), e.get("target"), e.get("semantic_type"),
                        e.get("reasoning_type"), bool(e.get("chainable")),
                        (e.get("details") or {}).get("directed"))
            if eid in edges and semantics(edges[eid]) != semantics(edge):
                raise AriadneError(f"conflicting library edge versions: {eid}")
            edges[eid] = edge
        result["search_documents"].extend(view.get("search_documents", []))
    result["nodes"] = list(nodes.values())
    result["edges"] = list(edges.values())
    return result


def paper_view(papers: list[dict]) -> dict:
    """Project source packages through the maintained merge viewer adapter."""
    from types import SimpleNamespace
    from pipeline_harness.models import ArtifactRef
    from pipeline_merge_question.view import MergeViewAdapter
    refs = [ArtifactRef(artifact_id="paper_" + str(index), kind="gaia.ir",
                        path=p["package"]["gaia_ir"], sha256=digest(Path(p["package"]["gaia_ir"])),
                        media_type="application/json", producer_stage="input")
            for index, p in enumerate(papers)]
    store = SimpleNamespace(artifact_path=lambda ref: Path(ref.path))
    return MergeViewAdapter().project(store, refs).to_dict()


def valid_views(catalogue: dict, papers: list[dict], code: str) -> list[dict]:
    bindings = {package_identity(p["package"]): digest(Path(p["package"]["gaia_ir"])) for p in papers}
    valid = []
    covered: set[str] = set()
    for ref in reversed(catalogue["artifacts"]):
        keys = set(ref.get("bindings") or {})
        if (keys - covered and ref.get("fingerprint") == code
                and all(bindings.get(k) == v for k, v in ref["bindings"].items())
                and verified(ref)
                and all(verified(a) for p in ref.get("integration_packages", []) for a in p["artifacts"])):
            valid.append(ref)
            covered.update(keys)
    return list(reversed(valid))


def library_view(catalogue: dict, papers: list[dict], code: str) -> dict:
    views = [paper_view(papers)] if papers else []
    views.extend(read_json(Path(ref["path"])) for ref in valid_views(catalogue, papers, code))
    return combine_views(views)


def integration_packages(catalogue: dict, papers: list[dict], code: str) -> list[dict]:
    packages = {}
    for ref in valid_views(catalogue, papers, code):
        for package in ref.get("integration_packages", []):
            packages[package_identity(package)] = package
    return list(packages.values())


def compiled_integration(merged: dict) -> dict:
    directory = Path(merged["run_dir"])
    if read_json(directory / "run.json").get("status") != "succeeded":
        raise AriadneError("integration pipeline did not succeed")
    items = read_json(directory / "artifacts/index.json")["artifacts"]
    result = {"package_dir": str(directory)}
    refs = []
    for kind, key, producer in (("gaia.ir", "gaia_ir", "step5_compile_gaia_ir"),
                                ("formalization", "formalization", "step4_formalize_integration")):
        choices = [a for a in items if a["kind"] == kind and a["producer_stage"] == producer]
        if not choices:
            raise AriadneError(f"successful integration is missing {kind}")
        item = choices[-1]
        path = directory / item["path"]
        if digest(path) != item["sha256"]:
            raise AriadneError(f"integration artifact hash mismatch: {kind}")
        result[key] = str(path)
        refs.append(artifact(path, kind))
    result["artifacts"] = refs
    return result


def query_tokens(text: str) -> set[str]:
    from pipeline_merge_question.question_layer import tokens
    result = tokens(text)
    # English-only lexical retrieval otherwise misses Chinese local documents.
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return result


def node_qid(node: dict) -> str:
    return str((node.get("details") or {}).get("qid") or str(node["id"]).removeprefix("knowledge:"))


def owns(package: dict, node: dict) -> bool:
    return node_qid(node).startswith(package_identity(package) + "::")


def local_context(view: dict, question: str, limit: int = 40) -> tuple[dict, list[str]]:
    """One-hop recall followed by complete explicit premise closure, never top-k truncation of premises."""
    nodes = {str(n["id"]): n for n in view.get("nodes", [])}
    query = query_tokens(question)
    scored = []
    for nid, node in nodes.items():
        if node.get("kind") in {"note", "answer_root"}:
            continue
        words = query_tokens(str(node.get("summary") or (node.get("details") or {}).get("content") or ""))
        overlap = len(query & words)
        if overlap:
            scored.append((overlap / max(1, len(words)) ** .5, nid))
    seeds = [nid for _, nid in sorted(scored, reverse=True)[:limit]]
    selected = set(seeds)
    aliases = {node_qid(n): str(n["id"]) for n in nodes.values()}
    for edge in view.get("edges", []):
        s, t = str(edge.get("source")), str(edge.get("target"))
        if s in seeds or t in seeds:
            selected.update([s, t])
    changed = True
    while changed:
        before = len(selected)
        for nid in list(selected):
            details = nodes.get(nid, {}).get("details") or {}
            selected.update(aliases.get(p, p) for p in details.get("premises", []) if isinstance(p, str))
        for edge in view.get("edges", []):
            s, t = str(edge.get("source")), str(edge.get("target"))
            if t in selected and (edge.get("details") or {}).get("directed") is not False:
                selected.add(s)
        if len(selected) > 500:
            raise AriadneError("local evidence closure exceeds 500 nodes; use a narrower question")
        changed = len(selected) != before
    missing = selected - nodes.keys()
    if missing:
        raise AriadneError(f"library evidence has unresolved premises: {sorted(missing)[:5]}")
    local = copy.deepcopy(view)
    local["nodes"] = [nodes[nid] for nid in sorted(selected)]
    local["edges"] = [e for e in view.get("edges", [])
                      if str(e.get("source")) in selected and str(e.get("target")) in selected]
    local["search_documents"] = [d for d in view.get("search_documents", [])
                                  if selected.intersection(map(str, d.get("refs", [])))]
    return local, seeds


def assess(question: str, view: dict, call_json, *, freshness_verified: bool = False) -> dict:
    known = {str(n["id"]) for n in view.get("nodes", [])}
    if not known:
        return {"sufficient": False, "gaps": [question], "evidence_ids": [],
                "reconcile_ids": [], "reason": "No relevant evidence in this library."}
    prompt = (
        "Judge evidence coverage for this question using ONLY the supplied graph. "
        "Do not answer from general knowledge, invent facts, or treat retrieval scores as confidence. "
        "Check the requested objects, conditions, comparisons, all necessary premises, and counterevidence. "
        "A documented unresolved disagreement can be a sufficient answer. "
        "When a question asks which conclusions the supplied materials do not establish, "
        "respect its explicitly limited source scope and allow a bounded answer stating the evidence gaps "
        "when that is what the question requests, without demanding proof of the unsupported claim. "
        "Distinguish missing materials "
        "(gaps: precise search queries) from missing cross-paper relations between existing materials "
        "(reconcile_ids: the existing claim IDs needing local relation analysis). "
        "infer/analogy/same-paper edges are not deductive support. Insufficient evidence must stay insufficient. "
        "Return exactly a JSON object with sufficient:boolean, gaps:list[str], evidence_ids:list[str], "
        "reconcile_ids:list[str], reason:str. Cite only supplied IDs; a sufficient judgment needs evidence IDs, "
        "no gaps and no reconcile_ids. In evidence_ids and reconcile_ids, copy exact top-level nodes[].id values, "
        "not display labels, claim numbers, or details.qid. Do not cite a previous candidate answer as evidence.\n"
        + json.dumps({"question": question, "nodes": view["nodes"], "edges": view["edges"],
                      "fresh_retrieval_performed": freshness_verified}, ensure_ascii=False)
    )
    # Both IDs are visible in the supplied graph. Resolve a qualified claim ID
    # only when it identifies exactly one node; never guess from a short label.
    aliases: dict[str, set[str]] = {}
    for node in view["nodes"]:
        aliases.setdefault(node_qid(node), set()).add(str(node["id"]))
    for attempt in range(2):
        result = call_json(prompt)
        if (not isinstance(result, dict) or type(result.get("sufficient")) is not bool
                or not isinstance(result.get("reason"), str) or not result["reason"].strip()):
            raise AriadneError("invalid library coverage judgment")
        result = dict(result)
        for field in ("gaps", "evidence_ids", "reconcile_ids"):
            if not isinstance(result.get(field), list) or any(not isinstance(s, str) or not s.strip() for s in result[field]):
                raise AriadneError(f"invalid library judgment {field}")
        for field in ("evidence_ids", "reconcile_ids"):
            resolved = []
            for value in result[field]:
                matches = aliases.get(value, set())
                if value not in known and len(matches) == 1:
                    value = next(iter(matches))
                resolved.append(value)
            result[field] = list(dict.fromkeys(resolved))
        unknown = sorted(set(result["evidence_ids"] + result["reconcile_ids"]) - known)
        if unknown:
            if attempt == 0:
                prompt += (
                    "\nThe previous response used invalid evidence references. Correct their format once, "
                    "using only exact top-level nodes[].id values from allowed_ids below. Return the complete "
                    "judgment object with all five fields; preserve the evidence limits and do not invent "
                    "claims or strengthen the judgment just to repair an ID. All fields will be revalidated.\n"
                    + json.dumps({"allowed_ids": sorted(known), "invalid_ids": unknown,
                                  "previous_judgment": result}, ensure_ascii=False)
                )
                continue
            raise AriadneError("library judgment cites unknown evidence: " + ", ".join(unknown))
        if result["sufficient"] and (not result["evidence_ids"] or result["gaps"] or result["reconcile_ids"]):
            raise AriadneError("inconsistent sufficient-evidence judgment")
        if not result["sufficient"] and not result["gaps"] and not result["reconcile_ids"]:
            raise AriadneError("insufficient-evidence judgment has no actionable gap")
        if not freshness_verified and re.search(r"最新|最近|截至目前|\blatest\b|\bcurrent\b|\brecent\b", question, re.I):
            result.update(sufficient=False, gaps=[question], reason="Freshness requested; verify with live retrieval.")
        return result


def answer_view(view: dict, question: str, question_en: str, domain: str,
                max_roots: int, run_dir: Path, runtime: Runtime,
                coverage: dict | None = None) -> tuple[Path, Path]:
    stage = run_dir / "05_outputs"
    base = run_dir / "04_merge" / "view_model.json"
    write_json(base, strip_answers(view))
    # Explicit empty roles prevents the standalone entry point from borrowing
    # the frozen aircraft demo's audit. Typed roles embedded in edges remain valid.
    roles = run_dir / "04_merge" / "edge_roles.json"
    write_json(roles, {"edges": []})
    run([runtime.python, str(REPO / "pipelines/merge/pipeline_merge_question/run_question_layer.py"),
         "--view", str(base), "--out", str(stage), "--edge-roles", str(roles),
         "--question-zh", question, "--question-en", question_en, "--domain", domain,
         "--max-roots", str(max_roots), "--no-viewer"],
        env=harness_env(), log=stage_log(run_dir, "question_layer"), timeout=3600)
    answer, augmented = stage / "answer_layer.json", stage / "domain_view_question.json"
    if coverage and not coverage["sufficient"]:
        layer = read_json(answer)
        warning = "【证据尚不充分】" + coverage["reason"] + "；待补充：" + "; ".join(coverage["gaps"])
        for root in layer.get("roots", []):
            root["caveat"] = warning + " " + str(root.get("caveat") or "")
        view_data = read_json(augmented)
        view_data["answer_layer"] = layer
        roots = {str(r["id"]): r for r in layer.get("roots", [])}
        for node in view_data.get("nodes", []):
            if str(node["id"]) in roots:
                node.setdefault("details", {})["caveat"] = roots[str(node["id"])]["caveat"]
        write_json(answer, layer)
        write_json(augmented, view_data)
    return answer, augmented


def _catalogue_paper(paper: dict, package: dict, code: str, run_dir: Path) -> dict:
    item = dict(paper)
    for key in ("graph_path", "ocr_path"):
        p = Path(item[key])
        item[key] = str(p if p.is_absolute() else run_dir / p)
    item.update(package=package, fingerprint=code)
    item["artifacts"] = [artifact(Path(item[k]), kind) for k, kind in
                         (("graph_path", "source.graph"), ("ocr_path", "source.paper_text"))]
    item["artifacts"].extend(artifact(Path(package[k]), kind) for k, kind in
                             (("gaia_ir", "gaia.ir"), ("formalization", "formalization"),
                              ("knowledge_index", "knowledge.index")))
    return item


def _limits_sources(*questions: str) -> bool:
    """Recognize leading, explicit restrictions to already supplied evidence."""
    chinese = (
        r"^\s*(?:请\s*)?(?:仅|只|只能)(?:依据|根据|基于|使用)\s*"
        r"(?:这|上述|以上|已上传|上传的|所选|已选|提供的|现有|当前|本)"
        r"[^，。？！\n]{0,20}(?:论文|文献|材料|文档|文件|知识库)"
    )
    english = (
        r"^\s*(?:please\s+)?"
        r"(?:(?:using|use)\s+(?:only|solely)|based\s+(?:only|solely)\s+on|based\s+on\s+only)\s+"
        r"(?:these|this|the\s+(?:two|selected|provided|uploaded|existing|current)|existing|current)\b"
        r"[^.!?\n]{0,50}\b(?:papers?|documents?|materials?|sources?|library)\b"
    )
    return any(re.search(chinese, q) or re.search(english, q, re.I) for q in questions)


def run_library(question: str, *, library: Path | None = None,
                documents: list[Path] | None = None, out: Path | None = None,
                top_k: int = 8, paper_spec: Path | None = None,
                question_en: str | None = None, domain: str | None = None,
                max_roots: int = 5, runtime: Runtime | None = None,
                yes: bool = False, fulltext: bool = True, dry_run: bool = False,
                **options) -> dict[str, Any]:
    runtime = runtime or resolve_runtime(Config.load(), yes=yes)
    runtime.apply_env()
    for extra in (REPO, REPO / "agent", REPO / "pipelines/merge", REPO / "pipelines/single_paper"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    from pipeline_merge_question.step3 import _post_json
    question_en = question_en or question
    limited_sources = _limits_sources(question, question_en)
    domain = domain or "the domain named by the question"
    if not question.strip() or top_k < 1 or max_roots < 1:
        raise AriadneError("question, top-k and max-roots must be nonempty/positive")
    library = library.resolve() if library else None
    documents = retrieval.validate_documents(documents or [])
    # Local reads do not require a Bohr session. Live preflight is deferred until
    # after coverage fails, so a warm library works without a working network.
    if not runtime.model_api_key:
        raise AriadneError("library question answering needs a configured model API key")
    if paper_spec is not None:
        retrieval.read_spec(paper_spec)
    retrieval_options = _retrieval_options(runtime, **options)
    if dry_run:
        result = {"dry_run": True, "library": str(library) if library else None,
                  "documents": [str(p) for p in documents], "question": question,
                  "plan": "local evidence -> coverage -> answer / local reconcile / gap retrieval; selected documents -> LKM"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    code = fingerprint(runtime)
    catalogue = load_library(library)
    revision = digest(library / "manifest.json") if library and (library / "manifest.json").exists() else None
    papers = available_papers(catalogue, code, runtime)
    base = library_view(catalogue, papers, code)
    integrations = integration_packages(catalogue, papers, code)
    # Never write into an existing run, even when --out is supplied.
    if out is not None and Path(out).exists() and (not Path(out).is_dir() or any(Path(out).iterdir())):
        raise AriadneError(f"output directory must be empty: {out}")
    run_dir = new_run_dir(question, out)
    record = {"product": "ariadne", "version": __version__, "mode": "library",
              "status": "running", "started_at": _utcnow(),
              "question": {"zh": question, "en": question_en, "domain": domain},
              "runtime": runtime.summary(), "stages": {}, "outputs": {},
              "library": {"path": str(library) if library else None, "fingerprint": code}}
    def stage(name, action):
        started = time.monotonic()
        value = action()
        previous_seconds = record["stages"].get(name, {}).get("seconds", 0)
        record["stages"][name] = {"seconds": round(previous_seconds + time.monotonic() - started, 3)}
        write_json(run_dir / "run.json", record)
        return value
    write_json(run_dir / "run.json", record)
    def live_preflight():
        _preflight(runtime, None)
        _confirm_spend(runtime, top_k, True, 0, fulltext, retrieval_options)
        if not runtime.auto_approve_spend:
            runtime.auto_approve_spend = True  # this run's explicit prompt approved it
    try:
        live_retrieved = False
        recall_text = question + " " + question_en
        if re.search(r"[\u4e00-\u9fff]", question) and question_en == question:
            queries, planner = stage("00_query_planning", lambda: retrieval.plan_queries(
                question, domain=domain, max_queries=3,
                use_query_planner=retrieval_options["use_query_planner"]))
            record["library"]["queries"] = queries
            recall_text += " " + " ".join(q["query"] for q in queries)
        incoming = []
        if documents:
            missing = [p for p in documents if "local_" + digest(p)[:24] not in {str(v["lkm_id"]) for v in papers}]
            if missing:
                print("[documents] Selected files will be uploaded to LKM:\n  " +
                      "\n  ".join(str(p) for p in missing) +
                      f"\n[documents] Up to {len(missing):.2f} CNY for extraction.", flush=True)
                live_preflight()
            incoming.extend(stage("01_documents", lambda: retrieval.import_documents(documents, run_dir, runtime, cached=papers)))
        if paper_spec is not None:
            _preflight(runtime, paper_spec)
            needs = any(not p.get("graph_path") for p in retrieval.read_spec(paper_spec))
            if needs:
                live_preflight()
            incoming.extend(stage("01_retrieval", lambda: retrieval.retrieve(question, run_dir, paper_spec=paper_spec,
                                                                            runtime=runtime, fulltext=fulltext)))
        local, seeds = local_context(base, recall_text)
        judgment = stage("00_coverage", lambda: assess(question, local, _post_json))
        record["library"].update(coverage=judgment, seed_ids=seeds)
        if not limited_sources and not incoming and not judgment["sufficient"] and judgment["gaps"]:
            live_preflight()
            gap_question = question + "\nEvidence gaps: " + "; ".join(judgment["gaps"])
            incoming = stage("01_retrieval", lambda: retrieval.retrieve(
                gap_question, run_dir, runtime=runtime, top_k=top_k,
                fulltext=fulltext, question_en=question_en, domain=domain, **retrieval_options))
            live_retrieved = True
        def integrate(incoming, base, papers, integrations, judgment):
            local, _ = local_context(base, recall_text)
            known = {str(p["lkm_id"]): p for p in papers}
            invalidates = False
            changed = []
            for paper in {str(p["lkm_id"]): p for p in incoming}.values():
                previous = known.get(str(paper["lkm_id"]))
                graph = Path(paper["graph_path"])
                text = Path(paper["ocr_path"])
                graph = graph if graph.is_absolute() else run_dir / graph
                text = text if text.is_absolute() else run_dir / text
                if previous and digest(graph) == digest(Path(previous["graph_path"])) and digest(text) == digest(Path(previous["ocr_path"])):
                    continue
                if previous or any(paper.get("source_path") and p.get("source_path") == paper["source_path"] for p in papers):
                    invalidates = True
                changed.append(paper)
            if changed:
                claims = stage("02_paper2ir", lambda: paper2ir.run_all(
                    changed, run_dir / "02_paper2ir", run_dir, runtime=runtime,
                    min_papers=len(changed), reuse=False))
                packages = stage("03_single", lambda: single.run_all(claims, run_dir / "03_single", run_dir, runtime=runtime))
                for paper, package in zip(changed, packages):
                    for pid, old in list(known.items()):
                        if (paper.get("source_path") and old.get("source_path") == paper["source_path"]
                                and pid != str(paper["lkm_id"])):
                            del known[pid]
                    known[str(paper["lkm_id"])] = _catalogue_paper(paper, package, code, run_dir)
                papers = list(known.values())
                # Drop superseded evidence/derived views before generating an answer.
                if invalidates:
                    base = library_view(catalogue, papers, code)
                    integrations = integration_packages(catalogue, papers, code)
                else:
                    base = combine_views([base, paper_view(papers)])
                local, seeds = local_context(base, recall_text)
                old_packages = [p["package"] for p in papers if p["lkm_id"] not in {q["lkm_id"] for q in changed}
                                and any(owns(p["package"], n) for n in local["nodes"])]
                old_packages.extend(p for p in integrations if any(owns(p, n) for n in local["nodes"]))
                selected = merge._selection(packages)[1]
                if old_packages:
                    mode = "incremental"
                    merge_packages = old_packages + packages
                else:
                    mode = "bootstrap"
                    merge_packages = packages
                if len(merge_packages) == 1:
                    # A single uploaded document already has its own reasoning
                    # graph. There is no cross-paper relation to build yet.
                    base = combine_views([base, paper_view(papers)])
                else:
                    merged = stage("04_merge", lambda: merge.run_question_pipeline(
                        merge_packages, run_dir / "04_merge", question=question, question_en=question_en,
                        domain=domain, max_roots=max_roots, runtime=runtime, mode=mode, selection=selected,
                        scope_domain=f"{domain} run_{hashlib.sha256(str(run_dir).encode()).hexdigest()[:16]}_{len(integrations)}"))
                    base = combine_views([base, read_json(Path(merged["view_model"]))])
                    integrations.append(compiled_integration(merged))
                    record["library"]["merge_mode"] = mode
            elif not judgment["sufficient"] and judgment["reconcile_ids"]:
                target_nodes = [n for n in local["nodes"] if str(n["id"]) in judgment["reconcile_ids"]]
                packages = [p["package"] for p in papers if any(owns(p["package"], n) for n in local["nodes"])]
                packages.extend(p for p in integrations if any(owns(p, n) for n in local["nodes"]))
                targets = [node_qid(n) for n in target_nodes]
                # Integration packages can re-export source QIDs. Resolve an
                # ambiguous claim selection to its actual owning package instead.
                identities = {package_identity(p) for p in packages}
                for index, qid in enumerate(targets):
                    count = sum(any(k.get("id") == qid for k in read_json(Path(p["gaia_ir"])).get("knowledges", [])) for p in packages)
                    owner = qid.split("::")[0]
                    if count > 1 and owner in identities:
                        targets[index] = owner
                targets = list(dict.fromkeys(targets))
                if not packages:
                    raise AriadneError("local reconcile targets cannot resolve to existing paper packages")
                merged = stage("04_merge", lambda: merge.run_question_pipeline(
                    packages, run_dir / "04_merge", question=question, question_en=question_en,
                    domain=domain, max_roots=max_roots, runtime=runtime, mode="reconcile", selection=targets,
                    scope_domain=f"{domain} run_{hashlib.sha256(str(run_dir).encode()).hexdigest()[:16]}_{len(integrations)}"))
                base = combine_views([base, read_json(Path(merged["view_model"]))])
                integrations.append(compiled_integration(merged))
                record["library"]["merge_mode"] = "reconcile"
            return base, papers, integrations, bool(changed)

        base, papers, integrations, modified = integrate(incoming, base, papers, integrations, judgment)
        local, seeds = local_context(base, recall_text)
        if not local["nodes"]:
            raise AriadneError("no relevant evidence after retrieval/import; original library was preserved")
        final_coverage = stage("04_coverage", lambda: assess(question, local, _post_json,
                              freshness_verified=live_retrieved)) if incoming or modified or judgment["reconcile_ids"] else judgment
        if (not limited_sources and documents and paper_spec is None and not live_retrieved
                and not final_coverage["sufficient"] and final_coverage["gaps"]):
            # Imported files enter the evidence graph before deciding what to
            # search. Supplement only their concrete gaps, once per question.
            record["library"]["import_coverage"] = final_coverage
            live_preflight()
            gap_question = question + "\nEvidence gaps: " + "; ".join(final_coverage["gaps"])
            incoming = stage("01_retrieval", lambda: retrieval.retrieve(
                gap_question, run_dir, runtime=runtime, top_k=top_k,
                fulltext=fulltext, question_en=question_en, domain=domain, **retrieval_options))
            base, papers, integrations, additional = integrate(incoming, base, papers, integrations, final_coverage)
            modified = modified or additional
            local, seeds = local_context(base, recall_text)
            final_coverage = stage("04_coverage_after_retrieval", lambda: assess(
                question, local, _post_json, freshness_verified=True))
        record["library"]["final_coverage"] = final_coverage
        # One bounded retrieval round. Remaining gaps are explicit, never hidden
        # behind an endless search loop or a successful-looking answer.
        if not final_coverage["sufficient"]:
            print("[library] remaining evidence gaps: " + final_coverage["reason"], flush=True)
        answer, view = stage("05_question", lambda: answer_view(local, question, question_en, domain, max_roots, run_dir, runtime, final_coverage))
        record["outputs"] = {"answer_layer": str(answer), "view_model": str(view)}
        installed = stage("05_render", lambda: install_outputs(render_outputs(view, answer, run_dir / "05_outputs", log_dir=run_dir), run_dir))
        record["outputs"].update(viewer=installed["html"], chains=installed["markdown"])
        record.update(status="complete", finished_at=_utcnow())
        write_json(run_dir / "run.json", record)
        if library is not None and (modified or record["library"].get("merge_mode")):
            full_view = run_dir / "04_merge" / "domain_view_model.json"
            write_json(full_view, strip_answers(base))
            bindings = {package_identity(p["package"]): digest(Path(p["package"]["gaia_ir"])) for p in papers}
            publish(library, papers, full_view, bindings, code, revision, integrations)
        print(f"Ariadne library run complete\n  viewer: {installed['html']}\n  chains: {installed['markdown']}")
        return {"run_dir": str(run_dir), "viewer": installed["html"], "chains": installed["markdown"],
                "coverage": final_coverage}
    except Exception as exc:
        record.update(status="failed", error=str(exc), finished_at=_utcnow())
        write_json(run_dir / "run.json", record)
        raise
