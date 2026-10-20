"""
position_alignment.py

给定一个conclusion的原始 content(未切分)和它的 segments 列表(label_validator
矫正后的、按原文顺序排列的片段),在"segments依次拼接起来的文本"和"真正的原始
content"之间建立位置映射。

之所以需要这个映射:label_validator只保证"拼接结果去空格后等于原文去空格后
的结果",不保证空格的具体分布完全一致。但后续处理(转义符/公式/引号/括号的
边界修复)有些判断需要参照"原始content里空格到底是怎么分布的"(比如转义符
挪位置之后要不要加空格,要看原文),所以需要能从"拼接文本里的某个位置"查到
"原始content里对应的位置"。

核心假设(如果不成立会直接报错,而不是静默出错):
把"拼接文本"和"原始content"各自的空白字符都去掉之后,剩下的非空白字符序列
必须完全一致——这是 label_validator 校验时已经保证过的事情。
"""


def build_concat_to_orig_map(original_content: str, concat_text: str) -> list:
    """
    返回一个长度为 len(concat_text)+1 的列表 m,其中 m[k] 是拼接文本里第 k 个
    字符位置(0-indexed,前闭)在原始content里对应的位置。m[len(concat_text)]
    是拼接文本结尾对应在原始content里的位置。

    对于拼接文本里的空白字符,m[k] 给出的是"当前原始content扫描指针所在位置"
    (不保证是哪个具体空白字符,因为空白的具体分布两边可能不完全一样)。
    """
    n_orig = len(original_content)
    n_concat = len(concat_text)

    concat_to_orig = [0] * (n_concat + 1)
    oi = 0  # 原始content指针
    ci = 0  # 拼接文本指针

    while ci < n_concat:
        cc = concat_text[ci]
        if cc.isspace():
            concat_to_orig[ci] = oi
            ci += 1
            continue

        # cc 是非空白字符,原始content指针先跳过任意数量的空白去找它
        while oi < n_orig and original_content[oi].isspace():
            oi += 1

        if oi >= n_orig or original_content[oi] != cc:
            raise ValueError(
                f"位置对齐失败:拼接文本第{ci}个字符是{cc!r},"
                f"但原始content第{oi}个位置是"
                f"{original_content[oi] if oi < n_orig else '(已到结尾)'!r}。"
                f"这说明segments去空格后的拼接结果跟原文对不上,"
                f"理论上label_validator应该已经拦下过这种情况,不应该发生。"
            )

        concat_to_orig[ci] = oi
        oi += 1
        ci += 1

    # 拼接文本结尾之后,原始content里可能还有一段尾随空白,一并跳过
    while oi < n_orig and original_content[oi].isspace():
        oi += 1
    concat_to_orig[n_concat] = oi

    return concat_to_orig


def has_space_at_orig(original_content: str, orig_pos_before: int, orig_pos_after: int) -> bool:
    """
    original_content[orig_pos_before:orig_pos_after] 这一段(通常很短,是两个
    紧邻token之间的空隙)里有没有空白字符。
    """
    gap = original_content[orig_pos_before:orig_pos_after]
    return any(ch.isspace() for ch in gap)


def compute_original_segment_spans(original_content: str, raw_texts: list) -> list:
    """
    raw_texts: label_validator给出的、按原文顺序排列的原始片段文本列表
    (未经boundary_patcher任何加工)。

    返回一个跟raw_texts等长的列表,每项 {'start': int, 'end': int},是这个
    片段在 original_content 里精确的字符起止位置(前闭后开)。

    分配规则:每个片段的起点 = 上一个片段的终点(第一个片段起点是0,
    不留空档、不重叠);终点 = 下一个片段"第一个非空白字符"在原文里的
    位置(最后一个片段的终点是 len(original_content))——也就是说,两个
    片段之间原文里的空白,全部划给前一个片段的尾巴。
    """
    concat = ''.join(raw_texts)
    pos_map = build_concat_to_orig_map(original_content, concat)

    concat_offsets = []
    pos = 0
    for t in raw_texts:
        concat_offsets.append((pos, pos + len(t)))
        pos += len(t)

    n = len(raw_texts)
    n_orig = len(original_content)
    boundaries = [0] * (n + 1)
    boundaries[0] = 0
    for i in range(1, n):
        concat_boundary = concat_offsets[i - 1][1]
        p = pos_map[concat_boundary]
        while p < n_orig and original_content[p].isspace():
            p += 1
        boundaries[i] = p
    boundaries[n] = n_orig

    return [{'start': boundaries[i], 'end': boundaries[i + 1]} for i in range(n)]
