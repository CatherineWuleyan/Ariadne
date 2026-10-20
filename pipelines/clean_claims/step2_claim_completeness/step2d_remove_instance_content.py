"""
step2d_remove_instance_content.py

用法:
    python step2d_remove_instance_content.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

step2(claim 语义完整性分析)的第四个子步骤:对 step2c 判定
instance_containment==True 的claim(疑似混入了instance内容),按conclusion
批量复核——同一个conclusion里全部疑似claim一起发给Sonnet 5(思考深度high)
一次判断,而不是一条claim调一次API。

============================== 为什么按conclusion批量、且要"复核" ==============================
step2c只是一道粗筛(Haiku判断"有没有至少一个instance的内容出现在claim里",
命中一项就算,详见step2c自己的文档)。粗筛的判定标准不足以区分两种性质
不同的情况:
  - claim原生就是在单独讲某个具体事物(比如"Spred initialization在xx条件
    下失败了"),这个具体事物恰好也是另一条claim旁边某个instance列举的
    一项——这不是"混入",两者只是碰巧提到同一个名字,没有语义上的重复。
  - claim的完整表述里,同时写出了某个instance要说明的"一般词语"和这个
    instance本身举的具体例子(比如"...multiple scoring functions, such as
    MSP and MaxLogit")——这才是真正的"混入":一般词语已经说清楚了,后面
    又把instance单独记录的具体例子重复了一遍。

区分这两种情况需要同时看到"conclusion原文+claim完整字段+全部instance",
且需要仔细读claim自己的文字里到底有没有连带一般词语一起出现——这已经
超出step2c那种"一个claim一个孤立判断"能可靠做到的范围,所以单独作为
一步,用更强的模型、更完整的上下文一次性对同一个conclusion里的全部疑似
claim做复核,而不是逐条重复调用。

============================== 处理逻辑 ==============================
  1. 读 organized_content.json,取出每个conclusion的原始content和全部
     instance part(跟step2c一样,拿全部字段)。
  2. 读 claim_completeness_analysis.json,按 instance_containment==True
     筛出候选claim,按conclusion分组(组内按number排序)。
  3. 对每个有候选claim的conclusion:把conclusion原始content、这一组
     候选claim的完整字段(原样带上claim_completeness_analysis.json里
     已有的全部字段,只有"完整表述"这一个字段在prompt里改用键名"text"
     展示、覆盖掉原本"text"键下改写前的版本——prompt里统一用英文字段名,
     不混用中文,也不需要同时展示改写前后两个版本;见
     _candidate_for_prompt)、这个conclusion全部instance的完整字段,
     一次性拼进prompt,调用 claude-sonnet-5(thinking={"type":"adaptive",
     "effort":"high"}),要求对组内每一条claim分别判断是不是真的混入了
     instance内容,是就把instance的具体内容剥掉(只做必要的最小语法
     修补),不是就原样输出不动。
  4. 校验(_parse_and_validate,见函数文档):
       - 回复必须是合法JSON,"results"数组跟这组候选claim的编号一一对应
         (不多不少不重复),每一条number是int、content是非空字符串
       - 只做格式/结构层面的校验,不检查"改写内容是不是真的把instance
         信息删干净了"这类内容质量层面的东西(这一层校验试过,容易把
         Sonnet实际上改对了的结果误判成没改对,反而弄巧成拙——比如
         instance切分成的"子项"跟改写后文本里其它无关词语偶然重合、
         或者一次合理的语法修补超出了预设的长度富余,都会被误杀。所以
         内容对不对完全交给Sonnet自己判断,这里只守住"格式对不对/编号
         全不全"这条底线)
       - 只要有任何一条格式不对,整组判定没通过(跟step1a/step2a"整批
         过/整批不过"的一贯做法一致,不做部分挽救)
       - 没通过但确实拿到了回复(不是被跳过)-> 依次升级两档模型重试
         (Opus 5关闭思考 -> Sonnet 5关闭思考,各自加格式提醒),通过就
         停在那一档
       - 三档都不通过,或某一档调用被跳过 -> 保守兜底:这个conclusion里
         全部候选claim都保持原文不动,status="fallback_unchanged"
  5. 每处理完一个conclusion就把当前累积的完整claim_completeness_analysis
     .json整体重写一次,防止中途被打断丢失进度。

============================== 输出格式 ==============================
claim_completeness_analysis.json里每条claim记录,在已有字段基础上追加:
    {
      ... (已有字段原样保留,包括step2c写的instance_containment /
           instance_containment_status) ...
      "完整表述_不含instance": "...",
      "instance_containment_confirmed": true,
      "instance_removal_status": "ok"
    }
"instance_containment_confirmed"是这一步新增的、比step2c更精确的判断——
true表示复核后确认是真的混入(内容已剥离),false表示复核后判定不是真的
混入(内容原样未变),这两种情况"完整表述_不含instance"都会有值(要么是
剥离后的新内容,要么是跟"完整表述"一样的原内容),不是只有确认混入的才
有值。instance_containment不是True的claim,这三个新字段都是null,
status分别是"skipped_not_contained"(False)/"skipped_not_applicable"(null)。

"完整表述"字段本身不会被覆盖或删除——新结果单独存成一个新字段,保留原始
版本方便对照/回滚,不是权宜之计。

============================== 三档模型 + 保守兜底 ==============================
第一档(Sonnet 5,思考深度high)是用户明确指定的,不是本脚本自己设计出来的
档位选择。第一档拿到了回复但没通过格式校验时,依次升级两档(理由跟
step1a_pipeline.py的三档设计一致:格式不对时先怀疑"这一档模型本身不擅长
这个任务",换一个更强/不同的模型档位,比在同一档反复重试更可能真正解决
问题):
  - 第二档: Opus 5,关闭思考,max_tokens=8000
  - 第三档: Sonnet 5,关闭思考,max_tokens=40000
每一档只有在"确实拿到了回复,但没通过校验"时才会往下一档升级——如果某一
档的调用本身被跳过(raw_answer是None,比如交互式菜单里选了's'),不会继续
往下一档升级(被跳过是人为决定跳过这一条,不是模型能力问题,换档位解决
不了),直接进入最终的保守兜底。三档都没通过(或最终还是被跳过)->
这个conclusion里全部候选claim保持原文不动,status="fallback_unchanged"。

路径解析基于本文件自身位置,预期跟 step2a/step2b/step2c 同放在
step2_claim_completeness/ 目录下。
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from claude_api_call import call_claude  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
PROMPT_TEMPLATE_PATH = SCRIPT_DIR / "step2d_remove_instance_content_prompt_template.txt"

# 第一档:用户明确指定,Sonnet 5,思考深度high
MODEL = "claude-sonnet-5"
THINKING = {"type": "adaptive", "effort": "high"}
MAX_TOKENS = 40000  # 一次要处理同一个conclusion里全部候选claim(可能不止1条)
                     # +全部instance+conclusion原文,比单条claim的情况需要
                     # 更大余量

# 第二档:第一档格式不对时的第一次升级
RETRY_MODEL_1 = "claude-opus-5"
RETRY_THINKING_1 = {"type": "disabled"}
RETRY_MAX_TOKENS_1 = 8000

# 第三档:第二档仍不通过时的第二次升级
RETRY_MODEL_2 = "claude-sonnet-5"
RETRY_THINKING_2 = {"type": "disabled"}
RETRY_MAX_TOKENS_2 = 40000

RETRY_REMINDER = (
    "\n\nIMPORTANT: your previous reply did not follow the required JSON "
    "format, or did not include exactly one entry per claim number listed "
    "above (no additions, omissions, or duplicates). Output ONLY a JSON "
    'object of the exact form {"results": [{"number": ..., "content": ...}, '
    '...]} with no markdown fences and no extra text.'
)


def load_conclusion_instances(organized_content_path: Path) -> dict:
    """{conclusion短id: {"content":..., "instances":[...]}},只保留至少有
    1个instance的conclusion。"""
    conclusions = json.loads(organized_content_path.read_text(encoding="utf-8"))
    result = {}
    for c in conclusions:
        conc_short = c["id"].split("::")[-1]
        instances = [p for p in c["organized_parts"] if p["label"] == "instance"]
        if instances:
            result[conc_short] = {"content": c["content"], "instances": instances}
    return result


def group_candidates_by_conclusion(records: list) -> dict:
    """{conclusion短id: [claim_record,...]},只收instance_containment==True
    的claim,每组内按number升序排列(方便prompt里按顺序展示、也方便回复
    按顺序核对)。"""
    groups = {}
    for r in records:
        if r.get("instance_containment") is True:
            groups.setdefault(r["conclusion"], []).append(r)
    for conc in groups:
        groups[conc].sort(key=lambda r: r["number"])
    return groups


def _candidate_for_prompt(r: dict) -> dict:
    """给prompt展示用的候选claim副本:把"完整表述"这个字段的值改用键名
    "text"展示(prompt里统一用英文字段名,不混用中文),这会覆盖掉原本
    "text"键下改写前的版本——prompt里只需要展示"当前要处理的这一份内容",
    不需要同时展示改写前后两个版本。不改动传入的原始dict,只在这里构造
    一份用于展示的副本,claim_completeness_analysis.json里实际存储的
    字段名不受影响。"""
    d = dict(r)
    if "完整表述" in d:
        d["text"] = d.pop("完整表述")
    return d


def build_prompt(conclusion_content: str, candidates: list, instances: list, extra_reminder: str = "") -> str:
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    candidates_block = json.dumps([_candidate_for_prompt(r) for r in candidates], ensure_ascii=False, indent=2)
    instances_block = json.dumps(instances, ensure_ascii=False, indent=2)
    body = (
        "\n\n=== Conclusion's original text ===\n" + conclusion_content
        + "\n\n=== Suspected claims (full existing fields) ===\n" + candidates_block
        + "\n\n=== All instances in this conclusion ===\n" + instances_block
    )
    return template.rstrip("\n") + body + extra_reminder


def _parse_and_validate(raw_answer, expected_numbers: set):
    """校验规则见模块文档"处理逻辑"第4步:只做JSON格式+编号完整性校验,
    不检查改写内容本身对不对(理由见模块文档)。返回 {number: content, ...}
    或 None(任何一步没通过就返回None,整组一起判失败,不做部分挽救)。"""
    if raw_answer is None:
        return None

    text = raw_answer.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        return None

    output = {}
    for item in parsed["results"]:
        if not isinstance(item, dict):
            return None
        number = item.get("number")
        content = item.get("content")
        if not isinstance(number, int) or isinstance(number, bool):
            return None
        if not isinstance(content, str) or not content.strip():
            return None
        if number in output:
            return None  # 重复编号

        output[number] = content

    if set(output.keys()) != expected_numbers:
        return None

    return output


def process_conclusion_candidates(conclusion_content: str, candidates: list, instances: list) -> tuple:
    """返回 ({number: 最终content, ...}, status字符串)。三档模型依次升级,
    见模块文档"三档模型+保守兜底"一节。"""
    expected_numbers = {r["number"] for r in candidates}
    originals = {r["number"]: (r.get("完整表述") or r["text"]) for r in candidates}

    # 第一档: Sonnet 5, 思考深度high
    raw_answer = call_claude(
        build_prompt(conclusion_content, candidates, instances),
        model=MODEL, thinking=THINKING, max_tokens=MAX_TOKENS,
    )
    result = _parse_and_validate(raw_answer, expected_numbers)
    status = "ok"

    if result is None and raw_answer is not None:
        # 第二档: Opus 5, 关闭思考
        raw_answer = call_claude(
            build_prompt(conclusion_content, candidates, instances, RETRY_REMINDER),
            model=RETRY_MODEL_1, thinking=RETRY_THINKING_1, max_tokens=RETRY_MAX_TOKENS_1,
        )
        result = _parse_and_validate(raw_answer, expected_numbers)
        status = "ok_after_opus5_nothink"

        if result is None and raw_answer is not None:
            # 第三档: Sonnet 5, 关闭思考
            raw_answer = call_claude(
                build_prompt(conclusion_content, candidates, instances, RETRY_REMINDER),
                model=RETRY_MODEL_2, thinking=RETRY_THINKING_2, max_tokens=RETRY_MAX_TOKENS_2,
            )
            result = _parse_and_validate(raw_answer, expected_numbers)
            status = "ok_after_sonnet5_nothink"

    if result is None:
        result = dict(originals)  # 保守兜底:全部保持原文不动
        status = "fallback_unchanged"

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
        print(f"找不到文件: {organized_path}")
        sys.exit(1)
    if not claim_analysis_path.exists():
        print(f"找不到文件: {claim_analysis_path}(需要先跑完 step2c_check_instance_containment.py)")
        sys.exit(1)

    conc_instances = load_conclusion_instances(organized_path)
    records = json.loads(claim_analysis_path.read_text(encoding="utf-8"))

    if not any("instance_containment" in r for r in records):
        print("这份 claim_completeness_analysis.json 里还没有 instance_containment 字段"
              "(需要先跑完 step2c_check_instance_containment.py)")
        sys.exit(1)

    candidate_groups = group_candidates_by_conclusion(records)
    n_candidates = sum(len(v) for v in candidate_groups.values())
    print(f"共 {len(records)} 条claim,{len(candidate_groups)} 个conclusion里有疑似混入的claim,合计 {n_candidates} 条候选")

    # 先把非候选claim的三个新字段填好(跳过的情况)
    for r in records:
        if r.get("instance_containment") is not True:
            r["完整表述_不含instance"] = None
            r["instance_containment_confirmed"] = None
            r["instance_removal_status"] = (
                "skipped_not_contained" if r.get("instance_containment") is False
                else "skipped_not_applicable"
            )

    # 先写一次,不依赖下面这个循环——如果这篇论文里一条候选claim都没有
    # (candidate_groups是空的),下面"for conc_short, candidates in
    # candidate_groups.items()"一次都不会执行,write_text就永远不会被
    # 调用,上面这些跳过字段虽然已经写进了内存里的records,却从来没真的
    # 落盘,但main()末尾的完成提示还是照样打印"已写入"——这里先补一次
    # 写入,保证哪怕没有任何候选,这些跳过字段也是真的写进了文件。
    claim_analysis_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for conc_short, candidates in candidate_groups.items():
        content = conc_instances[conc_short]["content"]
        instances = conc_instances[conc_short]["instances"]

        result, status = process_conclusion_candidates(content, candidates, instances)

        print(f"\n{conc_short}({len(candidates)}条候选) -> {status}")
        for r in candidates:
            original = r.get("完整表述") or r["text"]
            new_content = result[r["number"]]
            confirmed = new_content != original

            r["完整表述_不含instance"] = new_content
            r["instance_containment_confirmed"] = confirmed
            r["instance_removal_status"] = status

            marker = "确认混入,已剥离" if confirmed else "复核后判定不是混入,保持不变"
            print(f"  claim[{r['number']}] -> {marker}")
            if confirmed:
                print(f"    改写前: {original}")
                print(f"    改写后: {new_content}")

        claim_analysis_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    n_confirmed = sum(1 for r in records if r.get("instance_containment_confirmed") is True)
    n_rejected = sum(
        1 for r in records
        if r.get("instance_containment") is True and r.get("instance_containment_confirmed") is False
    )
    n_fallback = sum(1 for r in records if r.get("instance_removal_status") == "fallback_unchanged")
    print(
        f"\n全部完成:{n_confirmed}条claim确认混入并已剥离,"
        f"{n_rejected}条复核后判定不是真的混入(保持不变),"
        f"{n_fallback}条走了保守兜底,已写入 {claim_analysis_path}"
    )


if __name__ == "__main__":
    main()
