"""
test_step3b_check_target_containment.py

用构造出来的小例子测 step3b_check_target_containment.py 里的两个核心函数:
build_span_index(展开source_spans)和find_containing_spans(包含关系判断)。

用法:
    python test_step3b_check_target_containment.py
不依赖pytest,纯assert,跑完不报错就是全部通过。
"""

from step3b_check_target_containment import build_span_index, find_containing_spans


def _make_conclusion(parts):
    return {
        "id": "paper:x::conclusion_1",
        "global_id": "g1",
        "order": 1,
        "title": "t",
        "content": "irrelevant for this test",
        "organized_parts": parts,
    }


def test_build_span_index_flattens_multi_span_claim():
    # 一条assertion因为被打断,source_spans里有2段;一条elaboration只有1段。
    conclusion = _make_conclusion([
        {
            "number": 1, "label": "assertion", "content": "A and B",
            "source_spans": [{"start": 0, "end": 5}, {"start": 20, "end": 25}],
        },
        {
            "number": 2, "label": "elaboration", "content": "(explains X)",
            "source_spans": [{"start": 5, "end": 20}],
        },
    ])

    spans = build_span_index(conclusion)
    assert len(spans) == 3  # 2段assertion + 1段elaboration,拆开算3条

    assertion_spans = [s for s in spans if s["label"] == "assertion"]
    assert len(assertion_spans) == 2
    assert all(s["number"] == 1 and s["content"] == "A and B" for s in assertion_spans)
    assert {(s["start"], s["end"]) for s in assertion_spans} == {(0, 5), (20, 25)}

    elab_spans = [s for s in spans if s["label"] == "elaboration"]
    assert len(elab_spans) == 1
    assert elab_spans[0] == {
        "label": "elaboration", "number": 2, "content": "(explains X)",
        "start": 5, "end": 20,
    }


def test_find_containing_spans_exact_containment():
    spans = [
        {"label": "assertion", "number": 1, "content": "c1", "start": 0, "end": 10},
        {"label": "elaboration", "number": 2, "content": "c2", "start": 10, "end": 20},
    ]
    # 完全落在第一段内
    result = find_containing_spans(2, 8, spans)
    assert len(result) == 1
    assert result[0]["label"] == "assertion"

    # 完全落在第二段内
    result = find_containing_spans(12, 18, spans)
    assert len(result) == 1
    assert result[0]["label"] == "elaboration"


def test_find_containing_spans_boundary_crossing_returns_empty():
    # [8, 12) 横跨两段之间的边界(第一段是[0,10),第二段是[10,20)),哪个都
    # 装不下,应该返回空列表——这正是本脚本要抓的"跨part边界"的情况。
    spans = [
        {"label": "assertion", "number": 1, "content": "c1", "start": 0, "end": 10},
        {"label": "elaboration", "number": 2, "content": "c2", "start": 10, "end": 20},
    ]
    result = find_containing_spans(8, 12, spans)
    assert result == []


def test_find_containing_spans_exact_edges_included():
    # 边界情况:位置正好跟span的起止重合(前闭后开),应该算完全包含。
    spans = [{"label": "assertion", "number": 1, "content": "c1", "start": 5, "end": 15}]
    result = find_containing_spans(5, 15, spans)
    assert len(result) == 1


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
