"""Self-contained extraction regression tests."""

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
