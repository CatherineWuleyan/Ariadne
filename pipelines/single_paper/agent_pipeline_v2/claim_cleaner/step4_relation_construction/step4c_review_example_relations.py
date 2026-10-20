"""
step4c_review_example_relations.py

用法:
    python step4c_review_example_relations.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

step4(relation构建)的第三步:审查论据/example关系(不审查relation-label
本身那种逻辑关系,也不审查step4b加的instance关系)——如果一个论据/example
关系"[m] 是 (...) 的例子或证据"里,有某个target claim已经有实例化版本,
就问模型:这条关系连去一般版本的target更直接,还是连去实例化版本更直接;
按模型的判断,把这个target换成更直接的那个版本,更新relation本身。

============================== 哪些relation需要审查 ==============================
只审查 claim_final.json 里 expression以"的例子或证据"结尾的relation(这是
step4a给论据/example关系统一定的格式,靠这个后缀就能跟原生relation-label
的逻辑关系、以及step4b的"...的instance"关系区分开)。而且只审查其中
至少有一个target(connects[1:]里的某个编号)已经有实例化版本的——完全没有
target带实例化版本的论据/example关系,没什么好审的,跳过。

============================== 怎么知道某个claim有没有实例化版本 ==============================
不需要额外的映射文件,直接从relation列表里反查:step4b给每条实例化claim
都加了一条"[实例化编号] 是 [原编号] 的instance"这样的relation,扫一遍
所有以"的instance"结尾、且connects恰好2个编号的relation,就能建出
{原编号: 实例化编号}这张表。

============================== 为什么这个审查天然不怕重复跑 ==============================
一条论据/example关系一旦被审查过、某个target被换成了实例化编号,它在
connects里就不再是"原编号"而是"实例化编号"了——而实例化编号只会出现在
上面那张表的值里,不会是键,所以下一次扫描的时候,这条关系不会再被判定
为"还有target带实例化版本可选",自然不会被重复审查、重复问一遍。不需要
另外加一个"是否已审查过"的标记字段。

============================== 给模型看什么、问什么 ==============================
按conclusion批量(同一个conclusion里全部需要审查的关系一次性问),给:
  - 这个conclusion的原始content
  - 每条待审relation:claim m自己的文本(取自claims_final.json的claim
    列表,是已经完成过引用改写的版本),以及它每一个"有实例化版本可选"的
    target分别的:一般版本文本+实例化版本文本(两者都取自claims_final
    .json的claim列表,不回头翻更早的原始数据)
问的问题:claim m是在说target的实例化版本里那些具体例子,还是在说target
的一般词语——前者连实例化版本更直接,后者连一般版本更直接,拿不准就选
一般版本(跟你确认过的兜底方向一致,不是我自己定的)。

一条relation如果有多个target带实例化版本可选,每个target的选择是独立
判断的,不要求整条relation里所有target必须选同一边。

============================== 模型/兜底/max_tokens ==============================
第一档(Sonnet 5,思考深度high)是你明确指定的。第二、三档以及最终的保守
兜底完全沿用 step2d_remove_instance_content.py 的机制(Opus 5关闭思考->
Sonnet 5关闭思考->保守兜底),不是你这次要求的,是我按项目里已经用过两次
的既定做法补上的一套升级链,不引入新的设计——如果你想要不一样的升级
策略,告诉我改。保守兜底的方向是"全部target保持一般版本不动"(跟"拿不准
选一般版本"这条方向一致,不是另外定的)。

============================== 校验 ==============================
只做格式和编号完整性校验,不检查判断内容对不对(跟step2d/step3c一样的
理由:内容质量校验容易把模型判对的结果误判成没判对)。校验的是:
  - 合法JSON,"results"是列表
  - 每条有"m"(int)和"decisions"(列表);"m"的集合要跟这批送审的relation
    的m集合完全一致(不多不少不重复)
  - 每条"decisions"里每项有"n"(int)和"use_instantiated"(bool);"n"的
    集合要跟这条relation自己"有实例化版本可选"的target集合完全一致

============================== 处理逻辑 ==============================
  1. 读 claims_final.json,建 claim_number -> text 的查找表,和
     {原编号: 实例化编号} 的映射表。
  2. 找出全部需要审查的论据/example关系,按conclusion分组。
  3. 每个conclusion一次API调用,拿到每个target该选哪个版本的判断。
  4. 按判断重建每条relation的connects/expression(用跟step4a
     build_relations_from_claim_links一样的单目标/多目标expression格式:
     只有1个最终target时不带括号,多个时带括号+"和"连接),原地更新
     claims_final.json里对应的relation条目(不改变它在列表里的位置,
     conclusion字段不变)。
  5. 写回 claims_final.json。

路径解析基于本文件自身位置,预期跟 step4a/step4b 同放在
step4_relation_construction/ 目录下;需要从 step2_claim_completeness/
额外import三档模型的常量,复用同一套设定,不重新写一份数值。
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
STEP2_DIR = PROJECT_ROOT / "step2_claim_completeness"
DATA_DIR = PROJECT_ROOT / "data"

for _p in (PROJECT_ROOT, SCRIPT_DIR, STEP2_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from claude_api_call import call_claude  # noqa: E402
from step2d_remove_instance_content import (  # noqa: E402
    MODEL, THINKING, MAX_TOKENS,
    RETRY_MODEL_1, RETRY_THINKING_1, RETRY_MAX_TOKENS_1,
    RETRY_MODEL_2, RETRY_THINKING_2, RETRY_MAX_TOKENS_2,
)

PROMPT_TEMPLATE_PATH = SCRIPT_DIR / "step4c_review_example_relations_prompt_template.txt"

RETRY_REMINDER = (
    "\n\nIMPORTANT: your previous reply did not follow the required JSON "
    "format, or did not include exactly one entry per relation (matching "
    '"m") with exactly one decision per target choice (matching "n") - no '
    "additions, omissions, or duplicates at either level. Output ONLY a "
    'JSON object of the exact form {"results": [{"m": ..., "decisions": '
    '[{"n": ..., "use_instantiated": ...}, ...]}, ...]} with no markdown '
    "fences and no extra text."
)

_EXAMPLE_EXPRESSION_SUFFIX = "的例子或证据"
_INSTANCE_EXPRESSION_SUFFIX = "的instance"


def load_template_sections(path: Path) -> dict:
    """把 ===SECTION_NAME=== 分节的模板文件解析成 {SECTION_NAME: 文本}。"""
    sections = {}
    current_name = None
    current_lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("===") and stripped.endswith("===") and len(stripped) > 6:
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines).strip()
            current_name = stripped.strip("=")
            current_lines = []
        else:
            current_lines.append(line)
    if current_name is not None:
        sections[current_name] = "\n".join(current_lines).strip()
    return sections


def build_instantiation_lookup(relations: list) -> dict:
    """{原claim编号: 实例化claim编号},从relation列表里"[x] 是 [n] 的
    instance"这种(step4b产出的)关系反推出来。"""
    lookup = {}
    for r in relations:
        if r["expression"].endswith(_INSTANCE_EXPRESSION_SUFFIX) and len(r["connects"]) == 2:
            instantiated_num, original_num = r["connects"]
            lookup[original_num] = instantiated_num
    return lookup


def find_candidates(claims_final: dict, instantiation_lookup: dict) -> list:
    """找出全部需要审查的relation,每个候选是:
    {"relation_index", "conclusion", "m", "original_targets",
     "targets_with_alt"}
    relation_index是它在claims_final["relation"]列表里的下标(后面原地
    更新用);original_targets是这条关系当前全部target(含没有实例化版本
    可选的那些);targets_with_alt是其中"有实例化版本可选"的子集,只有
    这部分需要问模型。"""
    candidates = []
    for i, r in enumerate(claims_final["relation"]):
        if not r["expression"].endswith(_EXAMPLE_EXPRESSION_SUFFIX):
            continue
        m = r["connects"][0]
        original_targets = r["connects"][1:]
        targets_with_alt = [n for n in original_targets if n in instantiation_lookup]
        if not targets_with_alt:
            continue
        candidates.append({
            "relation_index": i,
            "conclusion": r["conclusion"],
            "m": m,
            "original_targets": original_targets,
            "targets_with_alt": targets_with_alt,
        })
    return candidates


def group_candidates_by_conclusion(candidates: list) -> dict:
    grouped = {}
    for c in candidates:
        grouped.setdefault(c["conclusion"], []).append(c)
    return grouped


def build_prompt(conclusion_content: str, candidates: list, claim_text_by_number: dict,
                  instantiation_lookup: dict, extra_reminder: str = "") -> str:
    sections = load_template_sections(PROMPT_TEMPLATE_PATH)
    parts = [sections["INTRO"], sections["CONCLUSION_LEADIN"], conclusion_content]

    for c in candidates:
        m = c["m"]
        parts.append(sections["RELATION_LEADIN"].format(m=m))
        parts.append(sections["CLAIM_M_LEADIN"].format(m=m))
        parts.append(claim_text_by_number[m])
        for n in c["targets_with_alt"]:
            parts.append(sections["TARGET_CHOICE_LEADIN"].format(m=m, n=n))
            instantiated_n = instantiation_lookup[n]
            parts.append(json.dumps({
                "general_version": claim_text_by_number[n],
                "instantiated_version": claim_text_by_number[instantiated_n],
            }, ensure_ascii=False, indent=2))

    parts.append(sections["OUTPUT_FORMAT"])
    return "\n\n".join(parts) + extra_reminder


def _parse_and_validate_review(raw_answer, candidates: list):
    """校验规则见模块文档"校验"一节。返回
    {m: {n: use_instantiated(bool), ...}, ...} 或 None。"""
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

    expected_by_m = {c["m"]: set(c["targets_with_alt"]) for c in candidates}
    output = {}

    for item in parsed["results"]:
        if not isinstance(item, dict):
            return None
        m = item.get("m")
        decisions = item.get("decisions")
        if not isinstance(m, int) or isinstance(m, bool):
            return None
        if m not in expected_by_m or m in output:
            return None
        if not isinstance(decisions, list):
            return None

        per_n = {}
        for d in decisions:
            if not isinstance(d, dict):
                return None
            n = d.get("n")
            use_instantiated = d.get("use_instantiated")
            if not isinstance(n, int) or isinstance(n, bool):
                return None
            if not isinstance(use_instantiated, bool):
                return None
            if n in per_n:
                return None
            per_n[n] = use_instantiated

        if set(per_n.keys()) != expected_by_m[m]:
            return None

        output[m] = per_n

    if set(output.keys()) != set(expected_by_m.keys()):
        return None

    return output


def review_conclusion(conclusion_content: str, candidates: list, claim_text_by_number: dict,
                       instantiation_lookup: dict) -> tuple:
    """返回 ({m: {n: use_instantiated, ...}, ...}, status字符串)。三档
    模型依次升级,机制照抄
    step2d_remove_instance_content.process_conclusion_candidates。"""
    fallback = {c["m"]: {n: False for n in c["targets_with_alt"]} for c in candidates}

    raw_answer = call_claude(
        build_prompt(conclusion_content, candidates, claim_text_by_number, instantiation_lookup),
        model=MODEL, thinking=THINKING, max_tokens=MAX_TOKENS,
    )
    result = _parse_and_validate_review(raw_answer, candidates)
    status = "ok"

    if result is None and raw_answer is not None:
        raw_answer = call_claude(
            build_prompt(conclusion_content, candidates, claim_text_by_number,
                         instantiation_lookup, RETRY_REMINDER),
            model=RETRY_MODEL_1, thinking=RETRY_THINKING_1, max_tokens=RETRY_MAX_TOKENS_1,
        )
        result = _parse_and_validate_review(raw_answer, candidates)
        status = "ok_after_opus5_nothink"

        if result is None and raw_answer is not None:
            raw_answer = call_claude(
                build_prompt(conclusion_content, candidates, claim_text_by_number,
                             instantiation_lookup, RETRY_REMINDER),
                model=RETRY_MODEL_2, thinking=RETRY_THINKING_2, max_tokens=RETRY_MAX_TOKENS_2,
            )
            result = _parse_and_validate_review(raw_answer, candidates)
            status = "ok_after_sonnet5_nothink"

    if result is None:
        result = fallback  # 保守兜底:全部target保持一般版本不动
        status = "fallback_general"

    return result, status


def apply_decisions(candidate: dict, decisions: dict) -> dict:
    """按decisions({n: use_instantiated})算出这条relation最终的target
    列表,重建connects/expression(格式规则跟step4a
    build_relations_from_claim_links一样:1个target不带括号,多个带括号+
    "和"连接)。"""
    instantiation_lookup = candidate["_instantiation_lookup"]
    final_targets = []
    for n in candidate["original_targets"]:
        if n in decisions and decisions[n]:
            final_targets.append(instantiation_lookup[n])
        else:
            final_targets.append(n)

    m = candidate["m"]
    if len(final_targets) == 1:
        expression = f"[{m}] 是 [{final_targets[0]}] 的例子或证据"
    else:
        joined = " 和 ".join(f"[{n}]" for n in final_targets)
        expression = f"[{m}] 是 ({joined}) 的例子或证据"

    return {"connects": [m] + final_targets, "expression": expression}


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    for fname in ("claims_final.json", "organized_content.json"):
        if not (paper_dir / fname).exists():
            print(f"找不到文件: {paper_dir / fname}")
            sys.exit(1)

    claims_final = json.loads((paper_dir / "claims_final.json").read_text(encoding="utf-8"))
    conclusion_contents = {
        c["id"].split("::")[-1]: c["content"]
        for c in json.loads((paper_dir / "organized_content.json").read_text(encoding="utf-8"))
    }
    claim_text_by_number = {c["number"]: c["text"] for c in claims_final["claim"]}

    instantiation_lookup = build_instantiation_lookup(claims_final["relation"])
    candidates = find_candidates(claims_final, instantiation_lookup)

    if not candidates:
        print("没有需要审查的论据/example关系(要么没有实例化claim,要么它们都不是任何关系的target)。")
        return

    for c in candidates:
        c["_instantiation_lookup"] = instantiation_lookup

    grouped = group_candidates_by_conclusion(candidates)
    print(f"{len(grouped)} 个conclusion里有需要审查的关系,合计 {len(candidates)} 条")

    n_switched = 0
    for conc_short, conc_candidates in grouped.items():
        decisions_by_m, status = review_conclusion(
            conclusion_contents[conc_short], conc_candidates, claim_text_by_number, instantiation_lookup
        )

        for c in conc_candidates:
            decisions = decisions_by_m[c["m"]]
            updated = apply_decisions(c, decisions)
            claims_final["relation"][c["relation_index"]]["connects"] = updated["connects"]
            claims_final["relation"][c["relation_index"]]["expression"] = updated["expression"]
            if any(decisions.values()):
                n_switched += 1

        print(f"  {conc_short}({len(conc_candidates)}条待审) -> {status}")

        (paper_dir / "claims_final.json").write_text(
            json.dumps(claims_final, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(f"\n全部完成,{n_switched} 条关系至少有一个target换成了实例化版本,已写回 {paper_dir / 'claims_final.json'}")


if __name__ == "__main__":
    main()
