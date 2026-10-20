"""
step2c_check_instance_containment.py

用法:
    python step2c_check_instance_containment.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

step2(claim 语义完整性分析)的第三个子步骤:对 claim_completeness_analysis.json
里已有的每一条claim,追加判断——这条claim"完整化后的内容"里,有没有包含
同一个conclusion里**至少一个**instance举的具体例子(只问有没有,不问是哪个)。

============================== 处理逻辑 ==============================
  1. 读 organized_content.json,按conclusion取出全部label=="instance"的
     part。没有instance的conclusion记下来,后面遇到就跳过、不调API。
  2. 读 claim_completeness_analysis.json 里已有的全部claim记录,按原有顺序
     逐条处理,不改动任何已有字段,只追加两个新字段:
       "instance_containment": true/false/null
       "instance_containment_status": 处理状态字符串
  3. 对每条claim:
       - 是纯实验数据(是纯实验数据==true)的claim不测这项,直接跳过,
         status="skipped_pure_data"。
       - 所在conclusion没有任何instance的,也跳过,
         status="skipped_no_instances"。
       - 否则,取这条claim的"完整表述"(理论上非纯数据claim这个字段总是
         有值,见complete_text_of()注释),连同conclusion原始content、这个
         conclusion里全部instance的完整字段一起拼进prompt,问Claude:这些
         instance里有没有至少一个的举例内容被这条claim包含。
  4. 校验:期望回复是单个词true/false。复用 pure_data_validator._judge_bool
     做单值提取(has 't' xor has 'f',撞了再退一步用完整单词精确匹配)——
     这就是那个函数本来做的事,不用整个validate_pure_data_response(那个
     是给"一段文本里有多个编号,每个编号各自判一次"这种形状设计的,这里
     只有一个claim、一个单一判断,没有编号,用不上那一层)。
       - 第一档(Haiku,开着思考)判出来了 -> status="ok"
       - 判不出来但确实拿到了回复 -> 换第二档:Sonnet 5,关闭思考,重试;
         判出来了 -> status="ok_after_retry_sonnet5_nothink"
       - 两档都判不出来(或被跳过)-> 保守兜底:记为True。选True而不是
         False作为兜底值是使用方明确要的方向(不是套用step2a"兜底选False"
         那套理由,这一步的下游用途不同,由使用方自己权衡过)。
         status="fallback_trivial"。
  5. 每处理完一条claim就把当前累积的完整claim_completeness_analysis.json
     整体重写一次,防止中途被打断丢失进度。

============================== 输出格式 ==============================
claim_completeness_analysis.json里每条claim记录,在已有字段基础上追加:
    {
      ... (已有字段原样保留) ...
      "instance_containment": true,
      "instance_containment_status": "ok"
    }
"instance_containment"在跳过时(纯数据claim,或所在conclusion没有instance)
为null,对应status分别是"skipped_pure_data"/"skipped_no_instances"。

路径解析基于本文件自身位置,预期跟 step2a_check_pure_data.py /
step2b_complete_claims.py 同放在 step2_claim_completeness/ 目录下。
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

for _p in (PROJECT_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from claude_api_call import call_claude  # noqa: E402
from pure_data_validator import _judge_bool  # noqa: E402  # 复用单值true/false提取逻辑

DATA_DIR = PROJECT_ROOT / "data"
PROMPT_TEMPLATE_PATH = SCRIPT_DIR / "step2c_instance_containment_prompt_template.txt"

MODEL = "claude-haiku-4-5-20251001"
THINKING = {"type": "enabled", "budget_tokens": 4096}
# 注意:max_tokens必须严格大于thinking.budget_tokens(max_tokens是"思考+
# 最终答案"的总预算,budget_tokens必须给最终答案留出空间,否则API直接400)。
# 虽然这一步的最终答案只有一个词(true/false),占不了几个token,但budget_tokens
# 本身就是4096,所以max_tokens不能低于这个数——这里跟step2a用一样的8000,
# 留够余量,不要因为"输出短"就想当然地把max_tokens也调小。
MAX_TOKENS = 8000

RETRY_MODEL = "claude-sonnet-5"
RETRY_THINKING = {"type": "disabled"}
RETRY_MAX_TOKENS = 6000

RETRY_REMINDER = (
    "\n\nIMPORTANT: your previous reply did not follow the required output "
    "format. Answer with exactly one word - true or false - and output "
    "nothing else."
)


def load_conclusion_instances(organized_content_path: Path) -> dict:
    """返回 {conclusion短id: {"content": 原始content, "instances": [完整
    instance dict,...]}},只保留至少有1个instance的conclusion。"""
    conclusions = json.loads(organized_content_path.read_text(encoding="utf-8"))
    result = {}
    for c in conclusions:
        conc_short = c["id"].split("::")[-1]
        instances = [p for p in c["organized_parts"] if p["label"] == "instance"]
        if instances:
            result[conc_short] = {"content": c["content"], "instances": instances}
    return result


def complete_text_of(claim_record: dict) -> str:
    """这条claim"完整化后的内容":用step2b补全的"完整表述"。真实数据里,
    非纯数据的claim这个字段总是有值(没有出现过"非纯数据但完整表述是null"
    的情况);万一某天出现这种极端情况,退回claim自己的原始"text",不让
    整个流程崩掉,但不当成正常路径来设计。"""
    return claim_record.get("完整表述") or claim_record["text"]


def build_prompt(conclusion_content: str, claim_text: str, instances: list, extra_reminder: str = "") -> str:
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    instances_block = json.dumps(instances, ensure_ascii=False, indent=2)
    body = (
        "\n\n=== Conclusion's original text ===\n" + conclusion_content
        + "\n\n=== Claim being tested ===\n" + claim_text
        + "\n\n=== All instances in this conclusion ===\n" + instances_block
    )
    return template.rstrip("\n") + body + extra_reminder


def check_claim_against_instances(conclusion_content: str, claim_text: str, instances: list) -> tuple:
    """返回 (contains(bool), status字符串)。"""
    raw_answer = call_claude(
        build_prompt(conclusion_content, claim_text, instances),
        model=MODEL, thinking=THINKING, max_tokens=MAX_TOKENS,
    )
    result = _judge_bool(raw_answer) if raw_answer is not None else None
    status = "ok"

    if result is None and raw_answer is not None:
        raw_answer = call_claude(
            build_prompt(conclusion_content, claim_text, instances, RETRY_REMINDER),
            model=RETRY_MODEL, thinking=RETRY_THINKING, max_tokens=RETRY_MAX_TOKENS,
        )
        result = _judge_bool(raw_answer) if raw_answer is not None else None
        status = "ok_after_retry_sonnet5_nothink"

    if result is None:
        # 保守兜底选True——方向跟step2a相反,是使用方按这一步的下游用途
        # 明确要的,不套用"错误标True代价更大"那套理由。
        result = True
        status = "fallback_trivial"

    return result, status


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    organized_path = paper_dir / "organized_content.json"
    claim_analysis_path = paper_dir / "claim_completeness_analysis.json"

    if not organized_path.exists():
        print(f"找不到文件: {organized_path}(需要先跑完 step1b_organize_labeled_content.py)")
        sys.exit(1)
    if not claim_analysis_path.exists():
        print(f"找不到文件: {claim_analysis_path}(需要先跑完 step2a_check_pure_data.py / step2b_complete_claims.py)")
        sys.exit(1)

    conc_instances = load_conclusion_instances(organized_path)
    records = json.loads(claim_analysis_path.read_text(encoding="utf-8"))

    n_pure = sum(1 for r in records if r.get("是纯实验数据"))
    n_no_instance = sum(
        1 for r in records
        if not r.get("是纯实验数据") and r["conclusion"] not in conc_instances
    )
    n_to_test = len(records) - n_pure - n_no_instance
    print(
        f"共 {len(records)} 条claim:{n_to_test} 条需要调API测试,"
        f"{n_pure} 条是纯数据跳过,{n_no_instance} 条所在conclusion无instance跳过"
    )

    for r in records:
        if r.get("是纯实验数据"):
            r["instance_containment"] = None
            r["instance_containment_status"] = "skipped_pure_data"
        else:
            conc_short = r["conclusion"]
            if conc_short not in conc_instances:
                r["instance_containment"] = None
                r["instance_containment_status"] = "skipped_no_instances"
            else:
                content = conc_instances[conc_short]["content"]
                instances = conc_instances[conc_short]["instances"]
                claim_text = complete_text_of(r)

                contains, status = check_claim_against_instances(content, claim_text, instances)
                r["instance_containment"] = contains
                r["instance_containment_status"] = status

                print(f"  {conc_short} claim[{r['number']}] -> {status}: {contains}")

        # 不管这一条走的是跳过分支还是真的调了API,每处理完一条就整体
        # 重写一次——之前这行代码放在两个continue之后,导致"一条claim都
        # 没跑,全是跳过"的论文永远不会触发这次写入,文件在磁盘上其实
        # 什么都没变,但下面的完成提示还是照样打印"已写入"。
        claim_analysis_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    n_true = sum(1 for r in records if r.get("instance_containment") is True)
    n_fallback = sum(1 for r in records if r.get("instance_containment_status") == "fallback_trivial")
    print(
        f"\n全部处理完成,共 {n_true} 条claim被判定包含了某个instance的举例内容"
        f"(其中{n_fallback}条走了保守兜底),已写入 {claim_analysis_path}"
    )


if __name__ == "__main__":
    main()
