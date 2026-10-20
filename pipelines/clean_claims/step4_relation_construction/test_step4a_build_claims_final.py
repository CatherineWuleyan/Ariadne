"""Self-contained extraction regression tests."""

import json
import shutil
import tempfile
from pathlib import Path

import step4a_build_claims_final as step4a


def _build_temp_paper(organized_conclusions: list, step2_output_records: list) -> Path:
    tmp_dir = Path(tempfile.mkdtemp())
    paper_dir = tmp_dir / "fake_paper"
    paper_dir.mkdir()
    (paper_dir / "organized_content.json").write_text(
        json.dumps(organized_conclusions, ensure_ascii=False), encoding="utf-8"
    )
    (paper_dir / "step2_output_claims.json").write_text(
        json.dumps(step2_output_records, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_dir, paper_dir


# ============================================================
# 真实数据 1: 867757662605934651/conclusion_2
#   [1][2][3][4][5][7] elaboration, [6][9][11] assertion,
#   [8] 论证(不是claim), [10] relation(connects=[6,9,11])
#   claim[6]/[9]/[11]的完整表述里各自引用了几个elaboration编号,
#   有重复引用(2/4/5被三条claim共同引用),用来测试note去重+合并。
# ============================================================


def test_multi_number_citation_in_one_bracket_gets_fully_rewritten():
    """真实数据里踩到过的bug:step2b引用多个part时,把编号写进同一对
    括号里、用逗号分隔(比如【1, 4】),不是分开写成【1】【4】——这种格式
    step2b自己的prompt规则里是明文允许的,旧版正则只认得起【单个数字】
    这一种形式,导致【1, 4】这种引用完全匹配不上,原样残留在最终输出里
    没有被替换成"claim N"/"note N"。用867760083600146646真实出现过的
    场景复现:一条claim引用【1, 4】,其中1是claim、4是note。"""
    conclusion = {
        "id": "paper:x::conclusion_5",
        "content": "irrelevant",
        "organized_parts": [
            {"number": 1, "label": "assertion", "content": "a", "source_spans": [{"start": 0, "end": 1}]},
            {"number": 4, "label": "elaboration", "content": "some elaboration content",
             "of_terms": ["something"], "source_spans": [{"start": 1, "end": 2}]},
            {"number": 9, "label": "assertion", "content": "b", "source_spans": [{"start": 2, "end": 3}]},
        ],
    }
    step2_output = [
        {"conclusion": "conclusion_5", "number": 1, "label": "assertion",
         "text": "first claim.", "is_pure_data": False, "instance_removed": False},
        {"conclusion": "conclusion_5", "number": 9, "label": "assertion",
         "text": "second claim, as described in 【1, 4】, holds.",
         "is_pure_data": False, "instance_removed": False},
    ]
    tmp_dir, paper_dir = _build_temp_paper([conclusion], step2_output)
    try:
        result = step4a.build_claims_final(paper_dir)
        claim9 = next(c for c in result["claim"] if c["text"].startswith("second claim"))
        assert "【" not in claim9["text"] and "】" not in claim9["text"]
        # 1是claim(新编号1),4是note(elaboration带of_terms,新编号1)
        assert claim9["text"] == "second claim, as described in claim 1, note 1, holds."
    finally:
        shutil.rmtree(tmp_dir)


# ============================================================
# 真实数据 2: 867750889056633390/conclusion_4
#   [1] assertion, [6] 论据(supports=[1]), [7] example(illustrates=[1])
#   用来测试单目标的论据/example关系。
# ============================================================


# ============================================================
# 真实数据 3: 867752822639165809/conclusion_7
#   claim编号(原始) 2,4,7,10,11,14,16 -> 新编号1,2,3,4,5,6,7
#   原14(论据)supports=[2,4,7,11] -> 4个目标,用来测试合并成1条、多目标
#   带括号"和"连接的场景(不再拆成多条1对1)。
# ============================================================


# ============================================================
# 合成边界情况:elaboration没有of_terms(有数字形式的of,或者完全没有)
# ============================================================

def test_elaboration_without_of_terms_gets_no_prefix():
    conclusion = {
        "id": "paper:x::conclusion_1",
        "content": "irrelevant",
        "organized_parts": [
            {"number": 1, "label": "elaboration", "content": "plain continuation text",
             "of": [3], "source_spans": [{"start": 0, "end": 1}]},
            {"number": 2, "label": "elaboration", "content": "another plain continuation",
             "source_spans": [{"start": 1, "end": 2}]},  # 完全没有of/of_terms
            {"number": 3, "label": "assertion", "content": "x", "source_spans": [{"start": 2, "end": 3}]},
        ],
    }
    step2_output = [
        {"conclusion": "conclusion_1", "number": 3, "label": "assertion",
         "text": "cites 【1】 and 【2】 here.", "is_pure_data": False, "instance_removed": False},
    ]
    tmp_dir, paper_dir = _build_temp_paper([conclusion], step2_output)
    try:
        result = step4a.build_claims_final(paper_dir)
        assert len(result["note"]) == 2
        # 两条note都不该有"elaboration of"前缀——[1]虽然带数字形式的of,
        # 但它是被claim[3]的【1】直接引用才收进来的,按更早确认过的规则
        # 不加前缀;[2]本来就没有of/of_terms
        texts = [n["text"] for n in result["note"]]
        assert texts == ["plain continuation text", "another plain continuation"]
        assert not any(t.startswith("elaboration of") for t in texts)
    finally:
        shutil.rmtree(tmp_dir)
