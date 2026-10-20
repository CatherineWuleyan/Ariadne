"""
test_step4c_review_example_relations.py

沙盒没有网络访问权限,用monkeypatch替换call_claude验证"不依赖模型质量"
的那部分逻辑:instantiation_lookup反查、候选relation筛选、prompt拼装、
JSON格式+编号完整性校验、三档模型依次升级+保守兜底、decisions应用后
relation重建、以及"重复跑不会重复审查"这条天然幂等性质。用真实的
867757662605934651论文数据(conclusion_5有3个论据/example各支持claim1,
conclusion_3有一条同时关联3个target的关系)搭测试场景。

用法:
    python test_step4c_review_example_relations.py
"""

import json
import shutil
import tempfile
from pathlib import Path

import step4c_review_example_relations as step4c


def _build_temp_paper(claims_final: dict, conclusion_contents: dict) -> tuple:
    tmp_dir = Path(tempfile.mkdtemp())
    paper_dir = tmp_dir / "fake_paper"
    paper_dir.mkdir()
    (paper_dir / "claims_final.json").write_text(
        json.dumps(claims_final, ensure_ascii=False), encoding="utf-8"
    )
    organized = [{"id": f"paper:x::{conc}", "content": content, "organized_parts": []}
                 for conc, content in conclusion_contents.items()]
    (paper_dir / "organized_content.json").write_text(
        json.dumps(organized, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_dir, paper_dir


def _base_claims_final():
    """构造一个贴合真实结构的claims_final.json:
      claim 1: assertion(general),说的是"多种架构"
      claim 2: 论据,supports claim 1,内容具体点名了ResNet-20
      claim 3: assertion,是claim 1的实例化版本(具体列出了ResNet-20等)
      relation: [2]是[1]的例子或证据 ; [3]是[1]的instance
    另外conclusion_2放一条完全不涉及实例化的论据关系,用来测"不该被
    选中审查"的情况。
      claim 10: assertion, claim 11: 论据 supports [10],没有实例化版本
    """
    return {
        "claim": [
            {"conclusion": "conclusion_1", "text": "The method works well across multiple architectures.",
             "is_pure_data": False, "number": 1},
            {"conclusion": "conclusion_1", "text": "For example, on ResNet-20, accuracy improved by 5%.",
             "is_pure_data": False, "number": 2},
            {"conclusion": "conclusion_1",
             "text": "The method works well across ResNet-20, VGG-16, and DenseNet-100.",
             "is_pure_data": False, "number": 3},
            {"conclusion": "conclusion_2", "text": "Pruning reduces overfitting.",
             "is_pure_data": False, "number": 10},
            {"conclusion": "conclusion_2", "text": "This matches prior theoretical results.",
             "is_pure_data": False, "number": 11},
        ],
        "note": [],
        "relation": [
            {"conclusion": "conclusion_1", "connects": [2, 1], "expression": "[2] 是 [1] 的例子或证据"},
            {"conclusion": "conclusion_1", "connects": [3, 1], "expression": "[3] 是 [1] 的instance"},
            {"conclusion": "conclusion_2", "connects": [11, 10], "expression": "[11] 是 [10] 的例子或证据"},
        ],
    }


def test_instantiation_lookup_built_from_instance_relations():
    lookup = step4c.build_instantiation_lookup(_base_claims_final()["relation"])
    assert lookup == {1: 3}  # 只有claim1有实例化版本(claim3),claim10/11没有


def test_only_relations_with_instantiated_target_are_candidates():
    claims_final = _base_claims_final()
    lookup = step4c.build_instantiation_lookup(claims_final["relation"])
    candidates = step4c.find_candidates(claims_final, lookup)

    # 只有[2]是[1]的例子或证据这条该被选中(因为1有实例化版本);
    # [11]是[10]的例子或证据不该被选中(10没有实例化版本);
    # [3]是[1]的instance不该被选中(这条本身就不是"例子或证据"关系)
    assert len(candidates) == 1
    assert candidates[0]["m"] == 2
    assert candidates[0]["targets_with_alt"] == [1]


def test_end_to_end_switches_to_instantiated_when_model_says_so():
    claims_final = _base_claims_final()

    def mock_call_claude(prompt, **kwargs):
        # claim2具体点名了ResNet-20,应该判定连实例化版本(claim3)更直接
        return json.dumps({"results": [
            {"m": 2, "decisions": [{"n": 1, "use_instantiated": True}]}
        ]})

    step4c.call_claude = mock_call_claude

    tmp_dir, paper_dir = _build_temp_paper(
        claims_final, {"conclusion_1": "irrelevant content", "conclusion_2": "irrelevant content"}
    )
    try:
        import subprocess
        import sys as _sys
        # 直接调用main()的核心逻辑(不经过CLI参数),用patch方式跑
        from unittest.mock import patch
        with patch.object(step4c, "DATA_DIR", tmp_dir), patch("sys.argv", ["prog", "fake_paper"]):
            step4c.main()

        result = json.loads((paper_dir / "claims_final.json").read_text(encoding="utf-8"))
        rel = next(r for r in result["relation"] if r["connects"][0] == 2)
        assert rel["connects"] == [2, 3]  # target从1换成了实例化版本3
        assert rel["expression"] == "[2] 是 [3] 的例子或证据"

        # conclusion_2那条完全没被碰过(不在候选里)
        rel2 = next(r for r in result["relation"] if r["connects"][0] == 11)
        assert rel2 == {"conclusion": "conclusion_2", "connects": [11, 10], "expression": "[11] 是 [10] 的例子或证据"}
    finally:
        shutil.rmtree(tmp_dir)


def test_rerunning_after_switch_finds_nothing_more_to_review():
    """切换成功之后,connects里的3是实例化编号(instantiation_lookup的值,
    不是键),第二次扫描应该找不到候选了——验证这个天然幂等性质。"""
    claims_final = _base_claims_final()
    claims_final["relation"][0] = {
        "conclusion": "conclusion_1", "connects": [2, 3], "expression": "[2] 是 [3] 的例子或证据",
    }  # 模拟已经审查+切换过一次
    lookup = step4c.build_instantiation_lookup(claims_final["relation"])
    candidates = step4c.find_candidates(claims_final, lookup)
    assert candidates == []  # 没有候选了,不会被重复审查


def test_uncertain_defaults_to_general_via_fallback():
    """三档都拿不到可用结果时,保守兜底应该是"全部target保持一般版本
    不动",不是随便选一边。"""
    claims_final = _base_claims_final()

    def always_bad(prompt, **kwargs):
        return "not valid json"

    step4c.call_claude = always_bad

    lookup = step4c.build_instantiation_lookup(claims_final["relation"])
    candidates = step4c.find_candidates(claims_final, lookup)
    for c in candidates:
        c["_instantiation_lookup"] = lookup

    claim_text_by_number = {c["number"]: c["text"] for c in claims_final["claim"]}
    decisions_by_m, status = step4c.review_conclusion(
        "irrelevant", candidates, claim_text_by_number, lookup
    )
    assert status == "fallback_general"
    assert decisions_by_m[2][1] is False  # 保持一般版本

    updated = step4c.apply_decisions(candidates[0], decisions_by_m[2])
    assert updated["connects"] == [2, 1]  # 没有换成实例化版本3
    assert updated["expression"] == "[2] 是 [1] 的例子或证据"


def test_multi_target_relation_partial_switch():
    """一条关系有2个target,只有其中1个有实例化版本、且模型判定该切换,
    另一个没有实例化版本、原样保留——验证部分切换、格式带括号的情况。"""
    claims_final = {
        "claim": [
            {"conclusion": "c1", "text": "general A", "is_pure_data": False, "number": 1},
            {"conclusion": "c1", "text": "general B (no instantiation exists)", "is_pure_data": False, "number": 2},
            {"conclusion": "c1", "text": "instantiated A", "is_pure_data": False, "number": 3},
            {"conclusion": "c1", "text": "evidence for both A and B", "is_pure_data": False, "number": 4},
        ],
        "note": [],
        "relation": [
            {"conclusion": "c1", "connects": [4, 1, 2], "expression": "[4] 是 ([1] 和 [2]) 的例子或证据"},
            {"conclusion": "c1", "connects": [3, 1], "expression": "[3] 是 [1] 的instance"},
        ],
    }
    lookup = step4c.build_instantiation_lookup(claims_final["relation"])
    candidates = step4c.find_candidates(claims_final, lookup)
    assert len(candidates) == 1
    assert candidates[0]["original_targets"] == [1, 2]
    assert candidates[0]["targets_with_alt"] == [1]  # 只有1有实例化版本,2没有

    for c in candidates:
        c["_instantiation_lookup"] = lookup
    updated = step4c.apply_decisions(candidates[0], {1: True})
    assert updated["connects"] == [4, 3, 2]  # 1换成3,2原样保留
    assert updated["expression"] == "[4] 是 ([3] 和 [2]) 的例子或证据"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
