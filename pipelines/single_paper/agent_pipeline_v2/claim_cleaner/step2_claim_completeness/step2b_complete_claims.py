"""
step2b_complete_claims.py

用法:
    python step2b_complete_claims.py <paper_id>
或不带参数运行,会提示输入。

============================== 这个文件取代了什么 ==============================
这是 step2(claim 语义完整性分析)的第二步,取代了之前两版尝试:
  1. 最早那版Haiku"是不是一个完整断言"/"有没有主语有没有谓语"的判断——
     实测下来对"缺主语/谓语"这个核心目标的召回率不稳定(75%~100%不等,
     跟问法细节强相关),而且它只回答一个粗粒度的是非判断,后面还需要
     另外一步去"补全"或"定位缺的内容在哪",等于没有真正做完事情。
  2. 后来试的spaCy依存句法分析路线——用完整conclusion原文解析+字符区间
     包含关系判断,在"缺主语/谓语"这个窄口径任务上做到了16条里只漏1条,
     但那唯一的漏判暴露了方法本身的天花板:有些情况缺的"主语"根本不是
     一个具体名词,而是另一条claim整个陈述的命题(比如"making X problematic"
     这种独立分词结构,隐含的主语是篇章层面的指代,不是句法树能读出来的
     结构信息)。这种案例需要真正理解论证逻辑,不是句法分析能解决的。

这一版不再拆成"先判断是否完整,再决定怎么补"两步,也不再区分"需要重构"
和"缺主语/谓语/状语"这些细分类别——直接一步到位:把整个conclusion的
全部内容(所有claim+所有非claim的elaboration/论证/motivation/framing/
relation/connection/instance/other)都喂给 Sonnet(开着思考),对每一条
非纯数据的claim,直接要求它产出一个"不依赖任何未引用内容"的完整表述——
不要求先诊断出"缺的是主语还是谓语还是别的",模型自己看完整context就能
判断该怎么补,诊断过程留在它的思考里,不需要落到输出schema上。

这一版还去掉了"premise"这个字段(早前版本用来单独记录claim依赖的条件/
限制)——现在的设计更统一:claim依赖的任何外部内容,只要不是"完整表述"
自己能直接理解的,一律通过【N】引用显式标出来(不再区分"能不能折进句子"
要不要单独开字段存),引用范围也不再局限于claim,可以引用这个conclusion
里的任何part。取而代之新增了"需要更多上下文"字段,用来处理"这个
conclusion的材料根本不够、怎么引用/怎么改写都补不出一个不依赖未引用
内容的版本"这种情况——让模型列出具体是哪个术语语义不清楚,而不是被逼着
编造或者勉强凑一个不诚实的完整表述。

============================== 处理逻辑 ==============================
  1. 读 data/<paper_id>/claim_completeness_analysis.json(step2a的产物,
     已经有"是纯实验数据"这个字段)和 organized_content.json(完整的
     conclusion结构,所有label都有,不只是claim)。
  2. 按conclusion分组。对每个conclusion:
       - 取出这个conclusion的全部organized_parts(所有label)作为上下文。
       - 从claim_completeness_analysis.json里找出这个conclusion下
         "是纯实验数据"为False的claim编号,作为"Claims to complete"列表。
       - 如果这个conclusion没有任何这样的claim(比如全是纯数据,或者
         这个conclusion本来就没有claim),跳过,不调用API。
  3. 调 Claude Sonnet 5(开着思考,effort=medium——这一步需要理解跨part
     的论证逻辑,但对"是否原词照用"的要求不像step1a那么严格、也没有
     逐字符校验的手段,思考太深反而容易让模型去改写不需要改的原词,
     所以不用xhigh),用
     complete_claims_validator.validate_complete_claims_response 校验,
     三档:
       - 第一档(Sonnet 5,开着思考medium)通过 -> status="ok"
       - 第一档拿到了回复但没通过(格式不对,或者编号集合对不上)-> 换
         第二档: Opus 5,关闭思考;通过 -> status="ok_after_retry_opus5_nothink"
       - 第二档也没通过 -> 换第三档: Sonnet 5,关闭思考,max_tokens是
         第二档的5倍;通过 -> status="ok_after_retry_sonnet5_nothink"
       - 三档都没通过(或者中途被跳过)-> 保守兜底:每条claim的"完整
         表述"直接用它自己原本的text,不做任何改写,"需要更多上下文"
         留空列表。选"保持原样"而不是"留空"或者"瞎编一个"的理由:
         原文本身通常已经是可用的内容,就算没能成功补全,也比空值或者
         错误改写更安全,不会让下游拿到明显有害的数据。
         status="fallback_original"。
  4. 每处理完一个conclusion,就把当前累积的完整记录列表整体重写一次到
     data/<paper_id>/claim_completeness_analysis.json(原地更新,新增
     "完整表述"、"需要更多上下文"、"completion_status"三个字段)。

============================== 输出格式(新增字段) ==============================
在 claim_completeness_analysis.json 每条记录里新增:
    "完整表述": str/null(是纯实验数据的claim为null,不适用),
    "需要更多上下文": list[str]/null(是纯实验数据的claim为null,不适用;
        其余claim如果模型确实碰到了"这个conclusion的材料补不出一个不
        依赖未引用内容的版本"的情况,这里会列出具体是哪些术语语义不
        清楚——空列表代表不需要,不代表出错),
    "completion_status": "ok"/"ok_after_category_retry"/"ok_after_retry_opus5_nothink"/"ok_after_retry_sonnet5_nothink"/"fallback_original"/null

不再产出"是完整断言"/"completeness_status"这两个字段(那是已经放弃的
旧方案留下的,如果同一个文件里还残留着这两个字段,不会被本脚本删除,
但也不会再被更新——如果想清理,自己手动删掉即可,不影响本脚本运行)。

路径解析基于本文件自身位置,预期跟 step2a_check_pure_data.py 放在同一个
step2_claim_completeness/ 目录下。
"""

import sys
import re
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

for _p in (PROJECT_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from claude_api_call import call_claude  # noqa: E402  (需要先把PROJECT_ROOT加进sys.path再import)
from complete_claims_validator import validate_complete_claims_response  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
PROMPT_TEMPLATE_PATH = SCRIPT_DIR / "step2b_complete_claims_prompt_template.txt"

CLAIM_LABELS = ("assertion", "论据", "example")

# 这一步需要理解跨part的论证逻辑,但对"是否原词照用"的要求不像step1a
# 那么严格(step1a要求切分出的片段能逐字符拼回原文,这一步只要求语义/
# 论证内容不能凭空增加,没有逐字符校验的余地),而且思考太深反而容易让
# 模型"手痒"去改写不需要改的原词——所以不用step1a那种xhigh,用medium。
# 这一步本身的复杂度也明显低于step1a的"分段+11类标签+编号+交叉引用"。
MODEL = "claude-sonnet-5"
THINKING = {"type": "adaptive", "effort": "medium"}
MAX_TOKENS = 20000

# 第一档判不出来时,依次升级到这两档(都关闭思考):
#   第二档: Opus 5
#   第三档: Sonnet 5,max_tokens是第二档的5倍
# 关闭思考是因为如果连Opus 5这种更强的模型都要靠思考才能做对,大概率是
# prompt本身有歧义,而不是算力不够,不该指望"再想想"掩盖这一点。
RETRY_MODEL_1 = "claude-opus-5"
RETRY_THINKING_1 = {"type": "disabled"}
RETRY_MAX_TOKENS_1 = 8000

RETRY_MODEL_2 = "claude-sonnet-5"
RETRY_THINKING_2 = {"type": "disabled"}
RETRY_MAX_TOKENS_2 = RETRY_MAX_TOKENS_1 * 5

RETRY_REMINDER = (
    "\n\nIMPORTANT: your previous reply did not follow the required JSON format, "
    "or did not include exactly one entry per claim number listed under "
    "\"Claims to complete\" above (with no additions, omissions, or duplicates, "
    "and with a non-empty \"完整表述\" string for each). Please strictly follow "
    "the exact JSON output format specified above, and output nothing else."
)

CATEGORY_REMINDER = (
    "\n\nIMPORTANT: your previous reply referred to (or drew words from) a part "
    "that is not labeled assertion, 论据, example, elaboration, framing, other, "
    "or motivation. Fix only that specific reference or borrowed content - "
    "replace it with an allowed reference or wording if one resolves it, or "
    "simply drop that particular detail if no allowed part covers it. Do not "
    "give up on the rest of the completion: still resolve every other missing "
    "piece and every other unclear reference or term using allowed parts, "
    "exactly as instructed above. Do not just revert the claim back to its "
    "original wording."
)


_NUMBER_REF_RE = re.compile(r"\[(\d+)\]")


def _convert_bracket_refs(text: str) -> str:
    """把字符串里"[数字]"这种旧式(半角方括号)编号引用换成"【数字】"。用于
    处理expression这类字段里可能已经嵌入了旧式方括号编号引用的情况(比如
    relation part的expression字段,形如"([6] 且 [9]) 推出 [11]")。"""
    return _NUMBER_REF_RE.sub(lambda m: f"【{m.group(1)}】", text)


def _prepare_part_for_prompt(p: dict) -> dict:
    """把一个organized_part转成"喂给模型看"的版本:所有编号引用字段
    (number本身、supports/illustrates/of/connects这几个跨part引用列表,
    以及expression字段里可能嵌入的旧式[数字]引用)统一换成【数字】这种
    全角方括号写法;content/label/of_terms这些字段原样不动——尤其是
    content字段,即使文字里本来就带数字(比如"90% sparsity"),那些数字
    不是编号引用,不能被误改。

    转换后的number字段从int变成了带方括号的字符串"【N】",这意味着这个
    结果不再是"number本该是int"这种意义上严格合法的JSON——这是有意的:
    这段内容不会真的经过JSON解析器,只是当成"看起来很结构化的文本"直接
    喂给模型读,目的是让模型在阅读输入的时候就直接看到【N】这个记号是
    怎么用在真实数据里的,不需要单独用大段文字去描述这个约定、模型自己
    照着输入里的样子学就行。也正因为不是要走JSON解析这条路,prompt里
    不会说"这是JSON",避免它对着一份"看起来是JSON但个别字段类型对不上"
    的东西较真。

    丢弃了source_spans字段——那是内部记账用的字符位置信息,跟这里"给模型
    读懂内容"的目的无关,只会增加噪音。
    """
    out = {
        "number": f"【{p['number']}】",
        "label": p["label"],
        "content": p["content"],
    }
    if p.get("of_terms"):
        out["of_terms"] = p["of_terms"]
    if p.get("supports"):
        out["supports"] = [f"【{n}】" for n in p["supports"]]
    if p.get("illustrates"):
        out["illustrates"] = [f"【{n}】" for n in p["illustrates"]]
    if p.get("of"):
        out["of"] = [f"【{n}】" for n in p["of"]]
    if p.get("connects"):
        out["connects"] = [f"【{n}】" for n in p["connects"]]
    if p.get("expression"):
        out["expression"] = _convert_bracket_refs(p["expression"])
    return out


def build_prompt(all_parts: list[dict], target_numbers: list[int], extra_reminder: str = "") -> str:
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    prepared_parts = [_prepare_part_for_prompt(p) for p in all_parts]
    context_block = json.dumps(prepared_parts, ensure_ascii=False, indent=2)
    target_str = "、".join(f"【{n}】" for n in target_numbers)
    return (
        template.rstrip("\n")
        + "\n"
        + context_block
        + "\n\nClaims to complete: "
        + target_str
        + extra_reminder
    )


def group_conclusions(organized_content_path: Path, pure_data_lookup: dict) -> list[tuple[str, list[dict], list[int]]]:
    """返回 [(conclusion短id, 全部organized_parts, 需要补全的claim编号列表), ...]。
    只保留"至少有一条需要补全的claim"的conclusion——纯数据的claim、以及
    压根没有claim的conclusion都不需要调用API处理。

    pure_data_lookup: {(conclusion短id, number): 是纯实验数据(bool)},来自
    step2a已经写好的claim_completeness_analysis.json,用来判断哪些claim
    编号需要补全、哪些是纯数据不需要。
    """
    conclusions = json.loads(organized_content_path.read_text(encoding="utf-8"))
    groups = []
    for c in conclusions:
        conc_short = c["id"].split("::")[-1]
        all_parts = c["organized_parts"]
        target_numbers = [
            p["number"]
            for p in all_parts
            if p["label"] in CLAIM_LABELS
            and pure_data_lookup.get((conc_short, p["number"])) is False
        ]
        if target_numbers:
            groups.append((conc_short, all_parts, target_numbers))
    return groups


def process_conclusion(
    all_parts: list[dict], target_numbers: list[int], interrupt_on_error: bool = True
) -> tuple[dict, str]:
    """对一个conclusion,调API产出这些target_numbers对应claim的"完整表述"。

    基本是三档(第一档 Sonnet 5开着思考medium -> 第二档 Opus 5关闭思考 ->
    第三档 Sonnet 5关闭思考max_tokens是第二档的5倍),每一档都拿到了回复
    但没通过校验时才升下一档(调用被跳过不升档,升档解决不了网络问题);
    三档都没通过就保守兜底,原文照抄。

    但第一档结束后有一个额外分支:如果validate_complete_claims_response
    报告"category_violation"(意思是其它检查都通过了,只有引用/取词的
    part类型选错了)——这种情况不直接跳去换更贵的模型,而是先在**同一档**
    (Sonnet 5,开着思考medium)上,把引用类型限制的提醒加进prompt,再试
    一次。这个提醒一旦被触发过,就会带进后面所有档位(哪怕后面某一档是
    因为别的原因失败、需要走普通的RETRY_REMINDER,引用类型提醒也会一并
    附上)——这样即使模型在后面某一档又犯了同样的引用类型错误,提醒也
    一直在场,不会被普通重试的prompt覆盖掉。

    interrupt_on_error: 透传给call_claude()——正式流程(main())保持默认
    True(遇到报错弹交互菜单,人工决定重试/跳过);调试脚本
    (debug_step2b_complete_claims.py)会传False,遇到报错直接抛异常、
    不弹菜单卡住。

    返回 (number -> 完整表述 的dict, status字符串)。
    """
    expected_numbers = set(target_numbers)
    part_labels = {p["number"]: p["label"] for p in all_parts}
    category_reminder = ""  # 一旦触发过category_violation就会被设为CATEGORY_REMINDER,并带进后续所有档位

    # 第一档: Sonnet 5,开着思考
    raw_answer = call_claude(
        build_prompt(all_parts, target_numbers, category_reminder),
        model=MODEL,
        thinking=THINKING,
        max_tokens=MAX_TOKENS,
        interrupt_on_error=interrupt_on_error,
    )
    result, category_violation = validate_complete_claims_response(raw_answer, expected_numbers, part_labels)
    status = "ok"

    if category_violation:
        # 其它检查都通过了,只有引用/取词的part类型选错了 -> 不升档,同一档
        # (Sonnet 5,开着思考medium)加提醒再试一次
        category_reminder = CATEGORY_REMINDER
        raw_answer = call_claude(
            build_prompt(all_parts, target_numbers, category_reminder),
            model=MODEL,
            thinking=THINKING,
            max_tokens=MAX_TOKENS,
            interrupt_on_error=interrupt_on_error,
        )
        result, category_violation = validate_complete_claims_response(raw_answer, expected_numbers, part_labels)
        status = "ok_after_category_retry"

    if result is None and raw_answer is not None:
        # 第二档: Opus 5,关闭思考(如果category_reminder之前被触发过,这里也带上)
        raw_answer = call_claude(
            build_prompt(all_parts, target_numbers, RETRY_REMINDER + category_reminder),
            model=RETRY_MODEL_1,
            thinking=RETRY_THINKING_1,
            max_tokens=RETRY_MAX_TOKENS_1,
            interrupt_on_error=interrupt_on_error,
        )
        result, category_violation = validate_complete_claims_response(raw_answer, expected_numbers, part_labels)
        status = "ok_after_retry_opus5_nothink"
        if category_violation:
            category_reminder = CATEGORY_REMINDER  # 万一是这一档才第一次触发,也记下来带进第三档

    if result is None and raw_answer is not None:
        # 第三档: Sonnet 5,关闭思考,max_tokens是第二档的5倍(同样带上category_reminder)
        raw_answer = call_claude(
            build_prompt(all_parts, target_numbers, RETRY_REMINDER + category_reminder),
            model=RETRY_MODEL_2,
            thinking=RETRY_THINKING_2,
            max_tokens=RETRY_MAX_TOKENS_2,
            interrupt_on_error=interrupt_on_error,
        )
        result, category_violation = validate_complete_claims_response(raw_answer, expected_numbers, part_labels)
        status = "ok_after_retry_sonnet5_nothink"

    if result is None:
        # 三档都没能拿到可用结果 -> 保守兜底,原文照抄,"需要更多上下文"留空
        # 列表(理由见模块docstring"处理逻辑"第3步)
        original_text_by_number = {p["number"]: p["content"] for p in all_parts}
        result = {
            n: {"完整表述": original_text_by_number[n], "需要更多上下文": []}
            for n in expected_numbers
        }
        status = "fallback_original"

    return result, status


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    organized_path = paper_dir / "organized_content.json"
    completeness_path = paper_dir / "claim_completeness_analysis.json"

    if not organized_path.exists():
        print(f"找不到文件: {organized_path}(需要先跑完 step1b_organize_labeled_content.py)")
        sys.exit(1)
    if not completeness_path.exists():
        print(f"找不到文件: {completeness_path}(需要先跑完 step2a_check_pure_data.py)")
        sys.exit(1)

    records = json.loads(completeness_path.read_text(encoding="utf-8"))
    pure_data_lookup = {(r["conclusion"], r["number"]): r["是纯实验数据"] for r in records}
    record_lookup = {(r["conclusion"], r["number"]): r for r in records}

    groups = group_conclusions(organized_path, pure_data_lookup)
    total_targets = sum(len(nums) for _, _, nums in groups)
    print(f"共 {len(groups)} 个conclusion含需要补全的claim,合计 {total_targets} 条")

    # 纯数据的claim(以及不在任何group里的,理论上不存在这种情况)先补null
    target_number_set_by_conc = {conc: set(nums) for conc, _, nums in groups}
    for r in records:
        conc = r["conclusion"]
        if r["是纯实验数据"] or r["number"] not in target_number_set_by_conc.get(conc, set()):
            r["完整表述"] = None
            r["需要更多上下文"] = None
            r["completion_status"] = None

    for conc_short, all_parts, target_numbers in groups:
        print(f"\n处理 {conc_short}({len(target_numbers)}条claim: {target_numbers}) ...")
        completed, status = process_conclusion(all_parts, target_numbers)

        for number, fields in completed.items():
            record_lookup[(conc_short, number)]["完整表述"] = fields["完整表述"]
            record_lookup[(conc_short, number)]["需要更多上下文"] = fields["需要更多上下文"]
            record_lookup[(conc_short, number)]["completion_status"] = status

        print(f"  -> 状态: {status}")

        completeness_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    n_fallback = sum(1 for r in records if r.get("completion_status") == "fallback_original")
    print(
        f"\n全部处理完成,共 {total_targets} 条claim完成补全"
        f"(其中{n_fallback}条走了保守兜底,原文照抄),已写入 {completeness_path}"
    )


if __name__ == "__main__":
    main()
