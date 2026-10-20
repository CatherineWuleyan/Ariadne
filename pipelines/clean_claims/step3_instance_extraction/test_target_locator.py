"""
test_target_locator.py

真实的5篇论文数据里,所有instance的of_terms都只有1项,而且第一层(最松)
匹配就已经唯一——四层递进收紧、uncertain候选列表、span裁剪这几条分支
在真实数据上都没被走到过。这个文件用构造出来的小例子,把每条分支单独
测一遍,确保代码逻辑本身是对的,不依赖"正好在真实数据里出现过"这件事。

用法:
    python test_target_locator.py
不依赖pytest,纯assert,跑完不报错就是全部通过。
"""

from target_locator import locate_target, _trim_span


def test_certain_at_L1_single_match():
    # 最简单的情况:最松的一层就已经唯一。
    content = "we evaluate several architectures. (ResNets or VGGs)"
    instance_start = content.index("(ResNets or VGGs)")
    instance_end = instance_start + len("(ResNets or VGGs)")

    result = locate_target(content, "architectures", instance_start, instance_end)
    assert result["certainty"] == "certain"
    assert result["content"] == "architectures"
    assert result["candidates"] == []


def test_narrows_from_L1_to_L2_via_punctuation():
    # "Cats Dogs" 和 "Cats: Dogs" 在L1(标点也被忽略)下都归一化成同一个
    # "catsdogs",L1能找到2处;但term自己写的是不带标点的"cats dogs",
    # 到L2(标点必须精确对上,只忽略大小写和空格)时,"Cats: Dogs"里的冒号
    # 保留下来跟term对不上,只剩"Cats Dogs"这一处匹配,收紧到1 -> certain,
    # 而且应该用L2选出的这个位置,不是L1那个"看起来有2个"的模糊状态。
    content = "Cats Dogs are cool. Cats: Dogs are neat too. (X, Y)."
    instance_start = content.index("(X, Y)")
    instance_end = instance_start + len("(X, Y)")

    result = locate_target(content, "cats dogs", instance_start, instance_end)
    assert result["certainty"] == "certain"
    assert result["content"] == "Cats Dogs"
    assert result["start"] == content.index("Cats Dogs")
    assert result["candidates"] == []


def test_uncertain_when_next_level_drops_to_zero():
    # 跟上一个例子用同样的文本,但这次term自己写的是带逗号的"cats, dogs"。
    # L1(标点也忽略)下,"Cats Dogs"和"Cats: Dogs"两处都能匹配 -> 2个。
    # L2(标点必须精确对上)下,term自带的逗号在两处都对不上(一处没有标点,
    # 一处是冒号)-> 0个。下一层直接从2变成0,没有任何一层恰好是1
    # -> uncertain,candidates应该是L1那2个位置。
    content = "Cats Dogs are cool. Cats: Dogs are neat too. (X, Y)."
    instance_start = content.index("(X, Y)")
    instance_end = instance_start + len("(X, Y)")

    result = locate_target(content, "cats, dogs", instance_start, instance_end)
    assert result["certainty"] == "uncertain"
    assert result["content"] is None
    assert result["start"] is None
    assert len(result["candidates"]) == 2
    cand_texts = sorted(c["content"] for c in result["candidates"])
    assert cand_texts == ["Cats Dogs", "Cats: Dogs"]


def test_not_found_when_absent_everywhere():
    content = "see the details below: (a widget example)."
    instance_start = content.index("(a widget example)")
    instance_end = instance_start + len("(a widget example)")

    result = locate_target(content, "banana", instance_start, instance_end)
    assert result["certainty"] == "not_found"
    assert result["content"] is None
    assert result["start"] is None
    assert result["candidates"] == []


def test_multiple_targets_independent():
    content = "alpha methods and beta methods are compared (SNIP, GraSP)."
    instance_start = content.index("(SNIP, GraSP)")
    instance_end = instance_start + len("(SNIP, GraSP)")

    r_alpha = locate_target(content, "alpha methods", instance_start, instance_end)
    r_beta = locate_target(content, "beta methods", instance_start, instance_end)

    assert r_alpha["certainty"] == "certain" and r_alpha["content"] == "alpha methods"
    assert r_beta["certainty"] == "certain" and r_beta["content"] == "beta methods"
    assert (r_alpha["start"], r_alpha["end"]) != (r_beta["start"], r_beta["end"])


def test_span_trimmed_when_term_has_leading_space():
    # term自带一个多余的前导空格(" widget"),这在L1/L2(空格本来就被忽略)
    # 下不影响什么,但到L3(大小写忽略、空格必须精确)时,term的这个前导
    # 空格字符要求匹配位置前面必须紧跟一个真实的空白字符。content里
    # "widget"出现两次:一次前面是空格("The widget"),一次前面是左括号
    # ("(widget)")。L1/L2下两处都能匹配(标点在这两层要么被忽略、要么
    # 只是恰好不影响这个不含标点的term)-> 2个,收不紧;到L3,只有前面
    # 是空格的那一处能满足"前导空格"这个要求 -> 收紧到1 -> certain,
    # 而且返回的span应该已经把那个前导空格裁掉,content是干净的"widget"。
    content = (
        "The widget behaves well in tests. "
        "Elsewhere we mention (widget) again. "
        "Finally we show (X, Y)."
    )
    instance_start = content.index("(X, Y)")
    instance_end = instance_start + len("(X, Y)")

    result = locate_target(content, " widget", instance_start, instance_end)
    assert result["certainty"] == "certain"
    assert result["content"] == "widget"  # 裁掉了前导空格,不是" widget"
    assert content[result["start"]] != " "
    # 命中的应该是"The widget"那一处,不是"(widget)"那一处
    assert result["start"] == content.index("widget")


def test_trim_span_helper_directly():
    content = "  hello world  "
    s, e = _trim_span(content, 0, len(content))
    assert content[s:e] == "hello world"

    # 已经是干净范围的,裁剪应该是no-op
    s2, e2 = _trim_span(content, 2, 13)
    assert (s2, e2) == (2, 13)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
