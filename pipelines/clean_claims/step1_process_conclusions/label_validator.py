"""
label_validator.py

对 step1a 分段打标签的结果做"校验 + 矫正"(v2 schema:11类标签,见
step1a_prompt_template.txt)。

对外只暴露一个函数:

    validate_and_correct(raw_answer, original_content) -> dict | None

- raw_answer: API 原始返回的文本(可能是 None,代表这次调用被跳过)
- original_content: 这条 conclusion 的原始 content,用于核对 segments 拼接后是否与原文一致
- 返回值:
    - 全部校验通过 -> 返回"矫正后"的 dict,结构是
      {"segments": [{"text":..., "label":..., <该label对应的额外字段>}, ...]}
      label 已被归一化成11个标准类别名之一;额外字段(number/supports/
      illustrates/of/of_terms/connects/expression)按下面"字段规范"里
      该类别允许的字段来输出,数值/列表已经过宽松提取矫正成规整的
      int / list[int] / list[str] / str。
    - 只要有任何一步没通过,返回 None

============================== JSON解析与转义修复 ==============================
raw_answer 不直接 json.loads():模型有时会把整段JSON包在markdown代码块里,
也可能在"text"字段里直接写LaTeX命令(比如\\theta)、忘了按JSON规则把反斜杠
转义成双反斜杠。这里不靠猜测式的局部窗口或字段边界识别去修复,而是先把
"text"字段的内容跟 original_content 做全文逐字符对齐(_align_text_fields):
从第一个字符对到最后一个字符,顺序推进,遇到反斜杠时按"original_content
当前位置实际是什么字符"来判断这个反斜杠该按标准转义解读、还是其实是没
转义的字面反斜杠。这个对齐本身就同时完成了两件事:(1)验证了"所有segment
拼起来跟原文完全一致"这个最基本的要求——对齐失败(不是空白差异、也不是
转义歧义能解释的不一致)直接判校验失败,不再往下做任何字段级校验;
(2)顺带确定了每个"text"字段在原文里的精确边界,不需要另外用启发式方法去
猜字段范围。对齐成功后,把每个"text"字段替换成验证过的正确内容,再拿这些
已经验证过的内容拼成的整体,对JSON里剩余部分(主要是of_terms,同样可能
包含没转义的LaTeX)做同样有证据支持的修复,最后才 json.loads() 拿到完整
结构。详见 _align_text_fields / _escape_remaining_invalid_backslashes /
_parse_with_alignment 的实现和各自的文档字符串。

============================== 11 个类别 ==============================
"assertion"、"论据"、"example" 三者共享同一个编号池(=claim,见 CLAIM_LABELS),
其余八类("论证"/"motivation"/"elaboration"/"instance"/"relation"/"framing"/
"connection"/"other")都不占用编号。

每个类别允许/必须出现的额外字段(text/label 两个基础字段之外):

    label         必须字段                    可选字段         禁止字段(其余全部)
    ----------------------------------------------------------------------
    assertion     number                      -                supports/illustrates/of/of_terms/connects/expression
    论据          number, supports(非空)      -                illustrates/of/of_terms/connects/expression
    论证          supports(非空)              -                number/illustrates/of/of_terms/connects/expression
    motivation    -                           -                (除text/label外都不许有)
    elaboration   -                           of 或 of_terms   number/supports/illustrates/connects/expression
                                               (二选一,不能都有,也可以都不给)
    example       number, illustrates(非空)   -                supports/of/of_terms/connects/expression
    instance      of_terms(非空)              -                number/supports/illustrates/of/connects/expression
    relation      connects(>=2个不同编号),    -                number/supports/illustrates/of/of_terms
                  expression
    framing       -                           -                (除text/label外都不许有)
    connection    -                           -                (除text/label外都不许有)
    other         -                           -                (除text/label外都不许有)

"必须字段"缺失(提取不出有效内容)-> 校验失败。
"禁止字段"如果给了但提取不出任何有效内容(比如给了个空列表、或者塞了一段
一个数字都没有的文字)-> 当成没给,不算违规;只有提取出真实、非空的内容
时才判违规(这样可以宽松处理模型偶尔多打一个空字段的情况,同时仍然能抓出
"论证"被错标成带编号"的真实混淆)。

elaboration 的 of / of_terms 判"同时出现"冲突,同样是在上述"提取干净"之后
才判断——两边都提取出非空内容才算真冲突,只有一边有内容不算。

============================== 校验规则细节 ==============================

1. 数字提取(用于 number/supports/illustrates/of/connects,以及 relation
   的 expression 里的方括号编号):不要求严格的 JSON 类型(int 或 list of
   int),不管原始给的是整数、数字字符串、一段夹杂数字的文字,还是list,统一
   转成字符串后用正则 \\d+ 依次扣出所有连续数字串、逐个 int() 转换(自然完成
   "去掉前导0直到非0或只剩一位"——int("007")==7,int("0")==0)、按首次出现
   顺序去重。两个数字之间只要隔了任何非数字字符(哪怕只是一个逗号或汉字),
   就算两个独立的数;连续数字字符不被任何字符打断,才算同一个多位数
   (\\d+ 本身就是这个语义)。

2. label 归一化匹配:转小写、去掉所有空白字符、去掉所有独立的"the"子串、
   去掉所有字母"s"(不做单词边界保护——这意味着"other"归一化后会变成"or",
   这是已知的、可接受的副作用,因为两侧用的是同一套归一化函数,不影响
   彼此之间的区分度)。归一化后必须恰好匹配11个标准类别之一,否则校验失败。
   这套归一化只用于"识别这是哪个类别",不用于其它任何匹配。

3. 跨segment的编号一致性:
   - 收集所有 assertion/论据/example 的 number,建立 number -> label 映射;
     同一个 number 如果被打上了不同的 label(比如一条 assertion 3、一条
     论据 3 同时出现),判校验失败。
   - 论据.supports / 论证.supports / example.illustrates / elaboration.of
     (如果给了)/ relation.connects 里出现的每个编号,必须能在上面收集到的
     claim 编号集合里找到,否则判校验失败。
   - 禁止自引用:论据的 supports 不能包含自己的 number;example 的
     illustrates 不能包含自己的 number。(论证没有自己的number,elaboration/
     relation 没有自己的 claim 编号,天然不涉及这条检查。)
   - relation 的 expression:只能由 且/和/与/或/非/推出/等价/矛盾 这几个
     连接词、圆括号、方括号、数字、空白字符组成(不做完整逻辑语法解析,只
     做token级校验);圆括号和方括号必须配对平衡;expression 里出现的
     (方括号包着的)编号集合,必须跟 connects 的编号集合完全一致(双向)。

4. of_terms 逐字核实(instance 的 of_terms 必查;elaboration 给了 of_terms
   才查):优先看"把该 segment 自己的 text 排除,其余所有 segment 的 text
   按原文顺序、用单个空格连接拼成的搜索池"(子串匹配,忽略大小写、忽略
   空白差异——搜索池的每一段先各自去掉内部空白再拼接,拼接时插入的单个
   空格是不会被消掉的分隔符,专门防止两个片段的文字意外首尾相连拼出一个
   本不存在的假匹配;忽略以下标点:全半角引号/逗号/句号/分号/冒号/叹号/
   问号/顿号/圆括号/破折号/美元符号,但不忽略方括号、连字符"-"、以及
   \\、^、_、+、=、<、>、%、&、#、@、*、/ 这些数学/技术符号;不额外忽略
   字母s/the,跟第2条的label归一化是两套独立的规则)。这个池子里找不到的
   短语,再看能不能在这个 segment 自己的 text 里找到——能找到就放行,但
   会在矫正后的结果里加一个 "of_terms_self_only" 字段,列出"只在自己这段
   里找到、原文别处没能印证"的那些短语,提醒下游这条依据不是凭空编造,
   但也没有被原文其它地方交叉印证过。只有当某个短语两处(自己的、别处的)
   都找不到时,才真正判定为凭空编造的短语,校验失败。

5. 文本还原校验:所有 segment 的 text 按顺序拼接、去掉空白字符后,必须跟
   original_content 去掉空白字符后完全一致(大小写、标点都必须精确匹配,
   不做任何忽略——这条检查的目的就是确认模型没有改写/遗漏原文,跟上面
   "宽松匹配"的几条是完全不同性质的检查,不能放宽)。这条检查现在是在
   _align_text_fields 的逐字符对齐过程中完成的,不是事后再拿字符串整体
   比较一次;好处是对齐这个动作本身顺带就确定了每个"text"字段在原文里
   的精确边界,不需要另外用启发式方法去猜。
"""

import re
import json


# =============================================================================
# 标签定义
# =============================================================================

ALL_LABELS = (
    "assertion", "论据", "论证", "motivation", "elaboration",
    "example", "instance", "relation", "framing", "connection", "other",
)

# 共享同一个编号池、可以在原文中被打断成多个片段(按 number 合并)的三类。
CLAIM_LABELS = ("assertion", "论据", "example")

# 每个 label 允许出现的额外字段(text/label 之外)。
_ALLOWED_EXTRA_FIELDS = {
    "assertion": {"number"},
    "论据": {"number", "supports"},
    "论证": {"supports"},
    "motivation": set(),
    "elaboration": {"of", "of_terms"},
    "example": {"number", "illustrates"},
    "instance": {"of_terms"},
    "relation": {"connects", "expression"},
    "framing": set(),
    "connection": set(),
    "other": set(),
}

_ALL_EXTRA_FIELDS = ("number", "supports", "illustrates", "of", "of_terms", "connects", "expression")

_FIELD_KIND = {
    "number": "number",
    "supports": "numlist",
    "illustrates": "numlist",
    "of": "numlist",
    "connects": "numlist",
    "of_terms": "termlist",
    "expression": "string",
}


# =============================================================================
# label 归一化与匹配
# =============================================================================

def _normalize_label(s: str) -> str:
    """转小写、去空白、去掉所有独立的"the"子串、去掉所有字母"s"(不做单词
    边界保护)。已知副作用:"other"会被归一化成"or"——可接受,因为两侧用
    同一套函数,不影响区分度。"""
    s = s.lower()
    s = re.sub(r"\s+", "", s)
    s = s.replace("the", "")
    s = s.replace("s", "")
    return s


_NORM_TO_CANONICAL = {_normalize_label(lbl): lbl for lbl in ALL_LABELS}


def _match_label(raw_label):
    """raw_label 归一化后精确匹配某个标准类别,返回标准类别名;匹配不上
    (或者raw_label根本不是字符串)返回 None。"""
    if not isinstance(raw_label, str):
        return None
    return _NORM_TO_CANONICAL.get(_normalize_label(raw_label))


# =============================================================================
# 数字/短语的宽松提取
# =============================================================================

def _extract_numbers(value) -> list:
    """把value转成字符串后用 \\d+ 依次扣出所有连续数字串、int()转换、按
    首次出现顺序去重。value为None时返回空列表。"""
    if value is None:
        return []
    seen = []
    for m in re.findall(r"\d+", str(value)):
        n = int(m)
        if n not in seen:
            seen.append(n)
    return seen


def _get_numbers(seg: dict, key: str) -> list:
    if key not in seg:
        return []
    return _extract_numbers(seg[key])


def _get_number(seg: dict, key: str = "number"):
    nums = _get_numbers(seg, key)
    return nums[0] if nums else None


def _extract_terms(value) -> list:
    """把value规整成字符串列表:裸值(非list)包成单元素列表;每项转字符串、
    去首尾空格、丢弃空字符串、按首次出现顺序去重。"""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    seen = []
    for item in items:
        if item is None:
            continue
        s = str(item).strip()
        if s and s not in seen:
            seen.append(s)
    return seen


def _get_terms(seg: dict, key: str) -> list:
    if key not in seg:
        return []
    return _extract_terms(seg[key])


def _field_is_empty(seg: dict, field: str) -> bool:
    """判断seg里的这个字段"提取不出任何有效内容"(不管是压根没给,还是
    给了但内容是空的/没有可提取的数字或短语)。"""
    kind = _FIELD_KIND[field]
    if kind == "number":
        return _get_number(seg, field) is None
    if kind == "numlist":
        return len(_get_numbers(seg, field)) == 0
    if kind == "termlist":
        return len(_get_terms(seg, field)) == 0
    if kind == "string":
        val = seg.get(field)
        return not (isinstance(val, str) and val.strip())
    raise AssertionError(f"unreachable: unknown field kind {kind!r}")


# =============================================================================
# of_terms 逐字核实(忽略大小写/空格/指定标点/美元符号,但保留方括号/连字符/
# 美元符号以外的数学符号)
# =============================================================================

_TERM_STRIP_CHARS = "".join([
    "，", ",", "。", ".", "；", ";", "：", ":", "！", "!", "？", "?",
    "“", "”", "‘", "’", '"', "'", "、",
    "(", ")", "（", "）", "—", "–",
    "$",  # 公式定界符,忽略掉——不然模型把同一个符号单独摘出来引用时,
          # 有没有重新包一层"$...$"这种跟内容本身无关的格式差异,会导致
          # 明明是原文里出现过的符号,却因为定界符对不上而被判定成"找不到"
])


def _term_normalize(s: str) -> str:
    s = s.lower()
    for ch in _TERM_STRIP_CHARS:
        s = s.replace(ch, "")
    s = re.sub(r"\s+", "", s)
    return s


def _check_terms_grounding(terms: list, self_index: int, segments: list):
    """检查terms里的每个短语,能不能在原文里找到印证:优先看"除self_index
    这一条之外,其余所有segment的text拼起来"的池子;这个池子里找不到的话,
    再看能不能在self_index自己这条的text里找到。

    如果有任何一个短语两处都找不到(自己的也没有、别处也没有),说明这是
    真正凭空编造的短语,返回 None(判定失败)。如果每个短语至少在两处之一
    能找到,返回一个列表:里面是"只能在自己这段文本里找到、没能在原文别处
    得到印证"的那些短语(找不到就是空列表)——这些短语不算校验失败,但值得
    标记出来,提醒下游这条依据没有在原文其它地方得到印证,不是空穴来风,
    但也不是被交叉印证过的。
    """
    pool_others = " ".join(
        _term_normalize(s["text"])
        for i, s in enumerate(segments) if i != self_index
    )
    pool_self = _term_normalize(segments[self_index]["text"])

    self_only = []
    for t in terms:
        t_norm = _term_normalize(t)
        if t_norm in pool_others:
            continue
        if t_norm in pool_self:
            self_only.append(t)
            continue
        return None  # 两处都找不到,真正的幻觉短语

    return self_only


# =============================================================================
# relation.expression 的 token 级校验
# =============================================================================

_EXPR_TOKEN_RE = re.compile(r"^(?:\s+|推出|等价|矛盾|[且和与或非()\[\]]|\d+)*$")


def _expression_tokens_valid(expr: str) -> bool:
    return bool(_EXPR_TOKEN_RE.fullmatch(expr))


def _brackets_balanced(expr: str) -> bool:
    depth_paren = 0
    depth_square = 0
    for ch in expr:
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
            if depth_paren < 0:
                return False
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
            if depth_square < 0:
                return False
    return depth_paren == 0 and depth_square == 0


# =============================================================================
# 文本还原校验(不忽略大小写/标点,只忽略空白)
# =============================================================================

def _strip_whitespace(s: str) -> str:
    return re.sub(r"\s+", "", s)


# =============================================================================
# 宽松JSON解析:模型有时不听"只输出JSON、不要有别的内容"这条指示,会把
# 整段JSON包在markdown代码块里(```json ... ``` 或者光```...```)。先按
# 原样解析;失败的话,如果整体正好是这种"被单个代码块包裹"的形状,剥掉
# 围栏再解析一次。两次都解析不出来才真正判定失败。
# =============================================================================

_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*\n(.*)\n```\s*$", re.DOTALL)


def _strip_markdown_fence(s: str):
    """s(去首尾空白后)如果是"整段被单个markdown代码块包裹"的形状,返回
    围栏里面的内容;不是这个形状返回 None。"""
    m = _CODE_FENCE_RE.match(s.strip())
    return m.group(1) if m else None


_SAFE_TO_KEEP_SINGLE = set('"\\/')  # 遇到这三种,当作"模型确实是有意转义,原样保留"

# \b/\f/\n/\r/\t 这五种,本身就是合法的JSON转义(退格/换页/换行/回车/制表符),
# 但也可能是模型忘了转义的LaTeX命令开头(\beta、\frac、\nabla、\rho、
# \theta...)。判断该按哪种解读,靠下面的全文逐字符对齐来核实,不是猜。
_AMBIGUOUS_ESCAPE_CHARS = {
    "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
}


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    return pos


_TEXT_FIELD_START_RE = re.compile(r'"text"\s*:\s*"')


def _find_text_field_starts(s: str) -> list:
    """找到s里每一处 "text": " 字段标记(容忍冒号/引号前后有多余空白),
    返回每一处对应的字段值真正开始的位置(左引号右边那一个位置)。"""
    return [m.end() for m in _TEXT_FIELD_START_RE.finditer(s)]


def _quote_is_escaped(s: str, pos: int) -> bool:
    """s[pos]是双引号,判断它是不是被转义的(前面连续反斜杠个数是奇数)。"""
    k = pos
    nb = 0
    while k > 0 and s[k - 1] == "\\":
        nb += 1
        k -= 1
    return nb % 2 == 1


def _align_text_fields(s: str, original_content: str):
    """
    在s(尚未json.loads()的原始JSON文本)里找到每一处"text"字段的值,从
    第一个字符到最后一个字符,跟original_content做逐字符对齐;遇到反斜杠
    时,按"当前original_content位置实际是什么字符"判断这个反斜杠该按
    标准转义解读、还是其实是没转义的字面反斜杠——不用局部窗口、不用猜
    字段边界,对齐这件事本身就同时验证了内容匹配、又确定了每个字段的
    准确边界。两边遇到空白不一致时,各自独立跳过空白字符再继续比,不算
    失败;但判断一个模糊转义(b/f/n/r/t)该怎么解读时,优先直接跟当前
    (未跳过空白的)位置比较——这样即使标准解读出来的刚好是空白字符
    (换行/制表符),也不会被空白容忍逻辑提前跳过而永远验证不到。

    每个"text"字段最终解码出来的内容,直接取 original_content 里这个
    字段对应的那一段切片,而不是拿s这边字符逐个拼——这样保证结果不只是
    "去空格后跟原文一致",而是逐字符精确等于原文的对应子串(包括空白的
    具体分布),下游(content_organizer/ 各模块)不需要再假设"拼接结果
    可能跟原文有空白差异"这种更弱的保证。

    全部"text"字段都对齐成功、且原文恰好被完全耗尽,才算成功,返回
    [(decoded_text, raw_value_start, raw_value_end), ...](顺序跟字段在
    s里出现的顺序一致;raw_value_start/end是该字段原始值在s里的字符
    范围,不含首尾引号,用于后续原地替换)。任何一步没法用"空白差异"或
    "转义歧义"解释的不一致,或者最后原文没被恰好耗尽,返回 None——这时
    候后续的字段级校验、of_terms核实等一律不用再做。
    """
    text_starts = _find_text_field_starts(s)
    if not text_starts:
        return None

    n_s = len(s)
    on = len(original_content)
    op = 0
    results = []

    for raw_start in text_starts:
        q = raw_start
        op_seg_start = op

        while True:
            if q >= n_s:
                return None

            ch = s[q]

            if ch == '"' and not _quote_is_escaped(s, q):
                results.append((original_content[op_seg_start:op], raw_start, q))
                break

            if ch == "\\":
                nxt = s[q + 1] if q + 1 < n_s else ""

                if nxt in _SAFE_TO_KEEP_SINGLE:
                    decoded, consumed = nxt, 2
                elif nxt == "u" and q + 6 <= n_s and all(c in "0123456789abcdefABCDEF" for c in s[q + 2:q + 6]):
                    decoded, consumed = chr(int(s[q + 2:q + 6], 16)), 6
                elif nxt in _AMBIGUOUS_ESCAPE_CHARS:
                    escape_char = _AMBIGUOUS_ESCAPE_CHARS[nxt]
                    if op < on and original_content[op] == escape_char:
                        decoded, consumed = escape_char, 2
                    elif op < on and original_content[op] == "\\":
                        decoded, consumed = "\\", 1
                    else:
                        ws_op = _skip_ws(original_content, op)
                        if ws_op < on and original_content[ws_op] == escape_char:
                            op, decoded, consumed = ws_op, escape_char, 2
                        elif ws_op < on and original_content[ws_op] == "\\":
                            op, decoded, consumed = ws_op, "\\", 1
                        else:
                            return None
                else:
                    # 真正非法的转义(比如\\o、\\m、\\i),没有别的解读方式,
                    # 只能是字面反斜杠
                    if op < on and original_content[op] == "\\":
                        decoded, consumed = "\\", 1
                    else:
                        ws_op = _skip_ws(original_content, op)
                        if ws_op < on and original_content[ws_op] == "\\":
                            op, decoded, consumed = ws_op, "\\", 1
                        else:
                            return None

                if op >= on or original_content[op] != decoded:
                    return None
                op += 1
                q += consumed
                continue

            # 普通字符(非反斜杠、非结束引号)
            if op < on and original_content[op] == ch:
                op += 1
                q += 1
                continue

            if ch.isspace():
                q += 1
                continue

            if op < on and original_content[op].isspace():
                op += 1
                continue

            return None

    op = _skip_ws(original_content, op)
    if op != on:
        return None

    return results


def _replace_text_field_values(s: str, aligned_results: list) -> str:
    """把s里每个"text"字段的原始值,替换成_align_text_fields验证过的
    解码后内容(用json.dumps()重新编码,保证转义正确;去掉dumps()外面
    自带的一对双引号,因为s里原来的引号还在)。"""
    pieces = []
    cursor = 0
    for decoded_text, raw_start, raw_end in aligned_results:
        pieces.append(s[cursor:raw_start])
        pieces.append(json.dumps(decoded_text, ensure_ascii=False)[1:-1])
        cursor = raw_end
    pieces.append(s[cursor:])
    return "".join(pieces)


def _enclosing_quoted_string_bounds(s: str, pos: int):
    """找到 s[pos] 所在这段JSON字符串值,最近的、未被转义的双引号边界
    (返回 [left, right),不含两侧引号)。用来给of_terms等"text"字段
    以外的转义核实框定范围,不让核实窗口跨出当前这个字符串值、混进
    JSON结构本身(逗号、方括号、字段名、别的字符串)。"""
    left = pos
    while left > 0:
        left -= 1
        if s[left] == '"' and not _quote_is_escaped(s, left):
            left += 1
            break
    else:
        left = 0

    right = pos
    while right < len(s):
        if s[right] == '"' and not _quote_is_escaped(s, right):
            break
        right += 1

    return left, right


def _escape_remaining_invalid_backslashes(s: str, pool: str) -> str:
    """"text"字段都已经替换成对齐验证过的正确内容之后,其余部分(主要是
    of_terms)如果还有反斜杠,这里处理:

      - 反斜杠+其它任何字符(不是"\\/,不是合法\\uXXXX,也不是b/f/n/r/t),
        没有别的解读方式,无条件修复成双反斜杠。

      - 反斜杠+b/f/n/r/t 这五种,可能是合法转义、也可能是没转义的LaTeX
        命令——不能凭空猜,也不能因为可能"反正后面还有核实"就不管;拿
        pool(所有"text"字段已经对齐验证过的正确内容拼在一起,等于是
        整篇论文原文的可靠版本)核实:分别构造"当作合法转义解读"和
        "当作字面反斜杠解读"两种局部窗口(反斜杠前后各取一段字符,且
        不跨出当前JSON字符串值的引号边界,避免混进逗号/方括号/别的
        字符串这些JSON结构噪音),看哪一种(忽略空白差异后)能在pool里
        找到。只有"字面反斜杠"这种能找到、"合法转义"这种找不到,才
        判定要修复;两种都能找到或都找不到(比如这个短语本来就不该
        出现在pool里,后面的of_terms逐字核实自会拒绝),保守按合法
        转义原样保留。
    """
    out = []
    i = 0
    n = len(s)
    pool_norm = _strip_whitespace(pool)
    window = 30

    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in _SAFE_TO_KEEP_SINGLE:
                out.append(s[i:i + 2])
                i += 2
                continue
            if nxt == "u" and i + 6 <= n and all(c in "0123456789abcdefABCDEF" for c in s[i + 2:i + 6]):
                out.append(s[i:i + 6])
                i += 6
                continue
            if nxt in _AMBIGUOUS_ESCAPE_CHARS:
                str_left, str_right = _enclosing_quoted_string_bounds(s, i)
                before = s[max(str_left, i - window):i]
                after = s[i + 2:min(str_right, i + 2 + window)]
                as_escape = _strip_whitespace(before + _AMBIGUOUS_ESCAPE_CHARS[nxt] + after)
                doubled = _strip_whitespace(before + "\\" + nxt + after)
                if doubled in pool_norm and as_escape not in pool_norm:
                    out.append("\\\\")
                    i += 1
                    continue
                out.append(s[i:i + 2])
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_with_alignment(raw_answer: str, original_content: str):
    """依次尝试(原样 / 剥掉markdown代码块围栏后),对每个候选文本做
    "text"字段跟original_content的全文逐字符对齐;对齐失败直接换下一个
    候选,都失败返回None。对齐成功后,把每个"text"字段替换成验证过的
    正确内容,再拿这些已经验证过的正确内容拼成的整体(pool),对剩余
    部分(主要是of_terms)里的模糊转义做同样有证据支持的修复,最后
    json.loads()拿到完整结构。"""
    fenced = _strip_markdown_fence(raw_answer)
    candidates = [raw_answer] + ([fenced] if fenced is not None else [])

    for text in candidates:
        aligned = _align_text_fields(text, original_content)
        if aligned is None:
            continue
        repaired = _replace_text_field_values(text, aligned)
        pool = "".join(decoded_text for decoded_text, _, _ in aligned)
        repaired = _escape_remaining_invalid_backslashes(repaired, pool)
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, TypeError):
            continue

    return None


# =============================================================================
# 主入口
# =============================================================================

def validate_and_correct(raw_answer, original_content: str):
    if raw_answer is None:
        return None
    parsed = _parse_with_alignment(raw_answer, original_content)
    if parsed is None:
        return None

    if not isinstance(parsed, dict):
        return None
    segments = parsed.get("segments")
    if not isinstance(segments, list) or len(segments) == 0:
        return None

    # ---- 第一步:逐条做结构+字段校验,归一化label,提取各字段的值 ----
    labels = []      # 跟segments等长,每项是归一化后的标准label
    extracted = []   # 跟segments等长,每项是dict,存这条segment提取出的字段

    for seg in segments:
        if not isinstance(seg, dict):
            return None
        text = seg.get("text")
        if not isinstance(text, str):
            return None

        label = _match_label(seg.get("label"))
        if label is None:
            return None

        # 通用的"不该有的字段必须提取不出内容"校验
        allowed = _ALLOWED_EXTRA_FIELDS[label]
        for field in _ALL_EXTRA_FIELDS:
            if field not in allowed and not _field_is_empty(seg, field):
                return None

        info = {}

        if label in CLAIM_LABELS:
            number = _get_number(seg, "number")
            if number is None:
                return None
            info["number"] = number

        if label in ("论据", "论证"):
            supports = _get_numbers(seg, "supports")
            if not supports:
                return None
            info["supports"] = supports

        elif label == "elaboration":
            of_nums = _get_numbers(seg, "of")
            of_terms = _get_terms(seg, "of_terms")
            if of_nums and of_terms:
                return None  # 二选一,不能同时有实质内容
            if of_nums:
                info["of"] = of_nums
            elif of_terms:
                info["of_terms"] = of_terms

        elif label == "example":
            illustrates = _get_numbers(seg, "illustrates")
            if not illustrates:
                return None
            info["illustrates"] = illustrates

        elif label == "instance":
            of_terms = _get_terms(seg, "of_terms")
            if not of_terms:
                return None
            info["of_terms"] = of_terms

        elif label == "relation":
            connects = _get_numbers(seg, "connects")
            if len(connects) < 2:
                return None
            expr_raw = seg.get("expression")
            if not (isinstance(expr_raw, str) and expr_raw.strip()):
                return None
            expr = expr_raw.strip()
            if not _expression_tokens_valid(expr):
                return None
            if not _brackets_balanced(expr):
                return None
            info["connects"] = connects
            info["expression"] = expr

        labels.append(label)
        extracted.append(info)

    # ---- 第二步(跨segment的编号一致性校验;文本还原已经在对齐阶段验证过了) ----
    number_to_label = {}
    for lbl, info in zip(labels, extracted):
        if lbl in CLAIM_LABELS:
            n = info["number"]
            if n in number_to_label and number_to_label[n] != lbl:
                return None  # 同一个编号被打了不同的label
            number_to_label[n] = lbl

    claim_numbers = set(number_to_label.keys())

    for lbl, info in zip(labels, extracted):
        if lbl in ("论据", "论证"):
            for n in info["supports"]:
                if n not in claim_numbers:
                    return None
            if lbl == "论据" and info["number"] in info["supports"]:
                return None  # 论据不能自引

        elif lbl == "example":
            for n in info["illustrates"]:
                if n not in claim_numbers:
                    return None
            if info["number"] in info["illustrates"]:
                return None  # example不能自引

        elif lbl == "elaboration" and "of" in info:
            for n in info["of"]:
                if n not in claim_numbers:
                    return None

        elif lbl == "relation":
            for n in info["connects"]:
                if n not in claim_numbers:
                    return None
            expr_numbers = set(_extract_numbers(info["expression"]))
            if expr_numbers != set(info["connects"]):
                return None

    # ---- 第四步:of_terms 逐字核实(允许"只在自己这段里找到",但要打警告) ----
    for idx, (lbl, info) in enumerate(zip(labels, extracted)):
        if lbl == "instance" or (lbl == "elaboration" and "of_terms" in info):
            self_only = _check_terms_grounding(info["of_terms"], idx, segments)
            if self_only is None:
                return None
            if self_only:
                info["of_terms_self_only"] = self_only

    # ---- 全部通过,组装矫正后的输出 ----
    corrected_segments = []
    for seg, lbl, info in zip(segments, labels, extracted):
        corrected = {"text": seg["text"], "label": lbl}
        corrected.update(info)
        corrected_segments.append(corrected)

    return {"segments": corrected_segments}
