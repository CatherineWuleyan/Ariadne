"""
quote_paren_scanner.py

对一段文本扫描"引号"和"括号"的配对范围,规则:
  - 引号只认英语直引号 "(不认单引号,不认中文引号/弯引号)。按出现顺序简单地
    交替当作开/关(第1个是开,第2个是关,第3个是开...)。
  - 括号认 ASCII ( ) 和全角 （ ）,两种字符统一当同一种括号,可以互相闭合
    (比如英文 ( 可以被中文 ） 闭合),需要处理嵌套。
  - 引号和括号都只看"公式外"的部分——先用 formula_scanner.scan_formulas
    算出哪些字符区间属于公式内部,扫描引号/括号的时候跳过这些区间里的字符,
    不把它们当成引号/括号看待。
"""

from formula_scanner import scan_formulas

_OPEN_PARENS = ('(', '（')
_CLOSE_PARENS = (')', '）')


def _build_in_formula_checker(text):
    spans = scan_formulas(text)

    def in_formula(pos):
        for sp in spans:
            if sp['start'] <= pos < sp['end']:
                return True
        return False

    return in_formula, spans


def scan_quotes(text):
    """
    返回引号配对列表,每项 {'start': i, 'end': j},i/j 是那一对 " 各自的位置
    (闭区间,即 text[i] 和 text[j] 都是引号本身)。跳过落在公式内的引号。
    """
    in_formula, _ = _build_in_formula_checker(text)
    pairs = []
    open_pos = None
    for i, ch in enumerate(text):
        if ch != '"' or in_formula(i):
            continue
        if open_pos is None:
            open_pos = i
        else:
            pairs.append({'start': open_pos, 'end': i})
            open_pos = None
    return pairs, open_pos is not None  # 第二个返回值:文本结尾是否还有一个没配对的引号


def scan_parens(text):
    """
    返回括号配对列表(含嵌套的所有层级),每项 {'start': i, 'end': j}。
    跳过落在公式内的括号字符。()和（）统一当同一种括号,用栈处理嵌套。
    """
    in_formula, _ = _build_in_formula_checker(text)
    pairs = []
    stack = []
    for i, ch in enumerate(text):
        if in_formula(i):
            continue
        if ch in _OPEN_PARENS:
            stack.append(i)
        elif ch in _CLOSE_PARENS:
            if stack:
                open_pos = stack.pop()
                pairs.append({'start': open_pos, 'end': i})
            # 如果栈是空的(多余的右括号),忽略,不报错——原文本身偶尔可能有
            # 不严谨的括号使用,不影响我们只是想知道"哪些位置在括号内部"
    return pairs, len(stack)  # 第二个返回值:还有几个没闭合的左括号留在栈里


def position_in_any_span(pos, spans):
    """pos 是否严格落在 spans 列表(每项有 'start'/'end')中某一个区间内部
    (start < pos < end,不含两端本身)。"""
    return any(sp['start'] < pos < sp['end'] for sp in spans)
