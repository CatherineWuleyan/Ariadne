"""Tests for converting local text into a graph and running extraction."""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import build_single_text_graph as graph_builder
import run_single_text_pipeline_sync as sync_entry

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "step1_process_conclusions"))
from step1a_pipeline import extract_conclusions  # noqa: E402


def test_paper_id_format():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_id = graph_builder.build_graph_for_text("hello", tmp_dir)
        assert paper_id.startswith("text_")
        # text_ + 8位日期 + _ + 6位时间 + _ + 6位微秒
        rest = paper_id[len("text_"):]
        parts = rest.split("_")
        assert len(parts) == 3
        assert len(parts[0]) == 8 and parts[0].isdigit()   # YYYYMMDD
        assert len(parts[1]) == 6 and parts[1].isdigit()   # HHMMSS
        assert len(parts[2]) == 6 and parts[2].isdigit()   # 微秒
    finally:
        shutil.rmtree(tmp_dir)


def test_graph_json_readable_by_real_extract_conclusions():
    """不是自己断言字段对不对,是真的拿extract_conclusions()去读,确认
    下游代码真的能正确解析出这段文本。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        text = "This is the conclusion text with a claim and 【some citation-looking thing】."
        paper_id = graph_builder.build_graph_for_text(text, tmp_dir)

        graph_path = tmp_dir / paper_id / "graph.json"
        conclusions = extract_conclusions(graph_path)

        assert len(conclusions) == 1
        c = conclusions[0]
        assert c["content"] == text
        assert c["id"].split("::")[-1] == "conclusion_1"  # 下游全部代码靠这个取短id
        assert c["order"] == 0
        assert "id" in c and "global_id" in c and "title" in c
    finally:
        shutil.rmtree(tmp_dir)


def test_two_calls_get_different_paper_ids():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        id1 = graph_builder.build_graph_for_text("first", tmp_dir)
        id2 = graph_builder.build_graph_for_text("second", tmp_dir)
        assert id1 != id2
        assert (tmp_dir / id1 / "graph.json").exists()
        assert (tmp_dir / id2 / "graph.json").exists()
    finally:
        shutil.rmtree(tmp_dir)


def test_collision_raises_instead_of_silently_overwriting():
    """强制模拟"同一微秒被调用两次"这种极端情况,确认不会静默覆盖
    已有数据,而是明确报错。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        with patch.object(graph_builder, "datetime") as mock_dt:
            mock_dt.now.return_value.astimezone.return_value.strftime.return_value = "20260823_120000_000000"
            graph_builder.build_graph_for_text("first", tmp_dir)
            try:
                graph_builder.build_graph_for_text("second", tmp_dir)
                raised = False
            except FileExistsError:
                raised = True
        assert raised
    finally:
        shutil.rmtree(tmp_dir)


def test_sync_entry_generates_graph_and_calls_run_step_with_correct_paper_id():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        called_paper_ids = []

        def fake_run_step(script_path, output_path, paper_id):
            called_paper_ids.append(paper_id)
            output_path.write_text("{}")
            return True

        with patch.object(sync_entry, "DATA_DIR", tmp_dir), \
             patch.object(sync_entry.sync_pipeline, "run_step", fake_run_step):
            paper_id = sync_entry.run_single_text("some text")

        assert (tmp_dir / paper_id / "graph.json").exists()
        assert len(called_paper_ids) == len(sync_entry.sync_pipeline.STEPS)  # 全部14步都跑了
        assert all(pid == paper_id for pid in called_paper_ids)  # 每一步都传的是同一个paper_id
    finally:
        shutil.rmtree(tmp_dir)


def test_sync_entry_stops_at_first_failure():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        call_count = [0]

        def fake_run_step(script_path, output_path, paper_id):
            call_count[0] += 1
            return call_count[0] < 3  # 第3步开始失败

        with patch.object(sync_entry, "DATA_DIR", tmp_dir), \
             patch.object(sync_entry.sync_pipeline, "run_step", fake_run_step):
            sync_entry.run_single_text("some text")

        assert call_count[0] == 3  # 第3步失败就停了,没有继续跑第4步
    finally:
        shutil.rmtree(tmp_dir)




def test_strict_sync_entry_rejects_partial_final_output():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)

        def fail_after_final(script_path, output_path, paper_id, *, non_interactive=False):
            assert non_interactive
            output_path.write_text("{}")
            return script_path.name != "step4d_normalize_instance_relations.py"

        with patch.object(sync_entry, "DATA_DIR", root), \
                patch.object(sync_entry.sync_pipeline, "run_step", fail_after_final):
            try:
                sync_entry.run_single_text("some text", raise_on_failure=True)
            except RuntimeError as exc:
                assert "step4d_normalize_instance_relations.py" in str(exc)
            else:
                raise AssertionError("partial claims_final.json must not count as completed cleaning")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
