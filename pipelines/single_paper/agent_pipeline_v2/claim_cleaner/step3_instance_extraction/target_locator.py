"""
target_locator.py

给定一条conclusion的原始 content 字符串,以及某个 instance part 的
of_terms 短语列表 + 它自己在 content 里的位置(instance_start, instance_end),
为每个 term 在 content 里定位一个精确的目标字符区间。

============================== 排除instance自身 ==============================
所有层次的匹配都先排除跟 instance 自己的 [instance_start, instance_end)
区间重叠的位置——instance 的定义就是"不能在自己文本里包含被解释的词"
(见 step1a_prompt_template.txt 对 instance 的说明),我们要定位的本来就是
"别处"的目标,不是instance自己。

如果排除自身之后,连下面四层里最宽松的一层都找不到任何匹配,直接判定为
not_found,不再有旧版本里"退回instance自己文本里找"的 self_only 分支。

============================== 四层递进严格匹配 ==============================
不再按"离instance最近"来选,而是让匹配规则从松到严依次收紧,只要某一层
的匹配数收紧到恰好1个,就用那一个;都收不到唯一时,给出候选列表而不是
瞎选一个。四层定义:

  L1 原始松匹配:跟 label_validator._check_terms_grounding 完全同一套规则
     (忽略大小写、忽略空白、忽略中英文引号/逗号/句号/顿号/圆括号/破折号/
     美元符号,但保留方括号/连字符/数学符号),直接复用 label_validator 的
     _term_normalize,不重新实现一份、避免两边规则跑偏。
  L2 只忽略大小写和空格:不再忽略任何标点(标点必须逐字符精确出现),
     空格仍然被忽略(不管有没有空格、几个空格都一样),大小写也忽略。
  L3 只忽略大小写:空格也必须精确匹配(有没有空格、空格数量都要对上),
     只有大小写被忽略。
  L4 精确匹配:不做任何归一化,区分大小写,逐字符精确匹配。

四层是同一个原则的不同松紧度:每一层能"看作相同"的字符差异,都是上一层
的子集,所以理论上层次越严格,匹配数只会越来越少、不会变多。处理规则:

  1. 如果排除自身后,L1(最松)的匹配数就是0 -> not_found,位置留空。
  2. 依次检查 L1 -> L2 -> L3 -> L4,只要某一层的匹配数恰好是1个,就采用
     那一个位置,certainty="certain",不再继续看更严格的层。
  3. 如果四层都没有恰好为1(要么一直>1,要么中途变成0而在那之前从没等于
     过1) -> 取"最后一个匹配数>0"的那一层的全部位置作为候选列表,
     certainty="uncertain"。特别地,如果一路到L4都还>1(原文里有完全
     一模一样的多次出现,没有更严格的层可以再收紧),候选就是L4的全部
     位置。

============================== span裁剪 ==============================
L1/L2这两层本身在归一化时就跳过了空白字符(不会给空白字符建立位置索引),
天然不会让算出来的span首尾是空白。但L3/L4不忽略空格,如果term本身
(of_terms里的短语)前后恰好带了多余的空格,匹配到的原始span可能会把这些
首尾空白也包含进去。不管是哪一层选出来的位置,最终写入结果之前都统一做
一次首尾空白裁剪(_trim_span),保证"content"字段和"start"/"end"对应的
永远是去掉首尾空白后的干净范围。

============================== 已知的局限 ==============================
四层匹配全部沿用子串包含判断,没有词边界概念。对于归一化后依然很短的
term(比如某个字母类的数学符号,剥掉美元符号后剩一两个字符),即使升到
L4精确匹配,也可能在原文别处的英文单词里连续出现同样的字符片段而被
判定为多个"完全相同"的候选,从而落到 uncertain 而不是 certain——这是
子串匹配的固有特性,不是这一层设计能完全消除的,遇到 uncertain 或者
term本身很短的情况,使用方应该人工复核。
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
_STEP1_DIR = _PROJECT_ROOT / "step1_process_conclusions"

for _p in (_PROJECT_ROOT, _STEP1_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from label_validator import _term_normalize  # noqa: E402  L1直接复用同一套归一化规则


# =============================================================================
# 通用扫描:给定"归一化文本+每个归一化字符对应的原始下标"和"归一化后的term",
# 找出全部匹配区间(允许重叠匹配,不做非重叠跳跃扫描,避免漏掉相关位置)。
# L1/L2/L3/L4 四层都复用这同一个扫描函数,只是各自构造归一化文本/term的
# 方式不同(见下面 _matches_at_level)。
# =============================================================================

def _scan_matches(normalized_text: str, raw_positions: list, term_norm: str) -> list:
    if not term_norm:
        return []
    matches = []
    search_from = 0
    term_len = len(term_norm)
    while True:
        idx = normalized_text.find(term_norm, search_from)
        if idx == -1:
            break
        raw_start = raw_positions[idx]
        raw_end = raw_positions[idx + term_len - 1] + 1
        matches.append((raw_start, raw_end))
        search_from = idx + 1
    return matches


# =============================================================================
# L1: 跟 label_validator 完全一致的归一化(忽略大小写/空白/特定标点)
# =============================================================================

def _build_map_L1(text: str):
    """逐字符调用 label_validator._term_normalize:该函数对单个字符的处理
    (小写化/是否落在剔除标点集合里/是否是空白)都是逐字符独立判断、不看
    上下文的,所以对整串字符调用一次和逐字符调用再拼接,结果完全一致——
    这个前提让这里可以省掉搜索式对齐,一次扫描就建好精确的位置映射。"""
    chars, positions = [], []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        norm_ch = _term_normalize(ch)
        if norm_ch == "":
            continue
        chars.append(norm_ch)
        positions.append(i)
    return "".join(chars), positions


def _matches_L1(content: str, term: str) -> list:
    term_norm = _term_normalize(term)
    if not term_norm:
        return []
    normalized_text, raw_positions = _build_map_L1(content)
    return _scan_matches(normalized_text, raw_positions, term_norm)


# =============================================================================
# L2/L3/L4: 本模块自己的两参数开关(要不要转小写、要不要去空格),完全不去
# 任何标点——这是L2/L3/L4跟L1的本质区别:L1还会去掉引号/逗号/句号/圆括号等
# 标点,L2/L3/L4把所有标点原样保留,只在"大小写"和"空格"这两个维度上取舍。
# =============================================================================

def _build_map_case_space(text: str, lower: bool, strip_space: bool):
    chars, positions = [], []
    for i, ch in enumerate(text):
        if strip_space and ch.isspace():
            continue
        chars.append(ch.lower() if lower else ch)
        positions.append(i)
    return "".join(chars), positions


def _normalize_case_space(s: str, lower: bool, strip_space: bool) -> str:
    chars = []
    for ch in s:
        if strip_space and ch.isspace():
            continue
        chars.append(ch.lower() if lower else ch)
    return "".join(chars)


def _matches_case_space(content: str, term: str, lower: bool, strip_space: bool) -> list:
    term_norm = _normalize_case_space(term, lower, strip_space)
    if not term_norm:
        return []
    normalized_text, raw_positions = _build_map_case_space(content, lower, strip_space)
    return _scan_matches(normalized_text, raw_positions, term_norm)


# =============================================================================
# 四层定义 + 排除instance自身 + 逐层收紧的选择逻辑
# =============================================================================

_LEVEL_NAMES = ("L1_loose", "L2_case_space", "L3_case_only", "L4_exact")


def _matches_at_level(content: str, term: str, level_name: str) -> list:
    if level_name == "L1_loose":
        return _matches_L1(content, term)
    if level_name == "L2_case_space":
        return _matches_case_space(content, term, lower=True, strip_space=True)
    if level_name == "L3_case_only":
        return _matches_case_space(content, term, lower=True, strip_space=False)
    if level_name == "L4_exact":
        return _matches_case_space(content, term, lower=False, strip_space=False)
    raise ValueError(f"未知的层级名: {level_name!r}")


def _overlaps(s: int, e: int, instance_start: int, instance_end: int) -> bool:
    return s < instance_end and instance_start < e


def _trim_span(content: str, start: int, end: int):
    """裁掉[start, end)首尾的空白字符,返回裁剪后的(start, end)。"""
    while start < end and content[start].isspace():
        start += 1
    while end > start and content[end - 1].isspace():
        end -= 1
    return start, end


def locate_target(content: str, term: str, instance_start: int, instance_end: int) -> dict:
    """
    为 instance 的一个 of_terms 短语,在 content 里定位目标位置。

    返回:
        {
          "term": term,
          "certainty": "certain" | "uncertain" | "not_found",
          "content": <裁剪后的原文原样内容> 或 None,
          "start": int 或 None,
          "end": int 或 None,
          "candidates": [{"content": ..., "start": ..., "end": ...}, ...],
        }
    "content"/"start"/"end" 只在 certainty=="certain" 时有值,其余情况为
    None;"candidates" 只在 certainty=="uncertain" 时有内容,其余情况为
    空列表。
    """
    level_matches = []
    for level_name in _LEVEL_NAMES:
        raw_matches = _matches_at_level(content, term, level_name)
        external = [
            (s, e) for s, e in raw_matches
            if not _overlaps(s, e, instance_start, instance_end)
        ]
        level_matches.append(external)

    if not level_matches[0]:
        return {
            "term": term, "certainty": "not_found",
            "content": None, "start": None, "end": None,
            "candidates": [],
        }

    for matches in level_matches:
        if len(matches) == 1:
            s, e = _trim_span(content, *matches[0])
            return {
                "term": term, "certainty": "certain",
                "content": content[s:e], "start": s, "end": e,
                "candidates": [],
            }

    # 没有任何一层恰好唯一 -> 取"最后一个匹配数>0"的那一层作为候选集合
    # (如果中途某层变成0,循环在那里停下,保留上一层非空的结果;如果一路
    # 到L4都还>1,candidate_matches会一直更新到L4的结果)
    candidate_matches = level_matches[0]
    for matches in level_matches[1:]:
        if not matches:
            break
        candidate_matches = matches

    candidates = []
    for s, e in candidate_matches:
        ts, te = _trim_span(content, s, e)
        candidates.append({"content": content[ts:te], "start": ts, "end": te})

    return {
        "term": term, "certainty": "uncertain",
        "content": None, "start": None, "end": None,
        "candidates": candidates,
    }
