"""
debug_step1a_conclusion.py

诊断某一条 conclusion 在 step1a 里被判定为 fallback_trivial 的具体原因。

跟正式流程的区别:
  - label_validator.validate_and_correct() 校验失败时只返回 None,看不出
    具体是哪一步、哪一条segment、什么原因没通过。这个脚本把
    label_validator 内部同一套检查顺序原样走一遍(直接复用它已经写好、
    测过的内部函数,保证诊断结果和真实校验行为完全一致),但改成在第一处
    失败的地方打印详细诊断信息并停下来,而不是静默返回 None。
  - 只发一次 API 调用(用 step1a_pipeline.py 里第一次尝试的参数:Sonnet 5,
    思考深度xhigh,max_tokens=50000),不会像正式流程那样最多发3次——
    目的是省钱,同时大概率能重现同样的问题(如果是prompt/参数本身的问题,
    几乎每次都会犯,不需要真的把3次全部重跑一遍)。
  - 用 interrupt_on_error=False 调用,这样万一是API报错(比如新加的
    effort=xhigh 参数被这个模型拒绝、或者 claude_api_call.py 里
    _try_repair_400() 的关键词匹配没接住这次的实际错误文案),会直接把
    完整的异常信息打印出来,而不是弹交互式菜单卡住看不出发生了什么。

用法:
    python debug_step1a_conclusion.py <paper_id> [conclusion_index]
    conclusion_index 从0开始,默认0(即第一条,也就是你看到 fallback_trivial
    的那一条)。
"""

import sys
import json
from pathlib import Path

# 这个脚本放在项目根目录(跟 claude_api_call.py 同级)、或者放在
# step1_process_conclusions/ 目录下(跟 step1a_pipeline.py 同级)都能跑,
# 自动识别自己实际被放在了哪一种位置,不用手动挪文件。
_HERE = Path(__file__).resolve().parent

if (_HERE / "step1_process_conclusions" / "label_validator.py").is_file():
    # 放在项目根目录
    PROJECT_ROOT = _HERE
    SCRIPT_DIR = _HERE / "step1_process_conclusions"
elif (_HERE / "label_validator.py").is_file():
    # 放在 step1_process_conclusions/ 目录下
    SCRIPT_DIR = _HERE
    PROJECT_ROOT = _HERE.parent
else:
    print("找不到 label_validator.py——请把这个脚本放到下面两个位置之一再运行:")
    print(f"  1. 项目根目录(跟 claude_api_call.py 同级): {_HERE}")
    print(f"  2. step1_process_conclusions/ 目录下(跟 step1a_pipeline.py 同级)")
    sys.exit(1)

for _p in (PROJECT_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from claude_api_call import call_claude  # noqa: E402
import label_validator as lv  # noqa: E402
from step1a_pipeline import extract_conclusions, build_prompt  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"


def verbose_validate(raw_answer: str, original_content: str) -> None:
    """
    跟 label_validator.validate_and_correct() 走一模一样的检查顺序,在第一处
    失败的地方打印详细原因并返回,而不是像正式流程那样只返回 None。
    """
    try:
        parsed = json.loads(raw_answer)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"[诊断] JSON解析失败: {e}")
        print("------ 原始返回内容 ------")
        print(raw_answer)
        return

    if not isinstance(parsed, dict):
        print(f"[诊断] 顶层不是dict,而是 {type(parsed).__name__}")
        return
    segments = parsed.get("segments")
    if not isinstance(segments, list) or len(segments) == 0:
        print(f"[诊断] segments 缺失,或者不是非空列表: {segments!r}")
        return

    print(f"[诊断] 顶层结构OK,共 {len(segments)} 条segments,逐条检查字段:")

    labels = []
    extracted = []
    reconstructed = ""

    for idx, seg in enumerate(segments):
        tag = f"  segment[{idx}]"
        if not isinstance(seg, dict):
            print(f"{tag}: 不是dict,而是 {type(seg).__name__} -> 校验失败")
            return
        text = seg.get("text")
        if not isinstance(text, str):
            print(f"{tag}: text 缺失或不是字符串: {text!r} -> 校验失败")
            return

        raw_label = seg.get("label")
        label = lv._match_label(raw_label)
        if label is None:
            print(f"{tag}: label {raw_label!r} 无法归一化匹配到11个标准类别之一 -> 校验失败")
            return

        allowed = lv._ALLOWED_EXTRA_FIELDS[label]
        bad_field = None
        for field in lv._ALL_EXTRA_FIELDS:
            if field not in allowed and not lv._field_is_empty(seg, field):
                bad_field = field
                break
        if bad_field is not None:
            print(f"{tag}: label={label!r} 不该出现的字段 {bad_field!r}={seg.get(bad_field)!r} 却有实质内容 -> 校验失败")
            print(f"    完整segment: {seg}")
            return

        info = {}

        if label in lv.CLAIM_LABELS:
            number = lv._get_number(seg, "number")
            if number is None:
                print(f"{tag}: label={label!r} 缺少有效的 number 字段: {seg.get('number')!r} -> 校验失败")
                return
            info["number"] = number

        if label in ("论据", "论证"):
            supports = lv._get_numbers(seg, "supports")
            if not supports:
                print(f"{tag}: label={label!r} 的 supports 字段提取不出任何编号: {seg.get('supports')!r} -> 校验失败")
                return
            info["supports"] = supports

        elif label == "elaboration":
            of_nums = lv._get_numbers(seg, "of")
            of_terms = lv._get_terms(seg, "of_terms")
            if of_nums and of_terms:
                print(f"{tag}: elaboration 同时给了非空的 of={of_nums} 和 of_terms={of_terms},二选一冲突 -> 校验失败")
                return
            if of_nums:
                info["of"] = of_nums
            elif of_terms:
                info["of_terms"] = of_terms

        elif label == "example":
            illustrates = lv._get_numbers(seg, "illustrates")
            if not illustrates:
                print(f"{tag}: example 的 illustrates 字段提取不出任何编号: {seg.get('illustrates')!r} -> 校验失败")
                return
            info["illustrates"] = illustrates

        elif label == "instance":
            of_terms = lv._get_terms(seg, "of_terms")
            if not of_terms:
                print(f"{tag}: instance 的 of_terms 字段提取不出任何短语: {seg.get('of_terms')!r} -> 校验失败")
                return
            info["of_terms"] = of_terms

        elif label == "relation":
            connects = lv._get_numbers(seg, "connects")
            if len(connects) < 2:
                print(f"{tag}: relation 的 connects 提取出的编号少于2个: {connects}(原始值 {seg.get('connects')!r})-> 校验失败")
                return
            expr_raw = seg.get("expression")
            if not (isinstance(expr_raw, str) and expr_raw.strip()):
                print(f"{tag}: relation 的 expression 缺失或为空: {expr_raw!r} -> 校验失败")
                return
            expr = expr_raw.strip()
            if not lv._expression_tokens_valid(expr):
                print(f"{tag}: relation 的 expression 里有不认识的字符/词: {expr!r} -> 校验失败")
                return
            if not lv._brackets_balanced(expr):
                print(f"{tag}: relation 的 expression 括号不配对: {expr!r} -> 校验失败")
                return
            info["connects"] = connects
            info["expression"] = expr

        labels.append(label)
        extracted.append(info)
        reconstructed += text

    print("  (逐条字段检查全部通过)")

    orig_stripped = lv._strip_whitespace(original_content)
    recon_stripped = lv._strip_whitespace(reconstructed)
    if recon_stripped != orig_stripped:
        print("[诊断] 文本还原校验失败:所有segment拼接、去空格后,跟原文去空格后不一致")
        print(f"    原文去空格后长度: {len(orig_stripped)}")
        print(f"    拼接去空格后长度: {len(recon_stripped)}")
        n = min(len(orig_stripped), len(recon_stripped))
        first_diff = next((i for i in range(n) if orig_stripped[i] != recon_stripped[i]), n)
        lo = max(0, first_diff - 40)
        print(f"    从第 {first_diff} 个字符开始不一致:")
        print(f"      原文: ...{orig_stripped[lo:first_diff + 40]}...")
        print(f"      拼接: ...{recon_stripped[lo:first_diff + 40]}...")
        return

    print("  (文本还原校验通过)")

    number_to_label = {}
    for lbl, info in zip(labels, extracted):
        if lbl in lv.CLAIM_LABELS:
            n = info["number"]
            if n in number_to_label and number_to_label[n] != lbl:
                print(f"[诊断] 编号 {n} 同时被打上了不同的label: {number_to_label[n]!r} 和 {lbl!r} -> 校验失败")
                return
            number_to_label[n] = lbl

    claim_numbers = set(number_to_label.keys())
    print(f"  (共有claim编号: {sorted(claim_numbers)})")

    for idx, (lbl, info) in enumerate(zip(labels, extracted)):
        tag = f"  segment[{idx}]"
        if lbl in ("论据", "论证"):
            for n in info["supports"]:
                if n not in claim_numbers:
                    print(f"{tag}: label={lbl!r} 的 supports 引用了不存在的编号 {n} -> 校验失败")
                    return
            if lbl == "论据" and info["number"] in info["supports"]:
                print(f"{tag}: 论据(编号{info['number']})的 supports 引用了自己 -> 校验失败(禁止自引)")
                return
        elif lbl == "example":
            for n in info["illustrates"]:
                if n not in claim_numbers:
                    print(f"{tag}: label=example 的 illustrates 引用了不存在的编号 {n} -> 校验失败")
                    return
            if info["number"] in info["illustrates"]:
                print(f"{tag}: example(编号{info['number']})的 illustrates 引用了自己 -> 校验失败(禁止自引)")
                return
        elif lbl == "elaboration" and "of" in info:
            for n in info["of"]:
                if n not in claim_numbers:
                    print(f"{tag}: elaboration 的 of 引用了不存在的编号 {n} -> 校验失败")
                    return
        elif lbl == "relation":
            for n in info["connects"]:
                if n not in claim_numbers:
                    print(f"{tag}: relation 的 connects 引用了不存在的编号 {n} -> 校验失败")
                    return
            expr_numbers = set(lv._extract_numbers(info["expression"]))
            if expr_numbers != set(info["connects"]):
                print(f"{tag}: relation 的 expression 里的编号集合 {expr_numbers} 跟 connects {set(info['connects'])} 不一致 -> 校验失败")
                return

    print("  (跨segment编号一致性校验通过)")

    for idx, (lbl, info) in enumerate(zip(labels, extracted)):
        if lbl == "instance" or (lbl == "elaboration" and "of_terms" in info):
            if not lv._terms_found_elsewhere(info["of_terms"], idx, segments):
                print(f"  segment[{idx}]: label={lbl!r} 的 of_terms={info['of_terms']} 没能在原文其它地方找到 -> 校验失败(禁止自引/幻觉短语)")
                return

    print("  (of_terms 逐字核实通过)")
    print()
    print("[诊断] 所有检查都通过了——按理说应该能拿到正常结果。")
    print("        如果实际跑正式流程还是失败,可能是这次诊断生成的内容跟当时不完全")
    print("        一样(模型不是完全确定性的),建议多跑几次看是否稳定复现。")


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    graph_path = DATA_DIR / paper_id / "graph.json"
    if not graph_path.exists():
        print(f"找不到文件: {graph_path}")
        sys.exit(1)

    conclusions = extract_conclusions(graph_path)
    if idx >= len(conclusions):
        print(f"conclusion_index={idx} 超出范围,这篇论文一共只有 {len(conclusions)} 条")
        sys.exit(1)

    conclusion = conclusions[idx]
    content = conclusion["content"]
    print(f"论文: {paper_id}")
    print(f"conclusion[{idx}]: {conclusion['id']}")
    print(f"content 长度: {len(content)} 字符")
    print()
    print("发起API调用(用第一次尝试的参数: Sonnet 5, 思考深度xhigh, max_tokens=50000)...")
    print("(思考深度xhigh可能会需要一点时间,这是正常的,不代表卡住了)")
    print()

    try:
        raw_answer = call_claude(
            build_prompt(content),
            model="claude-sonnet-5",
            thinking={"type": "adaptive", "effort": "xhigh"},
            max_tokens=50000,
            interrupt_on_error=False,
        )
    except Exception as e:
        print("[诊断] API调用直接抛出了异常,这大概率就是问题所在:")
        print(f"    异常类型: {type(e).__module__}.{type(e).__name__}")
        print(f"    异常内容: {e}")
        status_code = getattr(e, "status_code", None)
        if status_code is not None:
            print(f"    状态码: {status_code}")
        print()
        print("    如果是400错误、且错误内容提到了 effort/xhigh/thinking 之类的关键词,")
        print("    大概率是 claude_api_call.py 里 _try_repair_400() 的关键词匹配没接住")
        print("    这次的实际错误文案(比如模型不支持给Sonnet设xhigh),需要把上面这行")
        print("    完整的异常内容发回去,把匹配规则/参数选择调整一下。")
        sys.exit(1)

    out_path = SCRIPT_DIR / f"debug_raw_answer_{paper_id}_{idx}.txt"
    out_path.write_text(raw_answer if raw_answer is not None else "(None)", encoding="utf-8")
    print(f"原始返回已保存到: {out_path}")
    print()

    if raw_answer is None:
        print("[诊断] raw_answer 是 None —— 但这次用的是 interrupt_on_error=False,")
        print("        按理说报错会直接抛异常、不会走到这里返回None,如果看到这行,")
        print("        说明 call_claude() 内部逻辑有问题,需要进一步排查代码本身。")
        sys.exit(1)

    print(f"raw_answer 长度: {len(raw_answer)} 字符")
    print()
    print("开始逐步校验诊断:")
    verbose_validate(raw_answer, content)


if __name__ == "__main__":
    main()
