"""
test_step4d_normalize_instance_relations.py

不调API,纯本地字符串改写。用合成数据覆盖各种relation类型(instance型/
论据例子型/原生逻辑关系型)确认只改对了该改的那种,再用真实的
867757662605934651数据(已经有真实的"[14] 是 [4] 的instance"关系)做一次
端到端验证。

用法:
    python test_step4d_normalize_instance_relations.py
"""

import json
import shutil
import tempfile
from pathlib import Path

import step4d_normalize_instance_relations as step4d


def test_instance_relation_expression_is_rewritten():
    claims_final = {"relation": [
        {"conclusion": "c1", "connects": [14, 4], "expression": "[14] 是 [4] 的instance"},
    ]}
    n_changed = step4d.normalize_instance_relations(claims_final)
    assert n_changed == 1
    r = claims_final["relation"][0]
    assert r["connects"] == [14, 4]  # connects不变
    assert r["expression"] == "[14] 是 [4] 的例子或证据"
    assert r["conclusion"] == "c1"  # conclusion字段不受影响


def test_non_instance_relations_are_untouched():
    claims_final = {"relation": [
        {"conclusion": "c1", "connects": [1, 2, 3], "expression": "([1] 且 [2]) 推出 [3]"},
        {"conclusion": "c1", "connects": [5, 4], "expression": "[5] 是 [4] 的例子或证据"},
        {"conclusion": "c1", "connects": [7, 1, 2],
         "expression": "[7] 是 ([1] 和 [2]) 的例子或证据"},
    ]}
    before = json.loads(json.dumps(claims_final))  # 深拷贝,用来对比

    n_changed = step4d.normalize_instance_relations(claims_final)

    assert n_changed == 0
    assert claims_final == before  # 一个字段都不该变


def test_mixed_relations_only_instance_ones_change():
    claims_final = {"relation": [
        {"conclusion": "c1", "connects": [1, 2, 3], "expression": "([1] 且 [2]) 推出 [3]"},
        {"conclusion": "c1", "connects": [5, 4], "expression": "[5] 是 [4] 的例子或证据"},
        {"conclusion": "c1", "connects": [14, 4], "expression": "[14] 是 [4] 的instance"},
        {"conclusion": "c2", "connects": [20, 10], "expression": "[20] 是 [10] 的instance"},
    ]}
    n_changed = step4d.normalize_instance_relations(claims_final)

    assert n_changed == 2
    assert claims_final["relation"][0]["expression"] == "([1] 且 [2]) 推出 [3]"  # 逻辑关系不变
    assert claims_final["relation"][1]["expression"] == "[5] 是 [4] 的例子或证据"  # 论据关系不变
    assert claims_final["relation"][2]["expression"] == "[14] 是 [4] 的例子或证据"  # 改了
    assert claims_final["relation"][3]["expression"] == "[20] 是 [10] 的例子或证据"  # 改了


def test_idempotent_second_run_changes_nothing():
    claims_final = {"relation": [
        {"conclusion": "c1", "connects": [14, 4], "expression": "[14] 是 [4] 的instance"},
    ]}
    step4d.normalize_instance_relations(claims_final)  # 第一次
    n_changed_second = step4d.normalize_instance_relations(claims_final)  # 第二次
    assert n_changed_second == 0
    assert claims_final["relation"][0]["expression"] == "[14] 是 [4] 的例子或证据"


def test_real_data_867757662605934651():
    """真实数据:867757662605934651的claims_final.json里已经有一条
    "[14] 是 [4] 的instance"(step4b真实产出的)。"""
    real_path = step4d.DATA_DIR / "867757662605934651" / "claims_final.json"
    claims_final = json.loads(real_path.read_text(encoding="utf-8"))

    instance_relations_before = [
        r for r in claims_final["relation"] if r["expression"].endswith("的instance")
    ]
    assert len(instance_relations_before) >= 1  # 确认真实数据里确实有这种关系

    n_changed = step4d.normalize_instance_relations(claims_final)
    assert n_changed == len(instance_relations_before)

    # 改完之后不应该再有任何"的instance"结尾的关系
    assert not any(r["expression"].endswith("的instance") for r in claims_final["relation"])

    rel14 = next(r for r in claims_final["relation"] if r["connects"][0] == 14)
    assert rel14["expression"] == "[14] 是 [4] 的例子或证据"


def test_cli_end_to_end_with_temp_copy():
    """用临时目录跑一遍main(),不影响真实数据。"""
    from unittest.mock import patch

    real_path = step4d.DATA_DIR / "867757662605934651" / "claims_final.json"
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = tmp_dir / "fake_paper"
        paper_dir.mkdir()
        shutil.copy(real_path, paper_dir / "claims_final.json")

        with patch.object(step4d, "DATA_DIR", tmp_dir), patch("sys.argv", ["prog", "fake_paper"]):
            step4d.main()

        result = json.loads((paper_dir / "claims_final.json").read_text(encoding="utf-8"))
        assert not any(r["expression"].endswith("的instance") for r in result["relation"])
    finally:
        shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
