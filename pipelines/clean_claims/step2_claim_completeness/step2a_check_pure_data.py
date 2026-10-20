"""
step2a_check_pure_data.py

用法:
    python step2a_check_pure_data.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

这是 step2(claim 语义完整性分析,参照 step1_process_conclusions/
claim_semantic_completeness_analysis.json 里已有的字段体系)的第一个子步骤,
只负责其中一个字段——"是纯实验数据"。

============================== 为什么单独拆出这一步 ==============================
参照那份已有结果文件反推出的完整字段体系是一个三层分支结构(详见下面
"最终目标schema"一节):最外层先判断"是纯实验数据",如果是,其余7个字段
全部不适用(留空);如果不是,才继续判断"需要重构"以及"缺主语/缺谓语/
缺状语"等。"是纯实验数据"这一判断只需要看 claim 自己的文字——不需要
这个 conclusion 里其它 part 的上下文,不需要跨片段的指代消解——是这套
分支里最简单、最"局部"的一步,所以：
  - 第一档用便宜快的 Haiku(开着思考);只有Haiku也判不出来时才升级到
    Sonnet 5,只有两档,不是 step1a 那种 Sonnet5思考xhigh->Opus5->Sonnet5
    三档(那一套还要在中途换到Opus这个更贵的模型,是给"分段+11类标签+
    编号+交叉引用"这种结构复杂、光换同一档模型的思考深度都扳不回来的
    任务准备的;这里的失败模式更轻,同一个"小档位不行就上探一档更强的"
    思路够用,不需要再插一档中间的模型)。
  - 可以先于其它字段独立完成、独立复用:不管后续"需要重构"等字段用什么
    模型、什么prompt,都不需要重新判断哪些claim是纯数据。

后续字段(需要重构/缺主语/缺谓语/缺状语/重构内容/遗失的主语/遗失的谓语/
遗失的状语)计划在后续脚本(比如 step2b_xxx.py)里实现,会需要同一个
conclusion 内其它 part(包括非claim类型,比如 elaboration/framing)的上
下文来做指代消解,与这一步的性质不同,留到那时候再设计,这个文件目前
不管。

============================== 最终目标schema(供参考,这一步只填第一项) ==============================
    是纯实验数据 (bool,必填)
    ├─ True  -> 其余7个字段全部是 null
    └─ False -> 需要重构 (bool)
        ├─ True  -> 缺主语/缺谓语/缺状语/遗失的X 全部是 null,只填"重构内容"
        └─ False -> 重构内容 = null;缺主语/缺谓语/缺状语各自独立判断,
                     每个为True时对应的"遗失的X"给出去哪个part(引用同
                     conclusion内其它part,格式如"claim[6]的主语..."或
                     "elaboration[1]所述的...")能找回缺失内容的说明
这套分支是从 claim_semantic_completeness_analysis.json 全部133条记录里
反推、并逐条验证过(0处违反)得到的,不是猜测。

============================== 处理逻辑 ==============================
  1. 读 data/<paper_id>/organized_content.json(step1b 的产物),按
     conclusion 分组,抽出每个 conclusion 里 label 属于
     assertion/论据/example(即"claim")的 part(number/label/content)。
     不含claim的conclusion直接跳过,不调用API。
  2. 对每个含claim的conclusion:把这些claim的 number+label+content 拼进
     prompt模板(step2a_prompt_template.txt),调 Claude 要求逐个编号给出
     "是纯实验数据"的布尔判断。
  3. 用 pure_data_validator.validate_pure_data_response 校验,两档模型:
       - 第一档(Haiku,开着思考)通过 -> status="ok"
       - 第一档拿到了回复但没通过(格式/编号对不上)-> 换第二档:
         Sonnet 5,关闭思考,max_tokens=10000,prompt末尾加格式提醒;
         通过 -> status="ok_after_retry_sonnet5_nothink"
       - 第一档raw_answer是None(被跳过)-> 不升档,直接进兜底(升档是为了
         应对"格式不对"这种模型能力问题,调用本身被跳过不是模型能力问题,
         升档也解决不了)
       - 第二档也没通过(不管是格式不对还是被跳过)-> 保守兜底:这个
         conclusion里所有claim的"是纯实验数据"一律记为 False。选False
         而不是True作为兜底值,是因为两种误判的后果不对称——错误地标记为
         "纯数据"会让这条claim彻底跳过后续所有语义完整性检查(静默丢失
         分析),而错误地标记为"不是纯数据"只是让它继续走后续检查、最多
         是多分析一次,不会丢数据。status="fallback_trivial"(跟
         step1a_pipeline.py里同名兜底状态的叫法保持一致)。
  4. 每处理完一个conclusion,就把当前累积的完整结果列表整体重写一次到
     data/<paper_id>/claim_completeness_analysis.json,防止中途被打断
     丢失进度(同 step1a_pipeline.py 的做法)。

============================== 输出格式 ==============================
data/<paper_id>/claim_completeness_analysis.json,一个列表,每个claim一条:
    {
      "conclusion": "conclusion_2",
      "number": 6,
      "label": "assertion",
      "text": "Under the described IMP regime, the Trojan score is ...",
      "是纯实验数据": false,
      "pure_data_status": "ok"
    }
"label" 和 "pure_data_status" 是这一步自己需要的过程字段,参考文件
claim_semantic_completeness_analysis.json 里没有它们;等后续脚本把其余
7个字段都补齐、汇总成最终对照文件时,再决定要不要去掉这两个字段。

路径解析基于本文件自身位置,没有写死绝对路径。预期本文件放在
step2_claim_completeness/ 目录下,跟 step1_process_conclusions/ 同级、
跟 claude_api_call.py 相差一层(即 paper_graph2ir/step2_claim_completeness/
本文件,paper_graph2ir/claude_api_call.py):

    paper_graph2ir/
    ├── claude_api_call.py
    ├── data/
    │   └── <paper_id>/
    │       ├── organized_content.json          (step1b 产物,本脚本的输入)
    │       └── claim_completeness_analysis.json (本脚本的输出)
    ├── step1_process_conclusions/
    └── step2_claim_completeness/
        ├── step2a_check_pure_data.py            <- 本文件
        ├── step2a_prompt_template.txt
        └── pure_data_validator.py
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

for _p in (PROJECT_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from claude_api_call import call_claude  # noqa: E402  (需要先把PROJECT_ROOT加进sys.path再import)
from pure_data_validator import validate_pure_data_response  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
PROMPT_TEMPLATE_PATH = SCRIPT_DIR / "step2a_prompt_template.txt"

CLAIM_LABELS = ("assertion", "论据", "example")

# 这一步是"读一段文字、判断除了数据罗列外有没有多断言别的东西"的分类,
# 边界并不总是表面模式就能看出来(实践中调prompt时就碰到过好几处反直觉的
# 边界情况),第一档开着思考让Haiku先读懂这句话的语法结构再判断;判不出来
# 才换到更强的Sonnet 5(见下面RETRY_MODEL),不是同一个模型反复试。
MODEL = "claude-haiku-4-5-20251001"
# Haiku 4.5 只支持"手动模式"扩展思考(thinking.type="enabled"+budget_tokens),
# 不支持"自适应模式"(thinking.type="adaptive")——传adaptive会直接400。也没有
# effort这种深度档位可选(effort目前只有Opus 4.5这一个"仅支持扩展思考"的
# 模型能用),深度纯粹靠budget_tokens这个数字自己定。4096是给这个"判断有没有
# 额外断言"的简单分类任务留出的余量,不是照抄哪个官方推荐值。
THINKING = {"type": "enabled", "budget_tokens": 4096}

# 输出是"编号,布尔值;编号,布尔值;..."这种纯文本,不是JSON,单条最多几十个
# 字符;但开着思考之后,真正占用token的是思考过程而不是最终答案,所以这里
# 给的余量比"只按最终答案估"要大得多,避免思考被截断导致拿不到最终答案。
MAX_TOKENS = 8000

# 第一档(Haiku思考)判不出来时,升级到这一档:Sonnet 5,关闭思考,
# max_tokens=10000。不开思考是因为如果连Sonnet 5这个更强的模型都要靠
# 思考才能做对这么简单的一个分类判断,说明问题大概率出在prompt本身
# 没说清楚,而不是模型算力不够,不该指望靠"再多想会儿"来掩盖这一点。
RETRY_MODEL = "claude-sonnet-5"
RETRY_THINKING = {"type": "disabled"}
RETRY_MAX_TOKENS = 10000

RETRY_REMINDER = (
    "\n\nIMPORTANT: your previous reply did not follow the required output "
    "format, or did not include exactly one entry per claim listed above "
    "(with no additions, omissions, or duplicates). Please strictly follow "
    "the exact format specified above (<int>, <true or false>; ...), with "
    "exactly one entry per claim number listed, and output nothing else - "
    "no explanation, no extra text before or after."
)


def group_claims_by_conclusion(organized_content_path: Path) -> list[tuple[str, list[dict]]]:
    """返回 [(conclusion短id, [claim_dict, ...]), ...]。

    顺序:conclusion 按它们在 organized_content.json 里出现的顺序;每个
    conclusion 内的 claim 按 organized_parts 原有顺序(已经是按原文位置
    排好的)。只保留至少含一个 claim 的 conclusion——没有 claim 的
    conclusion 没有本步骤要判断的东西,直接跳过、不产生任何输出条目。

    每个 claim_dict: {"number": int, "label": str, "text": str}
    ("text" 就是 organized_parts 里的 "content" 字段,原样使用,不做任何
    拼接/加工——不复刻参考文件里偶尔出现的"[label supports/illustrates N]"
    这种前缀,因为核对下来那个前缀在参考文件里出现得并不一致,更像是当初
    生成过程里的偶然产物,不是一条应该被复现的规则。)
    """
    conclusions = json.loads(organized_content_path.read_text(encoding="utf-8"))
    groups = []
    for c in conclusions:
        conc_short = c["id"].split("::")[-1]
        claims = [
            {"number": p["number"], "label": p["label"], "text": p["content"]}
            for p in c["organized_parts"]
            if p["label"] in CLAIM_LABELS
        ]
        if claims:
            groups.append((conc_short, claims))
    return groups


def build_prompt(claims: list[dict], extra_reminder: str = "") -> str:
    """读模板文件 + 把claims列表格式化成"[编号] text"逐行列出(+可选的强调
    提醒),拼成最终输入文本。只写编号和内容,不写label——"是纯实验数据"
    这个判断跟claim是assertion/论据/example哪个子类型无关,label对这个
    判断没有帮助,写进去只是无关信息(label字段本身还留在claim_dict里,
    main()写最终输出文件时仍然会用到,只是不出现在发给模型的prompt里)。

    这里的"编号"是调用方(process_conclusion_claims)已经换算成的局部连续
    编号,不是organized_content.json里原本可能带跳号的全局编号——build_prompt
    本身不关心这一层换算,只管把传进来的claims原样格式化。"""
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    lines = [f"[{c['number']}] {c['text']}" for c in claims]
    return template.rstrip("\n") + "\n" + "\n".join(lines) + extra_reminder


def _assign_local_numbers(claims: list[dict]) -> tuple[list[dict], dict]:
    """claims已经按原文位置顺序排列(group_claims_by_conclusion保证的)。
    返回 (本地编号版的claims副本, local_to_global映射dict)。本地编号从1
    开始连续分配,不是organized_content.json里原本的全局number(那个number
    是跟其它八类非claim part混在一起、按位置统一重新编号的,同一个
    conclusion里claim的号可能是6/9/11这种带跳号的样子)。

    为什么要单独做这一层换算,而不是直接把全局number发给模型:LLM处理
    连续、无跳号的整数编号,比处理带跳号的编号更不容易在过程中漏答/
    编错号。这里换算成的局部编号,已经用真实数据核实过跟这些claim在
    step1a阶段(合并/重新按位置编号之前,那时候只有claim才占编号池,
    天然就是从1开始连续的)的原始编号完全一致,所以不需要改step1b、也
    不需要在organized_content.json里新增任何字段,这里现算就行。
    """
    local_to_global = {}
    local_claims = []
    for local_num, c in enumerate(claims, start=1):
        local_to_global[local_num] = c["number"]
        local_claims.append({**c, "number": local_num})
    return local_claims, local_to_global


def process_conclusion_claims(claims: list[dict]) -> tuple[dict, str]:
    """对一个conclusion里的claim列表,调API判断"是纯实验数据"。两档模型:
    第一档Haiku(开着思考)没通过校验(但确实拿到了回复,不是被跳过)时,
    换第二档Sonnet 5(关闭思考,max_tokens=10000)重试一次;仍不通过就
    保守兜底,全部记为False。

    发给模型的prompt、以及校验时用的编号,都是_assign_local_numbers()
    换算出的局部连续编号,不是claims里自带的全局number;返回给调用方
    之前会换算回全局number,所以本函数对外的输入输出契约(claims用什么
    编号,返回的dict就用什么编号)不受这层内部换算影响。

    返回 (number(全局) -> 是纯实验数据 的dict, status字符串)。
    """
    local_claims, local_to_global = _assign_local_numbers(claims)
    expected_local_numbers = set(local_to_global.keys())

    # 第一档: Haiku,开着思考
    raw_answer = call_claude(
        build_prompt(local_claims),
        model=MODEL,
        thinking=THINKING,
        max_tokens=MAX_TOKENS,
    )
    result = validate_pure_data_response(raw_answer, expected_local_numbers)
    status = "ok"

    if result is None and raw_answer is not None:
        # Haiku拿到了回复但格式/编号不对 -> 第二档: Sonnet 5,关闭思考,
        # max_tokens=10000,prompt末尾加格式提醒
        raw_answer = call_claude(
            build_prompt(local_claims, RETRY_REMINDER),
            model=RETRY_MODEL,
            thinking=RETRY_THINKING,
            max_tokens=RETRY_MAX_TOKENS,
        )
        result = validate_pure_data_response(raw_answer, expected_local_numbers)
        status = "ok_after_retry_sonnet5_nothink"

    if result is None:
        # 两档都没能拿到可用结果(不管是哪一档被跳过,还是两档都没通过
        # 格式/编号校验)-> 保守兜底,全部记为False
        # (选False而不是True的理由见模块docstring"处理逻辑"第3步)
        result = {n: False for n in expected_local_numbers}
        status = "fallback_trivial"

    # 换算回全局编号再返回,调用方(main())不需要知道局部编号这层存在
    return {local_to_global[local_num]: v for local_num, v in result.items()}, status


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    organized_path = paper_dir / "organized_content.json"

    if not organized_path.exists():
        print(f"找不到文件: {organized_path}(需要先跑完 step1b_organize_labeled_content.py)")
        sys.exit(1)

    groups = group_claims_by_conclusion(organized_path)
    total_claims = sum(len(claims) for _, claims in groups)
    print(f"共 {len(groups)} 个conclusion包含claim,合计 {total_claims} 条claim")

    out_path = paper_dir / "claim_completeness_analysis.json"
    results = []

    for conc_short, claims in groups:
        print(f"\n处理 {conc_short}({len(claims)}条claim) ...")
        judged, status = process_conclusion_claims(claims)

        for c in claims:
            results.append({
                "conclusion": conc_short,
                "number": c["number"],
                "label": c["label"],
                "text": c["text"],
                "是纯实验数据": judged[c["number"]],
                "pure_data_status": status,
            })

        # 每处理完一个conclusion就整体重写一次,避免中途被打断导致进度丢失
        out_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  -> 状态: {status}")

    n_fallback = sum(1 for r in results if r["pure_data_status"] == "fallback_trivial")
    print(
        f"\n全部处理完成,共 {len(results)} 条claim"
        f"(其中{n_fallback}条走了保守兜底),已写入 {out_path}"
    )


if __name__ == "__main__":
    main()
