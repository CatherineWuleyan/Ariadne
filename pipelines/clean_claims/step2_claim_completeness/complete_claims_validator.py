"""
complete_claims_validator.py

对 step2b_complete_claims.py 里"补全后不依赖任何未引用内容的完整claim
表述"结果的校验+提取。

对外只暴露一个函数:

    validate_complete_claims_response(raw_answer, expected_numbers, part_labels) -> (dict | None, bool)

- raw_answer: API 原始返回的文本(可能是 None,代表这次调用被跳过)
- expected_numbers: 这次 prompt 里"Claims to complete"下实际列出的claim
  编号集合(iterable of int;全局编号,可能有跳号)
- part_labels: {number(int): label(str)},这个conclusion里全部part(不分
  claim还是非claim类型)的编号->label映射——用来检查"完整表述"里嵌入的
  【N】引用,是不是指向这个conclusion里真实存在的part,以及那个part的
  类型是否在允许被引用/取词的范围内
- 返回值是一个二元组 (result, category_violation):
    - 全部通过 -> ({number(int): {"完整表述": str, "需要更多上下文": list[str]}, ...}, False)
    - 除了"引用类型限制"这一条,其它全部通过,但引用了不允许类型的part
      -> (None, True)——单独区分出这种情况,是因为调用方
      (step2b_complete_claims.py)对这种"其它都对、只有引用类型选错了"
      的情况,会走一条不一样的重试路径(同一档模型加提醒再试一次,而不是
      直接换更贵的模型)
    - 其它任何一步没通过(格式不对/编号漏答多答/自引用/引用不存在的
      编号/用了省略式范围引用) -> (None, False)

============================== 这一版改了什么 ==============================
新增了两条硬性校验:
  1. "完整表述"里不能用"【N】-【M】"这种省略式范围引用来一次性指代多个
     编号——模型有时会这样写表示"从N到M全部",但我们的编号提取只认单个
     【数字】,这种写法会让中间那些编号悄悄漏检,必须判失败逼它老实地把
     每个编号都单独列出来。
  2. 引用/取词的part类型收紧到 ALLOWED_REFERENCE_LABELS 这个白名单——
     基于对参考答案里全部27处"遗失的主语/谓语/状语"引用来源做过的实测
     核对,论证/instance/relation/connection这4类一次都没被引用过,不再
     允许被引用/取词。

============================== 校验设计 ==============================
延续上一版的整体思路(接近label_validator.py:容忍markdown代码块围栏、
宽松提取JSON,但不需要它那套逐字符对齐原文的重量级机制,因为这一步的
输出是"改写后的新文本",不要求跟原文逐字符对应)。

对"完整表述"里嵌入的【N】引用的校验规则改成了两条(不再检查"是不是
claim类型"):
  1. 不能引用自己(引用自己没有意义,只能是出错)
  2. 引用的编号必须是这个conclusion里真实存在的某个part(不管是claim
     还是非claim类型)——防止编号是凭空编出来的、这个conclusion里根本
     没有这一个part
"""

import re
import json


_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*\n(.*)\n```\s*$", re.DOTALL)
_BRACKET_CONTENT_RE = re.compile(r"【([^】]*)】")
_RANGE_SHORTHAND_RE = re.compile(r"】\s*[-–—]\s*【")

# 允许被"取词/引用"的part类型——基于对参考答案里全部27处"遗失的主语/谓语/
# 状语"引用来源做过的实测核对:assertion/论据/elaboration/framing 这4类
# 已经覆盖了全部27处引用,example没在样本里出现过但跟assertion/论据同属
# claim,一并算作允许;other/motivation是在样本证据之外额外加的安全余量。
# 论证/instance/relation/connection在27处样本里一次都没被引用过,不允许。
ALLOWED_REFERENCE_LABELS = {
    "assertion", "论据", "example", "elaboration", "framing", "other", "motivation",
}


def _strip_markdown_fence(s: str):
    m = _CODE_FENCE_RE.match(s.strip())
    return m.group(1) if m else None


def _parse_json_loosely(raw_answer: str):
    fenced = _strip_markdown_fence(raw_answer)
    candidates = [raw_answer] + ([fenced] if fenced is not None else [])
    for text in candidates:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _extract_number(value):
    if value is None or isinstance(value, bool):
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _extract_refs(text: str) -> set:
    """从文本里找出全部【...】,每一个括号内部按"遇到任意非数字字符就切断"
    的规则提取出全部连续数字串,合并成一个引用编号集合(int)。

    这样"【6】"(单个编号)和"【1, 2, 3, 4】"(多个编号挤在一个括号里,
    用逗号+空格分隔)都能正确提取——不管括号内数字之间用的是逗号、空格、
    顿号还是别的什么字符,只要不是数字,就会被当成分隔符切开,不需要
    针对某种具体分隔符写死规则。"""
    refs = set()
    for content in _BRACKET_CONTENT_RE.findall(text):
        refs.update(int(n) for n in re.findall(r"\d+", content))
    return refs


def _has_range_shorthand(text: str) -> bool:
    """检测"【N】-【M】"这种省略式范围引用——模型有时会用这种写法表示
    "从N到M全部引用",但我们的编号提取只认单个【数字】,这种省略写法会
    让中间那些编号(比如N到M之间的)悄悄漏检。判定失败,逼模型老实地把
    每个编号都单独写出来。"""
    return bool(_RANGE_SHORTHAND_RE.search(text))


def _references_are_valid(text: str, own_number: int, all_part_numbers: set) -> bool:
    """检查"完整表述"里嵌入的【N】引用,是否满足"必须真实存在、不能是自己"
    这两条硬性要求(不含"类型是否允许"——那条单独用
    _category_violations()检查,因为它需要区别对待:全部其它检查都通过、
    只有这一条不满足时,走一条不一样的重试路径,见
    step2b_complete_claims.py)。"""
    for ref in _extract_refs(text):
        if ref == own_number:
            return False
        if ref not in all_part_numbers:
            return False
    return True


def _category_violations(text: str, part_labels: dict) -> bool:
    """检查"完整表述"里嵌入的【N】引用,有没有指向不允许的part类型
    (ALLOWED_REFERENCE_LABELS之外的)。调用前提是_references_are_valid
    已经确认过这些引用都是真实存在、且不是自引用的编号,这里不再重复
    做那两项检查。"""
    return any(part_labels.get(ref) not in ALLOWED_REFERENCE_LABELS for ref in _extract_refs(text))


def validate_complete_claims_response(raw_answer, expected_numbers, part_labels):
    """
    part_labels: {number(int): label(str)},这个conclusion里全部part的
    编号->label映射(不只是claim,所有label都要包含在内,因为引用/取词的
    合法性检查需要知道每个编号对应的真实类型)。

    返回 (result, category_violation):
      - 全部通过 -> ({number: {"完整表述":str,"需要更多上下文":list}, ...}, False)
      - 除了"引用类型限制"这一条,其它全部通过,但引用了不允许类型的part
        -> (None, True)
      - 其它任何一步没通过(格式不对/编号漏答多答/自引用/引用不存在的
        编号/用了省略式范围引用) -> (None, False)
    """
    if raw_answer is None:
        return None, False

    parsed = _parse_json_loosely(raw_answer)
    if not isinstance(parsed, dict):
        return None, False

    results = parsed.get("results")
    if not isinstance(results, list) or len(results) == 0:
        return None, False

    all_part_numbers = set(part_labels.keys())
    output = {}
    any_category_violation = False

    for item in results:
        if not isinstance(item, dict):
            return None, False

        number = _extract_number(item.get("number"))
        if number is None:
            return None, False

        text = item.get("完整表述")
        if not isinstance(text, str) or not text.strip():
            return None, False  # 空字符串/非字符串一律判失败,不接受"补全成空"这种结果

        if _has_range_shorthand(text):
            return None, False  # 用了"【N】-【M】"这种省略式范围引用

        if not _references_are_valid(text, number, all_part_numbers):
            return None, False  # 引用了不存在的part编号,或者引用了自己

        if _category_violations(text, part_labels):
            any_category_violation = True  # 先记下来,不立即判失败,等其它检查都跑完再统一处理

        needs_context = item.get("需要更多上下文")
        if needs_context is None:
            needs_context = []
        elif not isinstance(needs_context, list) or not all(
            isinstance(t, str) and t.strip() for t in needs_context
        ):
            return None, False  # 存在这个字段但不是"非空字符串组成的列表",判失败
        else:
            needs_context = [t.strip() for t in needs_context]

        if number in output:
            return None, False  # 同一个编号出现了两次

        output[number] = {"完整表述": text.strip(), "需要更多上下文": needs_context}

    if set(output.keys()) != set(expected_numbers):
        return None, False  # 跟这次prompt里"Claims to complete"列出的编号集合对不上

    if any_category_violation:
        return None, True  # 其它全部通过,只有引用类型这一条没过

    return output, False
