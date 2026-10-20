"""
pure_data_validator.py

对 step2a_check_pure_data.py 里"是否为纯实验数据"判断结果的校验+矫正。

对外只暴露一个函数:

    validate_pure_data_response(raw_answer, expected_numbers) -> dict | None

- raw_answer: API 原始返回的文本(可能是 None,代表这次调用被跳过)
- expected_numbers: 这次 prompt 里实际列出的 claim 编号集合(iterable of int)
- 返回值:
    - 校验通过 -> 返回 {number(int): 是纯实验数据(bool), ...} 的 dict
    - 只要有任何一步没通过,返回 None

============================== 提取策略(有意放得很宽) ==============================
不要求整段文本被切分成"每一条都严格是'数字+布尔词'这个形状",对模型输出
格式的容错更宽:

1. 数字提取:在整段raw_answer里找所有连续数字串(正则\\d+),每一段连续数字
   算一个"编号"出现;数字之间隔了任何字符(哪怕只有一个逗号、一个空格),
   就算两个独立的编号。不要求这些数字前后有什么特定的包装。

2. 每个编号对应的"真值字符段",是这个编号后面到下一个编号开始之间的那段
   文字(最后一个编号则是到整个文本结尾);对这段文字(忽略大小写)依次做:
     a. 先看这段里有没有单个字母't'、有没有单个字母'f':
        - 有t没f -> 判true
        - 有f没t -> 判false
        - 都没有 -> 这个编号的真值判不出来,整体校验失败
     b. 如果t和f都出现了,就退一步用"true"/"false"这两个完整单词做精确
        匹配(依然忽略大小写):
        - 只出现"true"这个词,没出现"false" -> 判true
        - 只出现"false"这个词,没出现"true" -> 判false
        - 两个词都出现了,或者两个词都没出现(只是字母t/f凑巧都在)
          -> 判不出来,整体校验失败

3. 同一个编号出现了多次:如果每次判出的真值一致,不算问题(宽松放过);
   如果两次判出的真值不一样,才算真正冲突,校验失败。

============================== 不放松的部分:claim编号匹配 ==============================
提取宽松,不代表最终结果可以宽松:最后必须核对——提取出的编号集合是否跟
expected_numbers(这次prompt里实际列出的claim编号)完全一致,一个不多、
一个不少。这一步不因为上面放宽了提取方式而放宽:就算文本里夹杂了任何跟
claim无关的数字或说明性文字,只要最终编号集合对不上,照样判失败(混进来的
无关数字自己也会被当成一个"编号"参与提取,但会在这一步被识别为不在
expected_numbers里,从而让整体校验失败)。

============================== 相比上一版少支持的地方(有意放弃,不是遗漏) ==============================
上一版还接受中文"是/否/对/不对"、以及"yes/no/y/n"这些同义写法;这一版
严格按"单字母t/f,撞了才用true/false整词精确匹配"这一套逻辑,不再识别
这些其它写法——prompt本身要求的就是"<true or false>"这两个英文词,如果
Haiku真的偏离这两个词去回答(比如答"是"/"yes"),会在"都没有t也没有f"
这一步判不出来,校验失败,交给上层重试。
"""

import re


_NUMBER_RE = re.compile(r"\d+")


def _judge_bool(segment: str):
    """segment是某个编号对应的真值字符段(这个编号的数字之后、下一个编号
    数字之前的原始切片)。返回True/False/None(None表示判不出来)。"""
    seg = segment.lower()
    has_t = "t" in seg
    has_f = "f" in seg

    if has_t and not has_f:
        return True
    if has_f and not has_t:
        return False
    if not has_t and not has_f:
        return None

    # t和f都出现了,退一步用完整单词精确匹配
    has_true_word = "true" in seg
    has_false_word = "false" in seg
    if has_true_word and not has_false_word:
        return True
    if has_false_word and not has_true_word:
        return False
    return None  # 两个词都出现,或者都没出现(只是字母凑巧撞了),判不出来


def validate_pure_data_response(raw_answer, expected_numbers):
    if raw_answer is None:
        return None

    matches = list(_NUMBER_RE.finditer(raw_answer))
    if not matches:
        return None

    output = {}
    for i, m in enumerate(matches):
        number = int(m.group())
        seg_start = m.end()
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_answer)
        value = _judge_bool(raw_answer[seg_start:seg_end])

        if value is None:
            return None

        if number in output and output[number] != value:
            return None  # 同一个编号被判出了矛盾的两次结果

        output[number] = value

    if set(output.keys()) != set(expected_numbers):
        return None  # 跟这次prompt里实际列出的编号集合对不上(漏答/多答)

    return output
