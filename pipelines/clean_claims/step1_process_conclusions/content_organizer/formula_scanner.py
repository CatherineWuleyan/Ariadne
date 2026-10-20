"""
formula_scanner.py
全文公式范围扫描（逐字符状态机）。不修改、不转换原文内容。
"""
import re


# ---------------------------------------------------------------------------
# 全文公式范围扫描
# ---------------------------------------------------------------------------

def count_preceding_backslashes(text, pos):
    """Count consecutive backslashes immediately before text[pos]."""
    count = 0
    i = pos - 1
    while i >= 0 and text[i] == '\\':
        count += 1
        i -= 1
    return count


def is_escaped(text, pos):
    """Is text[pos] escaped (preceded by an odd number of backslashes)?"""
    return count_preceding_backslashes(text, pos) % 2 == 1


def find_matching_brace(text, open_pos):
    """text[open_pos] must be an unescaped '{'. Return index of the matching
    unescaped '}', honoring nesting and escaping. None if unbalanced (runs to
    end of text)."""
    depth = 0
    i = open_pos
    n = len(text)
    while i < n:
        c = text[i]
        if c == '{' and not is_escaped(text, i):
            depth += 1
        elif c == '}' and not is_escaped(text, i):
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


_WRAPPER_COMMANDS = ('text', 'footnote')


def _try_skip_wrapper_command(text, i, n):
    """If text[i:] starts with an unescaped \\verb, \\verb*, \\text{ or
    \\footnote{, return the index right after the whole construct. Otherwise
    return None."""
    if text[i] != '\\' or is_escaped(text, i):
        return None

    # \verb / \verb*
    if text[i:i + 5] == '\\verb':
        j = i + 5
        if j < n and text[j] == '*':
            j += 1
        if j >= n:
            return j
        delim = text[j]
        j += 1
        end = text.find(delim, j)
        if end == -1:
            return n
        return end + 1

    # \text{...} / \footnote{...}
    for cmdname in _WRAPPER_COMMANDS:
        cmd = '\\' + cmdname
        clen = len(cmd)
        if text[i:i + clen] == cmd and i + clen < n and text[i + clen] == '{':
            brace_start = i + clen
            close = find_matching_brace(text, brace_start)
            if close is None:
                return n
            return close + 1

    return None


def scan_formulas(text):
    """Scan the whole document text and return a list of formula spans.
    Each span: {'start': int, 'end': int, 'type': 'single'|'double'} using
    Python string indices (Unicode code point offsets) into `text`.
    `end` is exclusive (points just past the closing delimiter).
    """
    spans = []
    n = len(text)
    i = 0
    state = 'OUTSIDE'
    current_start = None

    while i < n:
        skip_to = _try_skip_wrapper_command(text, i, n)
        if skip_to is not None:
            i = skip_to
            continue

        c = text[i]
        if c == '$' and not is_escaped(text, i):
            if state == 'OUTSIDE':
                if i + 1 < n and text[i + 1] == '$':
                    state = 'IN_DOUBLE'
                    current_start = i
                    i += 2
                else:
                    state = 'IN_SINGLE'
                    current_start = i
                    i += 1
                continue
            elif state == 'IN_SINGLE':
                spans.append({'start': current_start, 'end': i + 1, 'type': 'single'})
                state = 'OUTSIDE'
                current_start = None
                i += 1
                continue
            else:  # IN_DOUBLE
                if i + 1 < n and text[i + 1] == '$':
                    spans.append({'start': current_start, 'end': i + 2, 'type': 'double'})
                    state = 'OUTSIDE'
                    current_start = None
                    i += 2
                else:
                    i += 1
                continue

        i += 1

    # NOTE: if state != 'OUTSIDE' here, the document had an unterminated
    # formula. We do not silently swallow the rest of the document -- we
    # close it off at EOF so a single stray '$' cannot corrupt everything
    # that follows.
    if state != 'OUTSIDE':
        spans.append({'start': current_start, 'end': n, 'type': 'single' if state == 'IN_SINGLE' else 'double'})

    return spans


class LineIndex:
    """Utility to convert an absolute character offset (into the whole
    document text) to a (line_number, char_offset_within_line) pair, both
    1-indexed for line_number and 0-indexed for the within-line offset,
    using Unicode code point counting throughout."""

    def __init__(self, text):
        self.text = text
        self.line_starts = [0]
        for idx, ch in enumerate(text):
            if ch == '\n':
                self.line_starts.append(idx + 1)

    def offset_to_line_col(self, offset):
        # binary search for the line containing `offset`
        lo, hi = 0, len(self.line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        line_no = lo + 1  # 1-indexed
        col = offset - self.line_starts[lo]
        return line_no, col
