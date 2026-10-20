"""
test_step3b_classify_claim_instances.py

用合成用例测每条分支(全部target都contained->certain / 部分overlap->
uncertain / 完全不沾边->None / not_found的target被跳过 / 全部target都
not_found的instance整体跳过 / 多段source_spans的claim逐段比较),再用
867752822639165809的真实数据做一次端到端验证。

用法:
    python test_step3b_classify_claim_instances.py
"""

import json

import step3b_classify_claim_instances as step3b


def _make_target(certainty, start=None, end=None, candidates=None):
    return {
        "term": "t", "certainty": certainty,
        "content": None, "start": start, "end": end,
        "candidates": candidates or [],
    }


def _make_instance(number, targets):
    return {
        "conclusion": "c", "instance_number": number,
        "instance_content": f"instance-{number}",
        "instance_span": {"start": 0, "end": 1},
        "targets": targets,
    }


def test_all_certain_targets_contained_is_certain():
    inst = _make_instance(1, [_make_target("certain", 10, 20)])
    assert step3b.classify_instance_for_claim(inst, [(0, 100)]) == "certain"


def test_target_outside_claim_is_none():
    inst = _make_instance(1, [_make_target("certain", 200, 210)])
    assert step3b.classify_instance_for_claim(inst, [(0, 100)]) is None


def test_uncertain_target_all_candidates_contained_is_certain():
    # uncertain的target有2个候选,只要两个都在claim span里,整体还是certain
    inst = _make_instance(1, [_make_target(
        "uncertain", candidates=[{"start": 10, "end": 20}, {"start": 30, "end": 40}]
    )])
    assert step3b.classify_instance_for_claim(inst, [(0, 100)]) == "certain"


def test_uncertain_target_partial_overlap_is_uncertain():
    # uncertain的target有2个候选,一个在claim span里,一个在外面
    # -> 不满足"全部contained"这条certain的线,但并集跟claim有交集
    # -> uncertain
    inst = _make_instance(1, [_make_target(
        "uncertain", candidates=[{"start": 10, "end": 20}, {"start": 200, "end": 210}]
    )])
    assert step3b.classify_instance_for_claim(inst, [(0, 100)]) == "uncertain"


def test_not_found_target_alone_does_not_block_others():
    # 一个target是not_found,另一个target是certain且被包含
    # -> not_found的target不参与判断,整体还是certain
    inst = _make_instance(1, [
        _make_target("not_found"),
        _make_target("certain", 10, 20),
    ])
    assert step3b.classify_instance_for_claim(inst, [(0, 100)]) == "certain"


def test_all_not_found_instance_is_skipped():
    # 全部target都是not_found -> 跟任何claim都不沾边,返回None
    inst = _make_instance(1, [_make_target("not_found"), _make_target("not_found")])
    assert step3b.classify_instance_for_claim(inst, [(0, 100)]) is None


def test_multi_piece_claim_checks_each_piece_separately():
    # claim因为被打断有2段source_spans:[(0,10), (50,60)]。
    # target在(52,58)里,应该命中第二段,contained。
    inst = _make_instance(1, [_make_target("certain", 52, 58)])
    assert step3b.classify_instance_for_claim(inst, [(0, 10), (50, 60)]) == "certain"

    # target横跨(8,55),既不完全在第一段也不完全在第二段,但跟第二段
    # (50,60)有重叠 -> uncertain(不是靠"拼起来当一个连续区间"算出来的)
    inst2 = _make_instance(2, [_make_target("certain", 8, 55)])
    assert step3b.classify_instance_for_claim(inst2, [(0, 10), (50, 60)]) == "uncertain"


def test_boundary_touching_counts_as_contained_not_just_overlap():
    # target跟claim span正好首尾相接([90,100)在claim(0,100)内),边界
    # 重合也算contained。
    inst = _make_instance(1, [_make_target("certain", 90, 100)])
    assert step3b.classify_instance_for_claim(inst, [(0, 100)]) == "certain"


def test_real_data_conclusion_5():
    """867752822639165809/conclusion_5的真实数据:instance[8]的target在
    (688,714),claim[6]的span是(670,715) -> 应该是certain,且不该出现在
    其它claim(比如claim[13] span=(1024,1063))的确定性/不确定性列表里。
    instance[14]的target在(1053,1062),claim[13]的span是(1024,1063)
    -> 应该是certain。
    """
    paper_dir = step3b.DATA_DIR / "867752822639165809"
    data = step3b.load_claims_and_instances(paper_dir)
    info = data["conclusion_5"]

    inst8 = next(i for i in info["instances"] if i["instance_number"] == 8)
    inst14 = next(i for i in info["instances"] if i["instance_number"] == 14)

    claim6 = next(c for c in info["claims"] if c["number"] == 6)
    claim13 = next(c for c in info["claims"] if c["number"] == 13)

    assert step3b.classify_instance_for_claim(inst8, claim6["pieces"]) == "certain"
    assert step3b.classify_instance_for_claim(inst8, claim13["pieces"]) is None

    assert step3b.classify_instance_for_claim(inst14, claim13["pieces"]) == "certain"
    assert step3b.classify_instance_for_claim(inst14, claim6["pieces"]) is None

    # 跑一遍完整的process_paper,确认claim[6]和claim[13]的结果里正确带上了
    # 对应的instance,而且是放进了certain_instances而不是uncertain_instances
    results = step3b.process_paper("867752822639165809")
    r6 = next(r for r in results if r["conclusion"] == "conclusion_5" and r["claim_number"] == 6)
    r13 = next(r for r in results if r["conclusion"] == "conclusion_5" and r["claim_number"] == 13)

    assert r6["certain_instances"] == [8]
    assert r6["uncertain_instances"] == []
    assert r13["certain_instances"] == [14]
    assert r13["uncertain_instances"] == []


def test_every_claim_gets_an_entry_even_with_empty_lists():
    """867752822639165809/conclusion_5里没有instance关联的claim(比如
    claim[1])也应该出现在结果里,两个列表都是空的,不是被跳过不输出。"""
    results = step3b.process_paper("867752822639165809")
    r1 = next(r for r in results if r["conclusion"] == "conclusion_5" and r["claim_number"] == 1)
    assert r1["certain_instances"] == []
    assert r1["uncertain_instances"] == []


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
