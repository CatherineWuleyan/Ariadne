"""Self-contained extraction regression tests."""

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
