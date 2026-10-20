"""
test_step2e_finalize_claims.py

不需要monkeypatch call_claude,因为这一步是纯计算,不调API。用构造出来的
数据(贴合真实的867752822639165809/867750889056633390情况)验证4种文本
优先级都选对了字段、is_pure_data字段被正确保留、论据/example的
supports/illustrates指向信息被正确补回来、assertion没有这两个键、以及
main()整个流程(用临时目录跑,不影响真实数据)。

用法:
    python test_step2e_finalize_claims.py
"""

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import step2e_finalize_claims as step2e


def test_confirmed_mixed_in_uses_stripped_text():
    # conclusion_4 claim[20]:instance_containment_confirmed=True,
    # "完整表述_不含instance"应该是剥离后的版本,不是原始"完整表述"。
    r = {
        "conclusion": "conclusion_4", "number": 20, "label": "assertion",
        "text": "the combined module is applied without modifying existing scoring functions.",
        "完整表述": (
            "The combined module of Average of Pruning (AoP) is applied without modifying "
            "existing post-hoc OOD scoring methods, such as Maximum Softmax Probability (MSP), "
            "Out-of-DIstribution detector for Neural networks (ODIN), energy-based scoring "
            "(Energy), Virtual-Logit Matching (ViM), K-Nearest Neighbors (KNN)."
        ),
        "是纯实验数据": False,
        "instance_containment": True,
        "完整表述_不含instance": (
            "The combined module of Average of Pruning (AoP) is applied without modifying "
            "existing post-hoc OOD scoring methods."
        ),
        "instance_containment_confirmed": True,
    }
    result = step2e.finalize_claim(r, {})
    assert result["text"] == r["完整表述_不含instance"]
    assert result["text"] != r["完整表述"]
    assert result["instance_removed"] is True
    assert result["is_pure_data"] is False
    assert result["needs_more_context"] == []
    assert "supports" not in result and "illustrates" not in result
    assert result == {
        "conclusion": "conclusion_4", "number": 20, "label": "assertion",
        "text": r["完整表述_不含instance"], "is_pure_data": False,
        "needs_more_context": [], "instance_removed": True,
    }


def test_confirmed_not_mixed_in_uses_unchanged_text():
    # conclusion_5 claim[11]:instance_containment==True(step2c筛中了),
    # 但instance_containment_confirmed==False(step2d复核后判定不是真的
    # 混入),"完整表述_不含instance"这时候应该跟"完整表述"完全一样。
    original = "On Tiny-ImageNet, Average of Pruning (AoP) improves Maximum Softmax Probability (MSP) AUROC by 1.5–3.3 percentage points on Textures, LSUN, and iSUN."
    r = {
        "conclusion": "conclusion_5", "number": 11, "label": "assertion",
        "text": "on Tiny-ImageNet, AoP improves MSP AUROC...",
        "完整表述": original,
        "是纯实验数据": False,
        "instance_containment": True,
        "完整表述_不含instance": original,  # 跟"完整表述"一样,没被改动
        "instance_containment_confirmed": False,
    }
    result = step2e.finalize_claim(r, {})
    assert result["text"] == original
    assert result["instance_removed"] is False
    assert result["is_pure_data"] is False


def test_not_a_candidate_falls_back_to_complete_text():
    # instance_containment==False(step2c判定没混入)或者None(所在
    # conclusion没有instance)时,step2d根本不会碰这条claim,
    # "完整表述_不含instance"是null,应该退回"完整表述"。
    r = {
        "conclusion": "conclusion_4", "number": 15, "label": "assertion",
        "text": "pruning reduces learned common features",
        "完整表述": "Pruning reduces learned common features, addressing overfitting.",
        "是纯实验数据": False,
        "instance_containment": False,
        "完整表述_不含instance": None,
        "instance_containment_confirmed": None,
    }
    result = step2e.finalize_claim(r, {})
    assert result["text"] == r["完整表述"]
    assert result["instance_removed"] is False
    assert result["is_pure_data"] is False


def test_pure_data_claim_falls_back_to_raw_text_and_flag_is_kept():
    # 是纯实验数据的claim:"完整表述"和"完整表述_不含instance"都是null,
    # 应该退回最原始的"text",同时is_pure_data这个信息要原样保留下来,
    # 不能因为综合成了一条统一的text字段就把这个信息丢掉。
    r = {
        "conclusion": "conclusion_9", "number": 6, "label": "assertion",
        "text": "Spred initialization with $2u_{0}v_{0}=0$ fails to approach the ground truth for all regularization strengths tested.",
        "是纯实验数据": True,
        "完整表述": None,
        "instance_containment": None,
        "完整表述_不含instance": None,
        "instance_containment_confirmed": None,
    }
    result = step2e.finalize_claim(r, {})
    assert result["text"] == r["text"]
    assert result["instance_removed"] is False
    assert result["is_pure_data"] is True


def test_needs_more_context_is_kept_and_defaults_to_empty_list():
    # 有需要更多上下文的术语时,原样保留成列表
    r_with_context = {
        "conclusion": "conclusion_4", "number": 3, "label": "assertion",
        "text": "raw.", "完整表述": "complete.", "是纯实验数据": False,
        "instance_containment": None, "完整表述_不含instance": None,
        "instance_containment_confirmed": None,
        "需要更多上下文": ["some undefined term"],
    }
    assert step2e.finalize_claim(r_with_context, {})["needs_more_context"] == ["some undefined term"]

    # None(比如是纯数据claim,压根没跑过step2b)应该退化成空列表,不是None
    r_pure_data = {
        "conclusion": "conclusion_9", "number": 6, "label": "assertion",
        "text": "raw text.", "是纯实验数据": True, "完整表述": None,
        "instance_containment": None, "完整表述_不含instance": None,
        "instance_containment_confirmed": None, "需要更多上下文": None,
    }
    assert step2e.finalize_claim(r_pure_data, {})["needs_more_context"] == []


def test_lundata_claim_gets_supports_field():
    # 论据(label=="论据")应该带上supports指向信息。
    r = {
        "conclusion": "conclusion_4", "number": 6, "label": "论据",
        "text": "some evidence text.",
        "完整表述": "some evidence text, fully spelled out.",
        "是纯实验数据": False,
        "instance_containment": False,
        "完整表述_不含instance": None,
        "instance_containment_confirmed": None,
    }
    pointer_fields = {("conclusion_4", 6): {"supports": [1]}}
    result = step2e.finalize_claim(r, pointer_fields)
    assert result["supports"] == [1]
    assert "illustrates" not in result
    # supports字段紧跟在label后面,在text之前
    assert list(result.keys()).index("supports") < list(result.keys()).index("text")


def test_example_claim_gets_illustrates_field():
    # example(label=="example")应该带上illustrates指向信息。
    r = {
        "conclusion": "conclusion_4", "number": 7, "label": "example",
        "text": "Compared to SNIP (62.23%, 67.51%, 69.35%) and GraSP (62.85%, 67.24%, 69.23%), ETTs are much closer to IMP.",
        "完整表述": "Compared to SNIP (...) and GraSP (...), the ETT-transferred tickets are much closer to IMP-found tickets.",
        "是纯实验数据": False,
        "instance_containment": False,
        "完整表述_不含instance": None,
        "instance_containment_confirmed": None,
    }
    pointer_fields = {("conclusion_4", 7): {"illustrates": [1]}}
    result = step2e.finalize_claim(r, pointer_fields)
    assert result["illustrates"] == [1]
    assert "supports" not in result


def test_load_pointer_fields_from_real_data():
    # 用真实的867750889056633390/conclusion_4验证load_pointer_fields()
    # 读出来的结果:number=6是论据带supports=[1],number=7是example带
    # illustrates=[1],assertion的number=1不带任何指向键。
    organized_path = step2e.DATA_DIR / "867750889056633390" / "organized_content.json"
    pointer_fields = step2e.load_pointer_fields(organized_path)

    assert pointer_fields[("conclusion_4", 6)] == {"supports": [1]}
    assert pointer_fields[("conclusion_4", 7)] == {"illustrates": [1]}
    assert pointer_fields[("conclusion_4", 1)] == {}


def test_main_end_to_end_with_temp_directory():
    """跑一遍main(),不用真实数据、不影响任何真实文件,验证assertion/论据/
    example/纯数据claim混在一起时,输出文件名、顺序、字段、supports/
    illustrates指向信息都对得上。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = tmp_dir / "fake_paper"
        paper_dir.mkdir()

        organized = [{
            "id": "paper:fake::conclusion_1",
            "content": "irrelevant",
            "organized_parts": [
                {"number": 20, "label": "assertion", "content": "x", "source_spans": [{"start": 0, "end": 1}]},
                {"number": 15, "label": "论据", "content": "y", "supports": [20],
                 "source_spans": [{"start": 1, "end": 2}]},
            ],
        }, {
            "id": "paper:fake::conclusion_2",
            "content": "irrelevant2",
            "organized_parts": [
                {"number": 6, "label": "example", "content": "z", "illustrates": [1],
                 "source_spans": [{"start": 0, "end": 1}]},
            ],
        }]
        (paper_dir / "organized_content.json").write_text(json.dumps(organized), encoding="utf-8")

        records = [
            {
                "conclusion": "conclusion_1", "number": 20, "label": "assertion",
                "text": "raw.", "完整表述": "complete with SNIP.", "是纯实验数据": False,
                "instance_containment": True,
                "完整表述_不含instance": "complete.",
                "instance_containment_confirmed": True,
            },
            {
                "conclusion": "conclusion_1", "number": 15, "label": "论据",
                "text": "raw2.", "完整表述": "complete2.", "是纯实验数据": False,
                "instance_containment": False,
                "完整表述_不含instance": None,
                "instance_containment_confirmed": None,
            },
            {
                "conclusion": "conclusion_2", "number": 6, "label": "example",
                "text": "pure data raw.", "是纯实验数据": True, "完整表述": None,
                "instance_containment": None,
                "完整表述_不含instance": None,
                "instance_containment_confirmed": None,
            },
        ]
        (paper_dir / "claim_completeness_analysis.json").write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8"
        )

        with patch.object(step2e, "DATA_DIR", tmp_dir), patch("sys.argv", ["prog", "fake_paper"]):
            step2e.main()

        # 文件名不叫final_claims.json——这是step2自己的产出,不是整个
        # 项目最终会用到的那份数据,避免跟后面的step3产生混淆
        out_path = paper_dir / "step2_output_claims.json"
        assert out_path.exists()
        assert not (paper_dir / "final_claims.json").exists()

        result = json.loads(out_path.read_text(encoding="utf-8"))

        assert len(result) == 3
        assert [c["number"] for c in result] == [20, 15, 6]  # 顺序原样保留,不重排

        assert result[0] == {
            "conclusion": "conclusion_1", "number": 20, "label": "assertion",
            "text": "complete.", "is_pure_data": False,
            "needs_more_context": [], "instance_removed": True,
        }
        assert result[1] == {
            "conclusion": "conclusion_1", "number": 15, "label": "论据", "supports": [20],
            "text": "complete2.", "is_pure_data": False,
            "needs_more_context": [], "instance_removed": False,
        }
        assert result[2] == {
            "conclusion": "conclusion_2", "number": 6, "label": "example", "illustrates": [1],
            "text": "pure data raw.", "is_pure_data": True,
            "needs_more_context": [], "instance_removed": False,
        }
    finally:
        shutil.rmtree(tmp_dir)


def test_main_requires_organized_content_json():
    """organized_content.json缺失时应该直接报错退出,不应该在没有指向
    信息来源的情况下硬跑。"""
    import subprocess
    import sys as _sys

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = tmp_dir / "fake_paper"
        paper_dir.mkdir()
        (paper_dir / "claim_completeness_analysis.json").write_text("[]", encoding="utf-8")

        with patch.object(step2e, "DATA_DIR", tmp_dir), patch("sys.argv", ["prog", "fake_paper"]):
            try:
                step2e.main()
                raised = False
            except SystemExit as e:
                raised = True
                exit_code = e.code
        assert raised
        assert exit_code != 0
    finally:
        shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
