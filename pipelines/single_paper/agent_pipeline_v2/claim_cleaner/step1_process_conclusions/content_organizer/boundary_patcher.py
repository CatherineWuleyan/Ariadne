"""
boundary_patcher.py

对一个conclusion的原始 segments 列表(label_validator矫正后、按原文顺序排列
的片段,尚未按assertion编号合并),依次做四种"接缝修复":

  1. 转义符修复:如果一个片段结尾是个"未被转义"的反斜杠,把它挪到下一个
     片段开头,并参照原始content决定挪过去之后要不要在其后补一个空格。
  2. 公式修复:如果两个相邻片段的接缝点落在一个未闭合的公式内部,就在
     前一个片段末尾补上闭合符号、后一个片段开头补上开启符号。
  3. 引号修复:同上,针对英语直引号 "。
  4. 括号修复:同上,针对 ( ) 和 （ ）(统一当同一种括号,处理嵌套)。

这四步全部基于"原始 segments 的顺序"操作,发生在assertion按编号合并之前。
"""

from formula_scanner import scan_formulas, is_escaped, count_preceding_backslashes
from quote_paren_scanner import scan_quotes, scan_parens
from position_alignment import build_concat_to_orig_map


def _rebuild_offsets(texts):
    offsets = []
    pos = 0
    for t in texts:
        offsets.append((pos, pos + len(t)))
        pos += len(t)
    return offsets


def fix_escapes(texts, original_content):
    """
    texts: list[str],按原文顺序排列的片段文本(会返回一份新的list,不修改原输入)。
    """
    texts = list(texts)
    n = len(texts)

    for i in range(n - 1):
        if not texts[i]:
            continue
        concat_so_far = ''.join(texts[:i + 1])
        last_pos = len(concat_so_far) - 1
        if concat_so_far[last_pos] != '\\':
            continue
        if is_escaped(concat_so_far, last_pos):
            # 这个反斜杠本身是被前面的反斜杠转义掉的,不是一个"活的"转义符,不用挪
            continue

        # 这是一个"活的"转义符,挪到下一个片段开头
        texts[i] = texts[i][:-1]

        # 参照原始content决定挪过去之后要不要加空格:
        # 用挪之前的完整拼接文本建立位置映射,找到这个反斜杠在原文里的位置,
        # 再看原文里它后面到下一个非空白字符之间有没有空白
        concat_before_move = ''.join(texts[:i] + [texts[i] + '\\'])  # 恢复出"挪之前"的样子用于对齐
        m = build_concat_to_orig_map(original_content, concat_before_move)
        backslash_pos_in_concat = len(concat_before_move) - 1
        orig_pos_of_backslash = m[backslash_pos_in_concat]

        j = orig_pos_of_backslash + 1
        while j < len(original_content) and original_content[j].isspace():
            j += 1
        need_space = j > orig_pos_of_backslash + 1

        already_has_leading_space = bool(texts[i + 1]) and texts[i + 1][0].isspace()
        prefix = '\\'
        if need_space and not already_has_leading_space:
            prefix += ' '
        texts[i + 1] = prefix + texts[i + 1]

    return texts


def _enclosing_spans_needing_patch(pos, spans):
    """
    返回接缝点pos落在其"内容区间"里的所有span(从外到内排序):只要pos
    严格大于定界符开始的位置、且不超过定界符结束的位置,就算需要打补丁——
    在每一处接缝上无差别地补上对应的一对符号(哪怕接缝恰好紧贴着定界符
    本身),多补出来的冗余符号交给后面的收尾步骤(空壳删除)清理。
    """
    enclosing = [sp for sp in spans if sp['start'] < pos <= sp['end']]
    enclosing.sort(key=lambda sp: sp['start'])
    return enclosing


def fix_formulas(texts):
    """
    在原始片段之间的接缝处,修复被截断的公式(单$/双$)。
    返回 (texts, end_added, start_added):后两个是跟texts等长的整数列表,
    分别记录每个片段结尾/开头,被这一步补上了多少个字符(供后续判断
    "这个定界符是不是打补丁加上去的、还是原文本来就有的"用)。
    """
    texts = list(texts)
    n = len(texts)
    end_added = [0] * n
    start_added = [0] * n

    changed = True
    while changed:
        changed = False
        concat = ''.join(texts)
        raw_spans = scan_formulas(concat)
        spans = [{'start': sp['start'], 'end': sp['end'] - 1, 'type': sp['type']} for sp in raw_spans]
        offsets = _rebuild_offsets(texts)

        for i in range(n - 1):
            boundary = offsets[i][1]
            enclosing = _enclosing_spans_needing_patch(boundary, spans)
            if not enclosing:
                continue
            sp = enclosing[0]
            delim = '$' if sp['type'] == 'single' else '$$'
            texts[i] = texts[i] + delim
            texts[i + 1] = delim + texts[i + 1]
            end_added[i] += len(delim)
            start_added[i + 1] += len(delim)
            changed = True
            break

    return texts, end_added, start_added


def fix_quotes(texts):
    """在原始片段之间的接缝处,修复被截断的英语直引号。返回同fix_formulas。"""
    texts = list(texts)
    n = len(texts)
    end_added = [0] * n
    start_added = [0] * n

    changed = True
    while changed:
        changed = False
        concat = ''.join(texts)
        pairs, _dangling = scan_quotes(concat)
        offsets = _rebuild_offsets(texts)

        for i in range(n - 1):
            boundary = offsets[i][1]
            enclosing = _enclosing_spans_needing_patch(boundary, pairs)
            if not enclosing:
                continue
            texts[i] = texts[i] + '"'
            texts[i + 1] = '"' + texts[i + 1]
            end_added[i] += 1
            start_added[i + 1] += 1
            changed = True
            break

    return texts, end_added, start_added


_MATCHING_CLOSE = {'(': ')', '（': '）'}
_MATCHING_OPEN = {')': '(', '）': '（'}


def fix_parens(texts):
    """
    在原始片段之间的接缝处,修复被截断的括号(()和（）统一处理,支持嵌套)。
    返回同fix_formulas。
    """
    texts = list(texts)
    n = len(texts)
    end_added = [0] * n
    start_added = [0] * n

    changed = True
    while changed:
        changed = False
        concat = ''.join(texts)
        pairs, _unclosed = scan_parens(concat)
        offsets = _rebuild_offsets(texts)

        for i in range(n - 1):
            boundary = offsets[i][1]
            enclosing = _enclosing_spans_needing_patch(boundary, pairs)
            if not enclosing:
                continue

            closers = []
            openers = []
            for sp in enclosing:
                open_char = concat[sp['start']]
                close_char = _MATCHING_CLOSE[open_char]
                openers.append(open_char)
                closers.append(close_char)
            closers.reverse()

            texts[i] = texts[i] + ''.join(closers)
            texts[i + 1] = ''.join(openers) + texts[i + 1]
            end_added[i] += len(closers)
            start_added[i + 1] += len(openers)
            changed = True
            break

    return texts, end_added, start_added


def patch_all_boundaries(segments, original_content):
    """
    segments: list[{'text':..., 'label':...}],按原文顺序。
    返回 (patched_segments, end_added, start_added):
      patched_segments: 同样的label,text已经过四步接缝修复
      end_added / start_added: 跟segments等长的整数列表,记录每个片段
        结尾/开头分别被(公式+引号+括号三步合计)打了多少个补丁字符——
        供后续判断"这个定界符是打补丁加上去的、还是原文本来就有的"用。
    """
    texts = [s['text'] for s in segments]
    texts = fix_escapes(texts, original_content)

    texts, e1, s1 = fix_formulas(texts)
    texts, e2, s2 = fix_quotes(texts)
    texts, e3, s3 = fix_parens(texts)

    n = len(texts)
    end_added = [e1[i] + e2[i] + e3[i] for i in range(n)]
    start_added = [s1[i] + s2[i] + s3[i] for i in range(n)]

    patched_segments = [
        {**seg, 'text': new_text}
        for seg, new_text in zip(segments, texts)
    ]
    return patched_segments, end_added, start_added
