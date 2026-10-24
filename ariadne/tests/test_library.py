"""Observable library reuse, invalidation, provenance and import regressions."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from ariadne import library as lib, retrieval
from ariadne.cli import build_parser
from ariadne.config import Config, Runtime
from ariadne.util import AriadneError, write_json, read_json

for folder in (lib.REPO / "agent", lib.REPO / "pipelines/merge", lib.REPO / "pipelines/single_paper"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))


def runtime(root):
    return Runtime(config=Config.load(root / "config.json"), model_api_key="test-only",
                   model_base_url="https://example.invalid/v1", model="test", merge_model="test",
                   bohr_profile="", bohr_access_key="", bohr_host="https://example.invalid",
                   auto_approve_spend=True, payment_source="", python=sys.executable, bohr_bin="bohr")


def graph():
    return {"nodes": [
        {"id": "knowledge:papers:alpha::a", "kind": "claim", "summary": "Aircraft simulation needs wind testing", "details": {"qid": "papers:alpha::a"}},
        {"id": "knowledge:papers:beta::b", "kind": "claim", "summary": "Wind testing requires measured loads", "details": {"qid": "papers:beta::b"}},
    ], "edges": [{"id": "e1", "source": "knowledge:papers:beta::b", "target": "knowledge:papers:alpha::a",
                  "semantic_type": "deduction", "reasoning_type": "deduction", "chainable": True}],
        "search_documents": []}


def sufficient(view=None):
    return {"sufficient": True, "gaps": [], "evidence_ids": [graph()["nodes"][0]["id"]],
            "reconcile_ids": [], "reason": "Requested scope is covered by the supplied evidence."}


def package(root, name="alpha", pid="p1"):
    base = root / name
    base.mkdir(parents=True, exist_ok=True)
    write_json(base / "gaia.ir.json", {"namespace": "papers", "package_name": name,
                                       "knowledges": [], "operators": [], "strategies": []})
    write_json(base / "formalization.json", {"package": {"namespace": "papers", "name": name}})
    write_json(base / "knowledge.index.json", {})
    return {"lkm_id": pid, "title": name, "package_dir": str(base),
            "gaia_ir": str(base / "gaia.ir.json"), "formalization": str(base / "formalization.json"),
            "knowledge_index": str(base / "knowledge.index.json")}


class SourceScopeTest(unittest.TestCase):
    def test_explicit_source_limits_do_not_capture_open_research_requests(self):
        for question in (
            "仅依据这两篇论文，能否证明训练加速？",
            "请仅根据已上传文档回答问题。",
            "只能使用现有知识库，证据不足时说明。",
            "只基于上述材料，比较稳定性定义。",
            "Using only these two papers, is a speedup established?",
            "Please use only the provided documents to answer.",
            "Based solely on the uploaded papers, what remains unknown?",
        ):
            with self.subTest(question=question):
                self.assertTrue(lib._limits_sources(question))
        for question in (
            "不要仅依据这两篇论文，请补充检索。",
            "仅依据最新文献，检索训练加速方法。",
            "论文中提到“仅依据这两篇论文”，请查找更多证据。",
            "Do not use only these papers; search for more evidence.",
            "These papers only show accuracy; find evidence of runtime.",
            "Find current papers on pruning.",
        ):
            with self.subTest(question=question):
                self.assertFalse(lib._limits_sources(question))

    def test_source_limited_gaps_answer_without_external_search_or_library_mutation(self):
        for with_documents in (False, True):
            with self.subTest(with_documents=with_documents), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                selected = root / "selected.md"
                selected.write_text("Aircraft simulation evidence.")
                write_json(root / "graph.json", graph())
                paper = {"lkm_id": "local_" + lib.digest(selected)[:24],
                         "graph_path": str(root / "graph.json"), "ocr_path": str(selected),
                         "package": package(root)}
                write_json(root / "library/manifest.json", {"version": 1, "papers": [], "artifacts": []})
                original = (root / "library/manifest.json").read_bytes()
                for name, result in (("fingerprint", "code"), ("available_papers", [paper]),
                                     ("library_view", graph())):
                    stack.enter_context(mock.patch.object(lib, name, return_value=result))
                gap = sufficient()
                gap.update(sufficient=False, gaps=["end-to-end runtime measurements"],
                           reason="The supplied papers do not establish runtime savings.")
                stack.enter_context(mock.patch("pipeline_merge_question.step3._post_json", return_value=gap))
                imported = stack.enter_context(mock.patch.object(retrieval, "import_documents", return_value=[paper]))
                prohibited = [stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError(name)))
                              for owner, name in ((retrieval, "retrieve"), (lib, "_preflight"),
                                                  (lib.paper2ir, "run_all"), (lib.single, "run_all"),
                                                  (lib.merge, "run_question_pipeline"))]
                def answer(*args):
                    self.assertFalse(args[-1]["sufficient"])
                    self.assertEqual(args[-1]["gaps"], gap["gaps"])
                    out = args[5] / "05_outputs"
                    write_json(out / "answer_layer.json", {"roots": []})
                    write_json(out / "domain_view_question.json", graph())
                    return out / "answer_layer.json", out / "domain_view_question.json"
                stack.enter_context(mock.patch.object(lib, "answer_view", side_effect=answer))
                stack.enter_context(mock.patch.object(lib, "render_outputs", return_value={}))
                stack.enter_context(mock.patch.object(lib, "install_outputs", return_value={"html": "viewer", "markdown": "chains"}))
                result = lib.run_library(
                    "仅依据这两篇论文，飞机仿真是否加速？",
                    question_en="Using only these two papers, is an aircraft simulation speedup established?",
                    library=root / "library", documents=[selected] if with_documents else None,
                    out=root / "run", runtime=runtime(root),
                )
                self.assertFalse(result["coverage"]["sufficient"])
                self.assertEqual((root / "library/manifest.json").read_bytes(), original)
                for method in prohibited:
                    method.assert_not_called()
                self.assertEqual(imported.call_count, int(with_documents))


class LocalEvidenceTest(unittest.TestCase):
    def test_recall_completes_premises_and_keeps_counterevidence(self):
        v = graph()
        v["nodes"].extend([
            {"id": "condition", "summary": "Only under fixed pressure", "details": {}},
            {"id": "counter", "summary": "Contradictory observation", "details": {}},
        ])
        v["nodes"][1]["details"]["premises"] = ["condition"]
        v["edges"].append({"id": "negative", "source": "counter", "target": v["nodes"][0]["id"], "semantic_type": "contradiction"})
        context, seeds = lib.local_context(v, "aircraft", limit=1)
        self.assertEqual(len(seeds), 1)
        self.assertEqual({n["id"] for n in context["nodes"]}, {n["id"] for n in v["nodes"]})
        self.assertEqual(context["edges"], v["edges"])

    def test_prior_answer_is_removed_without_mutating_source(self):
        v = graph()
        v["nodes"].append({"id": "answer:r1", "kind": "answer_root", "summary": "old answer"})
        v["edges"].append({"id": "old", "source": v["nodes"][0]["id"], "target": "answer:r1"})
        v["answer_layer"] = {"roots": [{"id": "answer:r1"}]}
        clean = lib.strip_answers(v)
        self.assertNotIn("answer_layer", clean)
        self.assertEqual(len(clean["nodes"]), 2)
        self.assertEqual(len(v["nodes"]), 3)

    def test_unknown_premise_fails_closed(self):
        v = graph()
        v["nodes"][0]["details"]["premises"] = ["missing"]
        with self.assertRaises(AriadneError):
            lib.local_context(v, "aircraft")

    def test_gate_requires_real_evidence_and_actionable_gaps(self):
        for change in ({"evidence_ids": ["invented"]}, {"evidence_ids": []},
                       {"gaps": ["missing comparison"]}, {"sufficient": False}):
            invalid = sufficient()
            invalid.update(change)
            with self.subTest(change=change), self.assertRaises(AriadneError):
                lib.assess("aircraft", graph(), lambda _: invalid)
        self.assertTrue(lib.assess("aircraft", graph(), lambda _: sufficient())["sufficient"])

    def test_freshness_cannot_be_satisfied_by_stale_local_evidence(self):
        result = lib.assess("latest aircraft methods", graph(), lambda _: sufficient())
        self.assertFalse(result["sufficient"])
        self.assertTrue(result["gaps"])
        result = lib.assess("latest aircraft methods", graph(), lambda _: sufficient(), freshness_verified=True)
        self.assertTrue(result["sufficient"])

    def test_coverage_accepts_unique_qualified_ids_from_supplied_nodes(self):
        answer = sufficient()
        answer["evidence_ids"] = ["papers:alpha::a", graph()["nodes"][0]["id"]]
        result = lib.assess("aircraft", graph(), lambda _: answer)
        self.assertEqual(result["evidence_ids"], [graph()["nodes"][0]["id"]])
        answer.update(sufficient=False, reconcile_ids=["papers:beta::b"])
        result = lib.assess("aircraft", graph(), lambda _: answer)
        self.assertEqual(result["reconcile_ids"], [graph()["nodes"][1]["id"]])

    def test_coverage_rejects_ambiguous_aliases_and_short_labels(self):
        view = graph()
        duplicate = copy.deepcopy(view["nodes"][0])
        duplicate["id"] = "another-node"
        view["nodes"].append(duplicate)
        for value in ("papers:alpha::a", "a", "papers:alpha::invented"):
            answer = sufficient()
            answer["evidence_ids"] = [value]
            with self.subTest(value=value), self.assertRaises(AriadneError):
                lib.assess("aircraft", view, lambda _: answer)

    def test_coverage_retries_invalid_ids_once_with_explicit_allowed_ids(self):
        invalid = sufficient()
        invalid["evidence_ids"] = ["claim_999"]
        call = mock.Mock(side_effect=[invalid, sufficient()])
        result = lib.assess("aircraft", graph(), call)
        self.assertEqual(result["evidence_ids"], sufficient()["evidence_ids"])
        self.assertEqual(call.call_count, 2)
        correction = call.call_args_list[1].args[0]
        self.assertIn('"invalid_ids": ["claim_999"]', correction)
        self.assertIn('"allowed_ids":', correction)
        for node in graph()["nodes"]:
            self.assertIn(node["id"], correction)

    def test_coverage_valid_response_needs_only_one_call(self):
        call = mock.Mock(return_value=sufficient())
        self.assertTrue(lib.assess("aircraft", graph(), call)["sufficient"])
        call.assert_called_once()
        self.assertIn("top-level nodes[].id", call.call_args.args[0])

    def test_coverage_second_unknown_id_fails_with_the_invalid_id(self):
        invalid = sufficient()
        invalid["evidence_ids"] = ["invented:claim_999"]
        call = mock.Mock(return_value=invalid)
        with self.assertRaisesRegex(AriadneError, "unknown evidence: invented:claim_999"):
            lib.assess("aircraft", graph(), call)
        self.assertEqual(call.call_count, 2)

    def test_coverage_retry_does_not_guess_ambiguous_or_short_ids(self):
        view = graph()
        duplicate = copy.deepcopy(view["nodes"][0])
        duplicate["id"] = "another-node"
        view["nodes"].append(duplicate)
        for value in ("papers:alpha::a", "a", "papers:alpha::invented"):
            invalid = sufficient()
            invalid["evidence_ids"] = [value]
            call = mock.Mock(return_value=invalid)
            with self.subTest(value=value), self.assertRaisesRegex(AriadneError, "unknown evidence"):
                lib.assess("aircraft", view, call)
            self.assertEqual(call.call_count, 2)

    def test_coverage_correction_is_fully_revalidated(self):
        invalid = sufficient()
        invalid["evidence_ids"] = ["wrong-format"]
        changes = ({"sufficient": "true"}, {"reason": ""}, {"gaps": "none"},
                   {"evidence_ids": [""]}, {"reconcile_ids": None},
                   {"gaps": ["still missing"]}, {"evidence_ids": []},
                   {"sufficient": False})
        for change in changes:
            corrected = sufficient()
            corrected.update(change)
            call = mock.Mock(side_effect=[invalid, corrected])
            with self.subTest(change=change), self.assertRaises(AriadneError):
                lib.assess("aircraft", graph(), call)
            self.assertEqual(call.call_count, 2)

    def test_conflicting_versions_never_silently_merge(self):
        changed = graph()
        changed["nodes"][0]["summary"] = "changed condition"
        with self.assertRaises(AriadneError):
            lib.combine_views([graph(), changed])


class LibraryRunTest(unittest.TestCase):
    def test_single_selected_document_does_not_require_cross_paper_merge(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            pkg = package(root)
            write_json(root / "source/graph.json", {"nodes": [{"id": "a"}]})
            (root / "source/ocr.md").write_text("Aircraft simulation")
            paper = {"lkm_id": "p1", "graph_path": str(root / "source/graph.json"), "ocr_path": str(root / "source/ocr.md")}
            write_json(root / "papers.json", {"papers": [paper]})
            stack.enter_context(mock.patch.object(lib, "fingerprint", return_value="code"))
            stack.enter_context(mock.patch.object(lib, "_preflight", return_value={}))
            stack.enter_context(mock.patch.object(retrieval, "retrieve", return_value=[paper]))
            stack.enter_context(mock.patch.object(lib.paper2ir, "run_all", return_value=[paper]))
            stack.enter_context(mock.patch.object(lib.single, "run_all", return_value=[pkg]))
            stack.enter_context(mock.patch.object(lib, "paper_view", return_value=graph()))
            stack.enter_context(mock.patch("pipeline_merge_question.step3._post_json", return_value=sufficient()))
            merger = stack.enter_context(mock.patch.object(lib.merge, "run_question_pipeline", side_effect=AssertionError("unneeded cross-paper merge")))
            def answer(*args):
                directory = args[5] / "05_outputs"
                write_json(directory / "answer_layer.json", {"roots": []})
                write_json(directory / "domain_view_question.json", graph())
                return directory / "answer_layer.json", directory / "domain_view_question.json"
            stack.enter_context(mock.patch.object(lib, "answer_view", side_effect=answer))
            stack.enter_context(mock.patch.object(lib, "render_outputs", return_value={}))
            stack.enter_context(mock.patch.object(lib, "install_outputs", return_value={"html": "viewer", "markdown": "chains"}))
            lib.run_library("aircraft", library=root / "lib", paper_spec=root / "papers.json", out=root / "run", runtime=runtime(root))
            merger.assert_not_called()
            self.assertEqual(len(lib.load_library(root / "lib")["papers"]), 1)

    def test_cold_build_warm_reuse_then_incremental_import(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            r = runtime(root)
            packages = {pid: package(root, name, pid) for pid, name in
                        (("p1", "alpha"), ("p2", "beta"), ("p3", "gamma"))}
            incoming = []
            for pid in packages:
                out = root / "inputs" / pid
                write_json(out / "graph.json", {"nodes": [{"id": pid}]})
                (out / "ocr.md").write_text("Aircraft simulation measured loads " + pid)
                incoming.append({"lkm_id": pid, "title": pid, "graph_path": str(out / "graph.json"),
                                 "ocr_path": str(out / "ocr.md")})
            spec = root / "papers.json"
            write_json(spec, {"papers": incoming[:2]})
            stack.enter_context(mock.patch.object(lib, "fingerprint", return_value="code"))
            stack.enter_context(mock.patch.object(lib, "_preflight", return_value={}))
            stack.enter_context(mock.patch.object(lib, "available_papers", side_effect=lambda c, *_: c["papers"]))
            stack.enter_context(mock.patch.object(lib, "paper_view", side_effect=lambda _: graph()))
            fetch = stack.enter_context(mock.patch.object(retrieval, "retrieve", return_value=incoming[:2]))
            build = stack.enter_context(mock.patch.object(lib.paper2ir, "run_all", side_effect=lambda p, *_a, **_k: p))
            stack.enter_context(mock.patch.object(lib.single, "run_all", side_effect=lambda p, *_a, **_k: [packages[q["lkm_id"]] for q in p]))
            def merged(*args, **kwargs):
                out = args[1]
                write_json(out / "view_model.json", graph())
                return {"view_model": str(out / "view_model.json"), "run_dir": str(out)}
            merger = stack.enter_context(mock.patch.object(lib.merge, "run_question_pipeline", side_effect=merged))
            stack.enter_context(mock.patch.object(lib, "compiled_integration", return_value={"package_dir": packages["p3"]["package_dir"],
                                                                                          "gaia_ir": packages["p3"]["gaia_ir"], "artifacts": []}))
            judge = stack.enter_context(mock.patch("pipeline_merge_question.step3._post_json", return_value=sufficient()))
            def answer(*args):
                out = args[5] / "05_outputs"
                write_json(out / "answer_layer.json", {"roots": []})
                write_json(out / "domain_view_question.json", graph())
                return out / "answer_layer.json", out / "domain_view_question.json"
            stack.enter_context(mock.patch.object(lib, "answer_view", side_effect=answer))
            stack.enter_context(mock.patch.object(lib, "render_outputs", return_value={}))
            stack.enter_context(mock.patch.object(lib, "install_outputs", return_value={"html": "viewer", "markdown": "chains"}))
            lib.run_library("aircraft", library=root / "lib", paper_spec=spec, out=root / "first", runtime=r)
            self.assertEqual(merger.call_args.kwargs["mode"], "bootstrap")
            self.assertEqual(len(build.call_args.args[0]), 2)
            lib.run_library("aircraft", library=root / "lib", out=root / "second", runtime=r)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(build.call_count, 1)
            self.assertEqual(merger.call_count, 1)
            write_json(spec, {"papers": incoming[2:]})
            fetch.return_value = incoming[2:]
            lib.run_library("aircraft", library=root / "lib", paper_spec=spec, out=root / "third", runtime=r)
            self.assertEqual(merger.call_args.kwargs["mode"], "incremental")
            self.assertEqual(merger.call_args.kwargs["selection"], ["papers:gamma"])
            self.assertEqual([p["lkm_id"] for p in build.call_args.args[0]], ["p3"])
            gap = sufficient()
            gap.update(sufficient=False, gaps=["matched wind-tunnel comparison"])
            judge.side_effect = [gap, sufficient()]
            lib.run_library("aircraft", library=root / "lib", out=root / "fourth", runtime=r)
            self.assertIn("matched wind-tunnel comparison", fetch.call_args.args[0])
            self.assertEqual(build.call_count, 2)  # retrieval returned an already-known paper
            missing_relation = sufficient()
            missing_relation.update(sufficient=False, reconcile_ids=[graph()["nodes"][0]["id"]])
            judge.side_effect = [missing_relation, sufficient()]
            fetch_count = fetch.call_count
            lib.run_library("aircraft", library=root / "lib", out=root / "fifth", runtime=r)
            self.assertEqual(fetch.call_count, fetch_count)
            self.assertEqual(merger.call_args.kwargs["mode"], "reconcile")
            self.assertEqual(merger.call_args.kwargs["selection"], ["papers:alpha::a"])
            selected = root / "selected.md"
            selected.write_text("Aircraft calibration notes")
            stack.enter_context(mock.patch.object(retrieval, "validate_documents", return_value=[selected]))
            stack.enter_context(mock.patch.object(retrieval, "import_documents", return_value=incoming[2:]))
            judge.side_effect = [sufficient(), gap, sufficient()]
            fetch_count = fetch.call_count
            lib.run_library("aircraft", library=root / "lib", documents=[selected], out=root / "sixth", runtime=r)
            self.assertEqual(fetch.call_count, fetch_count + 1)
            self.assertIn("matched wind-tunnel comparison", fetch.call_args.args[0])
            record = read_json(root / "sixth/run.json")
            self.assertEqual(record["library"]["import_coverage"]["gaps"], gap["gaps"])

    def test_warm_followup_skips_retrieval_all_build_stages_and_lkm_preflight(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            r = runtime(root)
            write_json(root / "personal/manifest.json", {"version": 1, "papers": [], "artifacts": []})
            original_manifest = (root / "personal/manifest.json").read_bytes()
            for name, result in (("fingerprint", "code"), ("available_papers", []),
                                 ("library_view", graph())):
                stack.enter_context(mock.patch.object(lib, name, return_value=result))
            stack.enter_context(mock.patch("pipeline_merge_question.step3._post_json", return_value=sufficient()))
            prohibited = [stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError(name)))
                          for owner, name in ((retrieval, "retrieve"), (lib.paper2ir, "run_all"),
                                              (lib.single, "run_all"), (lib.merge, "run_question_pipeline"),
                                              (lib, "_preflight"))]
            def answer(*args):
                out = args[5] / "05_outputs"
                write_json(out / "answer_layer.json", {"roots": []})
                write_json(out / "domain_view_question.json", graph())
                return out / "answer_layer.json", out / "domain_view_question.json"
            stack.enter_context(mock.patch.object(lib, "answer_view", side_effect=answer))
            stack.enter_context(mock.patch.object(lib, "render_outputs", return_value={}))
            stack.enter_context(mock.patch.object(lib, "install_outputs", return_value={"html": "viewer", "markdown": "chains"}))
            result = lib.run_library("aircraft simulation", library=root / "personal", out=root / "run", runtime=r)
            record = read_json(root / "run/run.json")
            self.assertEqual(record["status"], "complete")
            self.assertTrue(result["coverage"]["sufficient"])
            self.assertNotIn("02_paper2ir", record["stages"])
            self.assertTrue((root / "personal/manifest.json").is_file())
            self.assertEqual((root / "personal/manifest.json").read_bytes(), original_manifest)
            for method in prohibited:
                method.assert_not_called()

    def test_dry_run_never_writes_or_calls_remote(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(lib, "fingerprint", side_effect=AssertionError("no subprocess")):
            root = Path(tmp)
            result = lib.run_library("aircraft", library=root / "lib", out=root / "out", runtime=runtime(root), dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertFalse((root / "out").exists())
            self.assertFalse((root / "lib").exists())

    def test_failed_answer_preserves_library_and_records_failure(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            directory = root / "lib"
            write_json(directory / "manifest.json", {"version": 1, "papers": [], "artifacts": []})
            before = (directory / "manifest.json").read_bytes()
            for name, result in (("fingerprint", "code"), ("available_papers", []), ("library_view", graph())):
                stack.enter_context(mock.patch.object(lib, name, return_value=result))
            stack.enter_context(mock.patch("pipeline_merge_question.step3._post_json", return_value=sufficient()))
            stack.enter_context(mock.patch.object(lib, "answer_view", side_effect=AriadneError("provider unavailable")))
            with self.assertRaises(AriadneError):
                lib.run_library("aircraft", library=directory, out=root / "run", runtime=runtime(root))
            self.assertEqual((directory / "manifest.json").read_bytes(), before)
            self.assertEqual(read_json(root / "run/run.json")["status"], "failed")

    def test_manifest_hash_and_pipeline_version_invalidate_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = package(root)
            write_json(root / "graph.json", {"nodes": []})
            (root / "ocr.md").write_text("paper source")
            item = lib._catalogue_paper({"lkm_id": "p1", "graph_path": str(root / "graph.json"),
                                         "ocr_path": str(root / "ocr.md")}, pkg, "code", root)
            manifest = {"papers": [item]}
            with mock.patch.object(lib, "run", return_value=mock.Mock(returncode=0)):
                self.assertEqual(len(lib.available_papers(manifest, "code", runtime(root))), 1)
                self.assertEqual(lib.available_papers(manifest, "new-code", runtime(root)), [])
                (root / "ocr.md").write_text("revised source")
                self.assertEqual(lib.available_papers(manifest, "code", runtime(root)), [])

    def test_registry_lock_never_overwrites_another_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with lib.library_lock(root):
                with self.assertRaises(AriadneError):
                    with lib.library_lock(root):
                        self.fail("second writer acquired library")
            self.assertFalse((root / ".write-lock").exists())

    def test_publish_detects_concurrent_catalogue_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            view = root / "view.json"
            write_json(view, graph())
            lib.publish(root / "lib", [], view, {}, "code")
            before = (root / "lib/manifest.json").read_bytes()
            with self.assertRaises(AriadneError):
                lib.publish(root / "lib", [], view, {}, "code", expected_revision="stale")
            self.assertEqual((root / "lib/manifest.json").read_bytes(), before)

    def test_revised_document_replaces_old_catalogue_entry_but_preserves_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            view = root / "view.json"
            write_json(view, graph())
            lib.publish(root / "lib", [{"lkm_id": "old", "source_path": "/chosen/notes.md"}], view, {}, "code")
            revision = lib.digest(root / "lib/manifest.json")
            lib.publish(root / "lib", [{"lkm_id": "new", "source_path": "/chosen/notes.md"}], view, {}, "code", revision)
            catalogue = lib.load_library(root / "lib")
            self.assertEqual([p["lkm_id"] for p in catalogue["papers"]], ["new"])
            self.assertEqual(len(catalogue["artifacts"]), 2)

    def test_gaia_ir_only_integration_is_a_valid_merge_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = package(root)
            Path(pkg["formalization"]).unlink()
            Path(pkg["knowledge_index"]).unlink()
            refs, names = lib.merge._selection([{"package_dir": pkg["package_dir"], "gaia_ir": pkg["gaia_ir"]}])
            self.assertEqual(names, ["papers:alpha"])
            self.assertEqual([r["kind"] for r in refs], ["gaia.ir"])

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(lib, "fingerprint", return_value="code"), \
                mock.patch.object(lib, "available_papers", return_value=[]), mock.patch.object(lib, "library_view", return_value=graph()):
            root = Path(tmp)
            (root / "keep").write_text("original")
            with self.assertRaises(AriadneError):
                lib.run_library("aircraft", out=root, runtime=runtime(root))
            self.assertEqual((root / "keep").read_text(), "original")


@unittest.skipUnless(importlib.util.find_spec("reportlab") and importlib.util.find_spec("pypdf"), "document dependencies not installed")
class DocumentImportTest(unittest.TestCase):
    def test_uploaded_content_id_survives_paper_spec_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid = "local_84fb6d3f5adc4e9a5e506483"
            write_json(root / "papers.json", {"papers": [{
                "lkm_id": pid, "title": "Uploaded PDF",
                "graph_path": "graph.json", "ocr_path": "ocr.md",
            }]})
            self.assertEqual(retrieval.read_spec(root / "papers.json")[0]["lkm_id"], pid)
            self.assertEqual(retrieval._paper_id("paper:" + pid), pid)
            self.assertEqual(retrieval._paper_id("paper:812768401289117696"), "812768401289117696")

    def test_markdown_transport_preserves_chinese_and_formula_source(self):
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text("# 飞机仿真\n\nUse $F=ma$ under fixed pressure.\n\n中文证据与边界条件。\n")
            pdf = retrieval.markdown_pdf(source, root / "source.pdf")
            text = "".join(p.extract_text() for p in PdfReader(pdf).pages)
            self.assertIn("飞机仿真", text)
            self.assertIn("F=ma", text)
            self.assertIn("中文证据与边界条件", text)

    def test_markdown_transport_preserves_scientific_unicode(self):
        import shutil
        import subprocess
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "science.md"
            expected = "中文铌硒 Å é ó ö ü ć á m² m³ μm β λ κ ξ Φ ⊥ ∑ ∫ ≠ ≤ ≥ 25°C"
            source.write_text(expected)
            pdf = retrieval.markdown_pdf(source, root / "science.pdf")
            actual = "".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
            self.assertEqual("".join(actual.split()), "".join(expected.split()))
            if shutil.which("pdftotext"):
                # pypdf can echo CID codepoints even when PDF renderers lose
                # their glyphs; an independent extractor catches that failure.
                rendered_text = subprocess.check_output(
                    ["pdftotext", "-layout", str(pdf), "-"], text=True,
                )
                self.assertEqual("".join(rendered_text.split()), "".join(expected.split()))

    def test_unsupported_markdown_character_blocks_batch_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(retrieval, "_extract_graph") as extract:
            root = Path(tmp)
            (root / "valid.md").write_text("Valid source m²")
            (root / "unsupported.md").write_text("Unsupported character: 😀")
            with self.assertRaisesRegex(AriadneError, r"unsupported PDF character U\+1F600"):
                retrieval.import_documents([root / "valid.md", root / "unsupported.md"], root / "run", runtime(root))
            extract.assert_not_called()

    def test_changed_pdf_text_is_rejected_before_upload(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(retrieval, "_extract_graph") as extract:
            root = Path(tmp)
            source = root / "science.md"
            source.write_text("Preserve Å and m²")
            corrupt = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: "lost text")])
            with mock.patch("pypdf.PdfReader", return_value=corrupt), \
                    self.assertRaisesRegex(AriadneError, "conversion changed text"):
                retrieval.import_documents([source], root / "run", runtime(root))
            extract.assert_not_called()
            self.assertFalse(list((root / "run").rglob("source.pdf")))

    def test_all_inputs_are_validated_before_first_upload(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(retrieval, "_extract_graph") as extract:
            root = Path(tmp)
            (root / "valid.md").write_text("Aircraft simulation")
            with self.assertRaises(AriadneError):
                retrieval.import_documents([root / "valid.md", root / "missing.pdf"], root / "run", runtime(root))
            extract.assert_not_called()

    def test_import_uses_lkm_and_original_markdown_then_reuses_content_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "selected.md"
            source.write_text("# Aircraft simulation\n\nMeasured loads constrain the model.")
            def extract(record, dest, *args, **kwargs):
                write_json(dest, {"nodes": [{"id": "a"}]})
                return dest, {"nodes": [{"id": "a"}]}, {"graph_source": "lkm_extract"}
            with mock.patch.object(retrieval, "_extract_graph", side_effect=extract) as method:
                first = retrieval.import_documents([source], root / "first", runtime(root))
                self.assertEqual(method.call_count, 1)
                second = retrieval.import_documents([source], root / "second", runtime(root), cached=first)
                self.assertEqual(method.call_count, 1)
                self.assertEqual(first[0]["lkm_id"], second[0]["lkm_id"])
                self.assertEqual(Path(first[0]["ocr_path"]).read_text(), source.read_text())
                source.write_text("# Revised aircraft evidence")
                revised = retrieval.import_documents([source], root / "third", runtime(root), cached=first)
                self.assertNotEqual(first[0]["lkm_id"], revised[0]["lkm_id"])
                self.assertEqual(method.call_count, 2)

    def test_lkm_page_limit_is_checked_before_upload(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "long.pdf"
            writer = PdfWriter()
            for _ in range(51):
                writer.add_blank_page(width=100, height=100)
            writer.write(path)
            with self.assertRaises(AriadneError):
                retrieval.validate_documents([path])


class LibraryCLITest(unittest.TestCase):
    def test_selected_documents_and_library(self):
        args = build_parser().parse_args(["ask", "aircraft", "--library", "personal", "--documents", "a.pdf", "b.md", "--yes"])
        self.assertEqual(args.library, "personal")
        self.assertEqual(args.documents, ["a.pdf", "b.md"])

    def test_incremental_selection_excludes_existing_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            path = lib.merge.build_pipeline(out, "aircraft", "aircraft", "test", 5,
                                            ["papers:new"], mode="incremental", scope_domain="test run_unique")
            stage = read_json(path)["stages"][0]
            self.assertEqual(stage["options"]["mode"], "incremental")
            self.assertEqual(stage["options"]["scope"]["selection"], ["papers:new"])


if __name__ == "__main__":
    unittest.main()
