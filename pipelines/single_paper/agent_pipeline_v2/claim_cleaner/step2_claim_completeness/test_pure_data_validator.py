import sys
sys.path.insert(0, '.')
from pure_data_validator import validate_pure_data_response

tests = [
    # (描述, raw_answer, expected_numbers, 期望结果)  期望结果None表示应校验失败

    ("正常情况(完整true/false单词)", "6, false; 9, true; 11, false",
     {6, 9, 11}, {6: False, 9: True, 11: False}),

    ("顺序打乱不影响结果", "11, true; 6, false",
     {6, 11}, {6: False, 11: True}),

    ("大小写混用", "6, FALSE; 9, True; 11, tRuE",
     {6, 9, 11}, {6: False, 9: True, 11: True}),

    ("只给单字母t/f也能判(有t没f/有f没t)", "6: t; 9: f",
     {6, 9}, {6: True, 9: False}),

    ("数字前后带方括号/其它包装,不影响提取", "[6], false; [9], true",
     {6, 9}, {6: False, 9: True}),

    ("前面有一句无关的话,只要不含数字就不影响", "Here are my answers: 6, false; 9, true",
     {6, 9}, {6: False, 9: True}),

    ("t和f都出现,但精确匹配能分辨(not false里t来自not)", "6 not false",
     {6}, {6: False}),

    ("t和f都出现,且true/false两个词都出现 -> 判不出来", "6 true or false",
     {6}, None),

    ("t和f都没出现 -> 判不出来", "6 maybe",
     {6}, None),

    ("同一个编号出现两次但真值一致 -> 放过", "6 true, 6 true",
     {6}, {6: True}),

    ("同一个编号出现两次但真值冲突 -> 判失败", "6 true, 6 false",
     {6}, None),

    ("前面的无关文字里混进了数字 -> 编号集合对不上,判失败",
     "there are 12 items total. 6, true; 9, false",
     {6, 9}, None),

    ("漏答一个编号", "6, false", {6, 9}, None),

    ("raw_answer是None(被跳过)", None, {1}, None),

    ("完全没有任何数字", "抱歉我不知道怎么回答", {1}, None),

    ("中文是/否已不再支持,判不出来", "6, 是; 9, 否", {6, 9}, None),

    ("yes/no已不再支持(不含t/f单字母),判不出来", "6, yes; 9, no", {6, 9}, None),

    ("空字符串", "", {6}, None),
]

all_ok = True
for desc, raw, expected_numbers, expected_result in tests:
    got = validate_pure_data_response(raw, expected_numbers)
    ok = got == expected_result
    all_ok = all_ok and ok
    status = 'OK  ' if ok else 'FAIL'
    print(f"{status} {desc}: 得到 {got!r}" + ("" if ok else f" (期望 {expected_result!r})"))

print()
print('ALL OK' if all_ok else 'SOME FAILED')
