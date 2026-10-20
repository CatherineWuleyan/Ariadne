"""
final_cleanup.py

对 claim_organizer 产出的 final_parts 做最后两处收尾调整(都只处理
公式外的引号/括号,不处理公式内的):

  8a. 针对claim(assertion/论据/example,合并前可能是多个旧片段):检查相邻
      两个旧片段的接缝处——如果原文里这两个片段之间跳过的内容,整体正好
      包在同一个公式/双引号/括号里,而且这两个片段各自的边缘恰好都是
      boundary_patcher 打补丁加上去的字符(不是原文本来就有的),那这一对
      补丁字符就是冗余的,原样去掉。这一步专门解决"打补丁+合并跳过中间
      内容"导致的碎片化问题,比如 assertion:"(foo" elaboration:"bar"
      assertion:"baz)" 这种,如果不做这一步,merge之后会变成"(foo) (baz)"
      这种被拆碎的错误结果,而不是期望的"foo baz"(elaboration自己已经在
      boundary_patcher那一步被补成了自洽的"(bar)",不需要这里再处理)。

  8b. 扫描所有part(含刚处理完8a的claim),把内容只剩空白的"空公式"/
      "空括号"/"空引号"整个删掉(用状态机正确判断里外,支持括号嵌套)。
"""

import re

from formula_scanner import scan_formulas
from quote_paren_scanner import scan_quotes, scan_parens
from position_alignment import build_concat_to_orig_map


def _rebuild_offsets(texts):
    offsets = []
    pos = 0
    for t in texts:
        offsets.append((pos, pos + len(t)))
        pos += len(t)
    return offsets


def _collapse_whitespace(s: str) -> str:
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def _formula_spans_inclusive(text):
    raw = scan_formulas(text)
    return [{'start': sp['start'], 'end': sp['end'] - 1, 'type': sp['type']} for sp in raw]


def _gap_fully_inside_some_span(gap_start, gap_end, spans):
    """gap是半开区间[gap_start, gap_end)。检查是否存在一个span,其内容区间
    (定界符之间,不含定界符本身)完整覆盖这个gap。"""
    if gap_start >= gap_end:
        return False
    for sp in spans:
        if sp['start'] < gap_start and gap_end <= sp['end']:
            return True
    return False


def cleanup_assertion_seams(final_parts, original_segments, original_content):
    """
    对 final_parts 里带 'pieces'/'piece_orig_indices'/'piece_end_added'/
    'piece_start_added' 的条目(即 assertion/论据/example 这三类claim):
      1. 先做8a——检查每对相邻旧片段的接缝,如果gap被完整包在同一个wrapper里,
         且两侧恰好都是打补丁加上去的字符,就去掉这对冗余补丁。
      2. 把(可能已经去掉了冗余补丁的)各旧片段用单空格拼接起来,统一去空格,
         得到这个claim最终的content。拼接过程只消费掉四个piece追踪字段,
         其余字段(比如论据的supports、example的illustrates)原样保留,
         不能因为重新拼了一次dict就把它们弄丢。
    其它类型条目原样保留(已经在 claim_organizer 里去过空格了)。
    """
    raw_texts = [s['text'] for s in original_segments]
    raw_offsets = _rebuild_offsets(raw_texts)
    pos_map = build_concat_to_orig_map(original_content, ''.join(raw_texts))

    formula_spans = _formula_spans_inclusive(original_content)
    quote_spans, _ = scan_quotes(original_content)
    paren_spans, _ = scan_parens(original_content)

    _PIECE_TRACKING_KEYS = ('pieces', 'piece_orig_indices', 'piece_end_added', 'piece_start_added')

    result = []
    for part in final_parts:
        if 'pieces' not in part:
            result.append(part)
            continue

        pieces = list(part['pieces'])
        orig_indices = part['piece_orig_indices']
        end_added = list(part['piece_end_added'])
        start_added = list(part['piece_start_added'])

        for k in range(len(pieces) - 1):
            idx_a = orig_indices[k]
            idx_b = orig_indices[k + 1]
            gap_start = pos_map[raw_offsets[idx_a][1]]
            gap_end = pos_map[raw_offsets[idx_b][0]]

            is_wrapped = (
                _gap_fully_inside_some_span(gap_start, gap_end, formula_spans)
                or _gap_fully_inside_some_span(gap_start, gap_end, quote_spans)
                or _gap_fully_inside_some_span(gap_start, gap_end, paren_spans)
            )

            if is_wrapped and end_added[k] > 0 and start_added[k + 1] > 0:
                n_end = end_added[k]
                n_start = start_added[k + 1]
                pieces[k] = pieces[k][:len(pieces[k]) - n_end]
                pieces[k + 1] = pieces[k + 1][n_start:]
                # 这两处补丁已经被消耗掉了,清零避免影响到相邻的下一对接缝判断
                end_added[k] = 0
                start_added[k + 1] = 0

        merged = _collapse_whitespace(' '.join(pieces))
        extra_fields = {
            k: v for k, v in part.items()
            if k not in _PIECE_TRACKING_KEYS and k not in ('number', 'label')
        }
        result.append({'number': part['number'], 'label': part['label'], 'content': merged, **extra_fields})

    return result


# ---------------- 8b: 空公式/空括号/空引号删除 ----------------

def remove_empty_wrappers(text: str) -> str:
    """
    反复扫描并删除"内容只剩空白"的公式/引号/括号包裹,直到没有可删的为止。
    用状态机(formula_scanner/quote_paren_scanner)正确判断里外,支持括号
    嵌套(从最内层开始删,外层空壳留到下一轮循环处理)。
    """
    changed = True
    while changed:
        changed = False

        for sp in _formula_spans_inclusive(text):
            inner = text[sp['start'] + 1: sp['end']]
            if inner.strip() == '':
                text = text[:sp['start']] + text[sp['end'] + 1:]
                changed = True
                break
        if changed:
            continue

        pairs, _ = scan_quotes(text)
        for sp in pairs:
            inner = text[sp['start'] + 1: sp['end']]
            if inner.strip() == '':
                text = text[:sp['start']] + text[sp['end'] + 1:]
                changed = True
                break
        if changed:
            continue

        pairs, _ = scan_parens(text)
        pairs.sort(key=lambda sp: -sp['start'])  # 从内到外,先删最内层的空括号
        for sp in pairs:
            inner = text[sp['start'] + 1: sp['end']]
            if inner.strip() == '':
                text = text[:sp['start']] + text[sp['end'] + 1:]
                changed = True
                break

    return _collapse_whitespace(text)
