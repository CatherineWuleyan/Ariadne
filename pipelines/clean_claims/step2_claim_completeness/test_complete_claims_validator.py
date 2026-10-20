import sys
sys.path.insert(0, '.')
from complete_claims_validator import validate_complete_claims_response

tests = [
    # (描述, raw_answer, expected_numbers, part_labels, 期望的(result, category_violation))

    ("正常情况(不含引用)",
     '{"results": [{"number": 6, "完整表述": "The Trojan score is approximately zero."}]}',
     {6}, {6: "assertion", 9: "assertion"},
     ({6: {"完整表述": "The Trojan score is approximately zero.", "需要更多上下文": []}}, False)),

    ("引用允许类型(elaboration)",
     '{"results": [{"number": 6, "完整表述": "As 【4】 states, the score is zero."}]}',
     {6}, {6: "assertion", 4: "elaboration"},
     ({6: {"完整表述": "As 【4】 states, the score is zero.", "需要更多上下文": []}}, False)),

    ("引用允许类型,多个编号挤在一个括号里(新格式)",
     '{"results": [{"number": 6, "完整表述": "As 【1, 4】 state, the score is zero."}]}',
     {6}, {6: "assertion", 1: "elaboration", 4: "elaboration"},
     ({6: {"完整表述": "As 【1, 4】 state, the score is zero.", "需要更多上下文": []}}, False)),

    ("多编号括号里混了一个不允许类型 -> category_violation",
     '{"results": [{"number": 6, "完整表述": "As 【1, 4】 state, the score is zero."}]}',
     {6}, {6: "assertion", 1: "elaboration", 4: "论证"},
     (None, True)),

    ("多编号括号里混了一个不存在的编号(判普通失败,不是category_violation)",
     '{"results": [{"number": 6, "完整表述": "As 【1, 999】 state, the score is zero."}]}',
     {6}, {6: "assertion", 1: "elaboration"},
     (None, False)),

    ("引用不允许类型(论证) -> category_violation",
     '{"results": [{"number": 6, "完整表述": "As 【4】 states, the score is zero."}]}',
     {6}, {6: "assertion", 4: "论证"},
     (None, True)),

    ("引用不允许类型(instance) -> category_violation",
     '{"results": [{"number": 6, "完整表述": "As 【4】 states, the score is zero."}]}',
     {6}, {6: "assertion", 4: "instance"},
     (None, True)),

    ("引用自己(判失败,不算category_violation)",
     '{"results": [{"number": 6, "完整表述": "As 【6】 states, the score is zero."}]}',
     {6}, {6: "assertion"},
     (None, False)),

    ("引用不存在的编号(判失败,不算category_violation)",
     '{"results": [{"number": 6, "完整表述": "As 【999】 states, the score is zero."}]}',
     {6}, {6: "assertion"},
     (None, False)),

    ("用了省略式范围引用【1】-【4】(判失败)",
     '{"results": [{"number": 6, "完整表述": "As 【1】-【4】 state, the score is zero."}]}',
     {6}, {6: "assertion", 1: "assertion", 2: "elaboration", 3: "elaboration", 4: "assertion"},
     (None, False)),

    ("同时有非法编号和category违规(优先判普通失败,不是category_violation)",
     '{"results": [{"number": 6, "完整表述": "As 【999】 and 【4】 state, the score is zero."}]}',
     {6}, {6: "assertion", 4: "论证"},
     (None, False)),

    ("需要更多上下文(合法)",
     '{"results": [{"number": 3, "完整表述": "some text", "需要更多上下文": ["term X"]}]}',
     {3}, {3: "assertion"},
     ({3: {"完整表述": "some text", "需要更多上下文": ["term X"]}}, False)),

    ("需要更多上下文不是列表(判失败)",
     '{"results": [{"number": 3, "完整表述": "some text", "需要更多上下文": "term X"}]}',
     {3}, {3: "assertion"},
     (None, False)),

    ("包在markdown代码块里", '```json\n{"results": [{"number": 1, "完整表述": "Some text."}]}\n```',
     {1}, {1: "assertion"},
     ({1: {"完整表述": "Some text.", "需要更多上下文": []}}, False)),

    ("number是【】包裹的字符串", '{"results": [{"number": "【6】", "完整表述": "text"}]}',
     {6}, {6: "assertion"},
     ({6: {"完整表述": "text", "需要更多上下文": []}}, False)),

    ("raw_answer是None(被跳过)", None, {1}, {1: "assertion"}, (None, False)),

    ("完全不是JSON", "抱歉我不知道", {1}, {1: "assertion"}, (None, False)),

    ("漏答一个编号", '{"results": [{"number": 6, "完整表述": "text"}]}', {6, 9},
     {6: "assertion", 9: "assertion"}, (None, False)),

    ("多答一个不存在的编号", '{"results": [{"number": 6, "完整表述": "a"}, {"number": 999, "完整表述": "b"}]}',
     {6}, {6: "assertion"}, (None, False)),

    ("同一个编号重复出现", '{"results": [{"number": 6, "完整表述": "a"}, {"number": 6, "完整表述": "b"}]}',
     {6}, {6: "assertion"}, (None, False)),

    ("完整表述是空字符串", '{"results": [{"number": 6, "完整表述": "   "}]}', {6}, {6: "assertion"}, (None, False)),

    ("完整表述字段缺失", '{"results": [{"number": 6}]}', {6}, {6: "assertion"}, (None, False)),

    ("results是空列表", '{"results": []}', {6}, {6: "assertion"}, (None, False)),

    ("顶层不是dict", '[{"number": 6, "完整表述": "a"}]', {6}, {6: "assertion"}, (None, False)),
]

all_ok = True
for desc, raw, expected_numbers, part_labels, expected in tests:
    got = validate_complete_claims_response(raw, expected_numbers, part_labels)
    ok = got == expected
    all_ok = all_ok and ok
    status = 'OK  ' if ok else 'FAIL'
    print(f"{status} {desc}: 得到 {got!r}" + ("" if ok else f" (期望 {expected!r})"))

print()
print('ALL OK' if all_ok else 'SOME FAILED')
