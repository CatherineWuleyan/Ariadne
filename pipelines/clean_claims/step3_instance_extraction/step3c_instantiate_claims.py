"""
step3c_instantiate_claims.py

用法:
    python step3c_instantiate_claims.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

step3(instance提取)的第三步:反过来做——从一般性claim导出"实例化"版本的
特殊claim,把claim里被instance举例说明的那个一般性词语,换成instance
给出的具体内容。

============================== 待处理claim怎么定 ==============================
只处理 claim_instance_associations.json(step3b产物)里 certain_instances
或 uncertan_instances 非空的claim——这两个列表里的instance,才是"这条claim
可能提到了某个被instance举例说明的一般性词语"的候选。

============================== 用哪个文本改写 ==============================
claim的"当前内容"取 step2_output_claims.json(step2的最终产物)里的text——
这是已经完成语义补全、且已经把误混入的instance内容剥离干净的自足版本,
在这个"干净"的版本上做实例化替换才有意义,不是在organized_content.json
的原始未处理文本上改。

============================== 按conclusion批量,一次处理该conclusion全部待处理claim ==============================
跟step2d一样,同一个conclusion里全部待处理claim一次性发给模型,不是一条
claim调一次API。

============================== 每条待处理claim,给模型看什么 ==============================
claim自己的当前内容,加上它涉及到的每个instance(certain_instances里的、
uncertan_instances里的)的:
  - instance编号
  - targets:这个instance在step3a里定位到的、已经找到的那段原文文字
    本身(不是字符位置——模型是照着文字去claim里找,不是照着坐标去找)。
    一个instance如果有多个target(现有数据里都只有1个,但代码按"可能有
    多个"设计),这里是把每个target已经确定/候选到的文字去重后列出来。
  - instance_content:这个instance给出的具体例子内容,要拿去替换claim里
    对应的那个词。

certain_instances里的这些instance,prompt里说"这些instance确定给claim里
的一般性词语举例了";uncertain_instances里的,说"这些instance可能给claim
里的一般性词语举例了(没有确认)"。一条claim如果两种都有,两段话都写;
只有一种,就只写对应那一段。

只要这一次要处理的这个conclusion里,所有待处理claim加起来,一个
uncertain_instances都没有(全部claim的uncertain_instances都是空的)——
这个conclusion对应的prompt里,连"如果claim其实没包含instance就原样输出"
这条兜底提醒都不写,因为没有不确定的候选需要这条提醒来兜底。

============================== prompt的固定/可变内容交替结构 ==============================
prompt模板文件(step3c_instantiate_claims_prompt_template.txt)按
===SECTION_NAME=== 分成几段固定文字,代码在INTRO/(可选)UNCERTAIN_CAVEAT/
CONCLUSION_LEADIN之后接上conclusion原文,然后对每一条待处理claim依次接上
CLAIM_LEADIN+这条claim的内容+(如果有)CERTAIN_LEADIN+对应instance列表的
JSON+(如果有)UNCERTAIN_LEADIN+对应instance列表的JSON,最后接OUTPUT_FORMAT
——是"固定文字、可变内容"交替拼接,不是把全部固定文字堆在最前面、可变
内容再一次性跟在后面。

============================== 模型/兜底/max_tokens ==============================
完全沿用step2d_remove_instance_content.py的机制,数值和逻辑都不重新设计:
  - 第一档: Sonnet 5,思考深度high,max_tokens=40000
  - 第二档: Opus 5,关闭思考,max_tokens=8000(第一档拿到回复但没通过校验
    才升级)
  - 第三档: Sonnet 5,关闭思考,max_tokens=40000(第二档拿到回复但没通过
    校验才升级)
  - 某一档调用被跳过(不是格式问题)-> 不升级,直接进最终兜底
  - 三档都不通过 -> 保守兜底:这个conclusion里全部待处理claim都保持
    step2_output_claims.json里的原文不动

============================== 校验 ==============================
直接复用 step2d_remove_instance_content._parse_and_validate——只做格式
(合法JSON、每条number是int/content是非空字符串、无重复编号)和编号完整性
(跟这批候选claim编号一一对应,不多不少)校验,不检查改写内容对不对(原因
跟step2d一样:内容质量校验容易把模型改对的结果误判成没改对)。

============================== 输出前的最后一步:忽略空格的内容变化检测 ==============================
校验通过之后,对每条claim,拿模型给的content跟它原本(改写前)的text比较——
比较时先把两边的空白都折叠成单个空格再比较(_normalize_ws),忽略纯空白
差异,只看是不是真的有实质内容变了。没有变化的claim(不管是因为模型判定
"其实不该改"、还是保守兜底保持原文)不出现在最终输出文件里;只有真的
变了的才留下。

============================== 输出格式 ==============================
data/<paper_id>/special_claims.json,一个列表,只包含真的发生了实例化
改写的claim:
    {
      "conclusion": "conclusion_5",
      "claim_number": 6,
      "text": "Average of Pruning (AoP) also improves Maximum Softmax Probability (MSP) and MaxLogit.",
      "certain_instances_given": [8],
      "uncertain_instances_given": []
    }
"certain_instances_given"/"uncertain_instances_given"是这次prompt里实际
给模型看过的instance编号(不是模型最终用了哪个,只是如实记录给过什么,
方便回溯)。

如果整篇论文一条claim都没有真的发生实例化改写(包括压根没有待处理claim
的情况),仍然会写这个文件,内容是空列表[]——用同名文件+空列表来明确
表示"这篇论文没有特殊claim",而不是直接不产生这个文件,让下游没法区分
"跑过但没有"和"压根没跑过"。

路径解析基于本文件自身位置,预期跟 step3a/step3b 同放在
step3_instance_extraction/ 目录下;需要从 step2_claim_completeness/
额外import _parse_and_validate,复用同一份校验逻辑,不重新写一份。
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
STEP2_DIR = PROJECT_ROOT / "step2_claim_completeness"

for _p in (PROJECT_ROOT, SCRIPT_DIR, STEP2_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from claude_api_call import call_claude  # noqa: E402
from step2d_remove_instance_content import _parse_and_validate  # noqa: E402  # 复用同一套格式+编号校验

DATA_DIR = PROJECT_ROOT / "data"
PROMPT_TEMPLATE_PATH = SCRIPT_DIR / "step3c_instantiate_claims_prompt_template.txt"

# 完全沿用step2d的三档模型/max_tokens设定
MODEL = "claude-sonnet-5"
THINKING = {"type": "adaptive", "effort": "high"}
MAX_TOKENS = 40000

RETRY_MODEL_1 = "claude-opus-5"
RETRY_THINKING_1 = {"type": "disabled"}
RETRY_MAX_TOKENS_1 = 8000

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


def load_conclusion_contents(paper_dir: Path) -> dict:
    """{conclusion短id: 原始content}"""
    conclusions = json.loads((paper_dir / "organized_content.json").read_text(encoding="utf-8"))
    return {c["id"].split("::")[-1]: c["content"] for c in conclusions}


def load_instances_by_key(paper_dir: Path) -> dict:
    """{(conclusion短id, instance_number): instance记录} from
    instance_target_positions.json(step3a产物)。"""
    records = json.loads((paper_dir / "instance_target_positions.json").read_text(encoding="utf-8"))
    return {(r["conclusion"], r["instance_number"]): r for r in records}


def load_claim_texts(paper_dir: Path) -> dict:
    """{(conclusion短id, claim_number): text} from step2_output_claims.json
    (step2最终产物)。"""
    records = json.loads((paper_dir / "step2_output_claims.json").read_text(encoding="utf-8"))
    return {(r["conclusion"], r["number"]): r["text"] for r in records}


def load_candidate_associations(paper_dir: Path) -> dict:
    """{conclusion短id: [association记录,...]},只保留certain_instances或
    uncertain_instances至少有一个非空的claim(待处理claim),来自
    claim_instance_associations.json(step3b产物)。"""
    records = json.loads((paper_dir / "claim_instance_associations.json").read_text(encoding="utf-8"))
    result = {}
    for r in records:
        if r["certain_instances"] or r["uncertain_instances"]:
            result.setdefault(r["conclusion"], []).append(r)
    return result


def instance_target_texts(instance_record: dict) -> list:
    """这个instance全部target已经定位到的文字内容,去重但保留顺序:
    certain的target取它的content,uncertain的取它候选列表里每一个的
    content,not_found的跳过(没有文字可给)。"""
    texts = []
    for t in instance_record["targets"]:
        if t["certainty"] == "certain":
            texts.append(t["content"])
        elif t["certainty"] == "uncertain":
            texts.extend(c["content"] for c in t["candidates"])
    seen = set()
    result = []
    for x in texts:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result


def build_instance_detail(instance_record: dict) -> dict:
    return {
        "instance_number": instance_record["instance_number"],
        "targets": instance_target_texts(instance_record),
        "instance_content": instance_record["instance_content"],
    }


def build_claims_info(conc_short: str, assoc_list: list, claim_texts: dict, instances_by_key: dict) -> list:
    """把这个conclusion的待处理claim整理成
    [{"number", "text", "certain": [...], "uncertain": [...]}, ...],
    certain/uncertain里是build_instance_detail()的结果。"""
    claims_info = []
    for assoc in assoc_list:
        number = assoc["claim_number"]
        certain = [
            build_instance_detail(instances_by_key[(conc_short, n)])
            for n in assoc["certain_instances"]
        ]
        uncertain = [
            build_instance_detail(instances_by_key[(conc_short, n)])
            for n in assoc["uncertain_instances"]
        ]
        claims_info.append({
            "number": number,
            "text": claim_texts[(conc_short, number)],
            "certain": certain,
            "uncertain": uncertain,
        })
    return claims_info


def build_prompt(conclusion_content: str, claims_info: list, extra_reminder: str = "") -> str:
    sections = load_template_sections(PROMPT_TEMPLATE_PATH)
    has_any_uncertain = any(c["uncertain"] for c in claims_info)

    parts = [sections["INTRO"]]
    if has_any_uncertain:
        parts.append(sections["UNCERTAIN_CAVEAT"])
    parts.append(sections["CONCLUSION_LEADIN"])
    parts.append(conclusion_content)

    for claim in claims_info:
        parts.append(sections["CLAIM_LEADIN"].format(number=claim["number"]))
        parts.append(claim["text"])
        if claim["certain"]:
            parts.append(sections["CERTAIN_LEADIN"])
            parts.append(json.dumps(claim["certain"], ensure_ascii=False, indent=2))
        if claim["uncertain"]:
            parts.append(sections["UNCERTAIN_LEADIN"])
            parts.append(json.dumps(claim["uncertain"], ensure_ascii=False, indent=2))

    parts.append(sections["OUTPUT_FORMAT"])
    return "\n\n".join(parts) + extra_reminder


def _normalize_ws(s: str) -> str:
    return " ".join(s.split())


def content_changed(original: str, new: str) -> bool:
    """忽略空白差异后,内容是不是真的不一样。"""
    return _normalize_ws(original) != _normalize_ws(new)


def process_conclusion(conclusion_content: str, claims_info: list) -> tuple:
    """返回 ({number: 最终content, ...}, status字符串)。三档模型依次
    升级,机制完全照抄step2d_remove_instance_content.process_conclusion_
    candidates。"""
    expected_numbers = {c["number"] for c in claims_info}
    originals = {c["number"]: c["text"] for c in claims_info}

    raw_answer = call_claude(
        build_prompt(conclusion_content, claims_info),
        model=MODEL, thinking=THINKING, max_tokens=MAX_TOKENS,
    )
    result = _parse_and_validate(raw_answer, expected_numbers)
    status = "ok"

    if result is None and raw_answer is not None:
        raw_answer = call_claude(
            build_prompt(conclusion_content, claims_info, RETRY_REMINDER),
            model=RETRY_MODEL_1, thinking=RETRY_THINKING_1, max_tokens=RETRY_MAX_TOKENS_1,
        )
        result = _parse_and_validate(raw_answer, expected_numbers)
        status = "ok_after_opus5_nothink"

        if result is None and raw_answer is not None:
            raw_answer = call_claude(
                build_prompt(conclusion_content, claims_info, RETRY_REMINDER),
                model=RETRY_MODEL_2, thinking=RETRY_THINKING_2, max_tokens=RETRY_MAX_TOKENS_2,
            )
            result = _parse_and_validate(raw_answer, expected_numbers)
            status = "ok_after_sonnet5_nothink"

    if result is None:
        result = dict(originals)
        status = "fallback_unchanged"

    return result, status


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    required_files = [
        "organized_content.json",
        "instance_target_positions.json",
        "claim_instance_associations.json",
        "step2_output_claims.json",
    ]
    for fname in required_files:
        if not (paper_dir / fname).exists():
            print(f"找不到文件: {paper_dir / fname}")
            sys.exit(1)

    conclusion_contents = load_conclusion_contents(paper_dir)
    instances_by_key = load_instances_by_key(paper_dir)
    claim_texts = load_claim_texts(paper_dir)
    candidate_groups = load_candidate_associations(paper_dir)

    n_candidates = sum(len(v) for v in candidate_groups.values())
    print(f"{len(candidate_groups)} 个conclusion里有待处理claim,合计 {n_candidates} 条待处理claim")

    special_claims = []
    out_path = paper_dir / "special_claims.json"

    for conc_short, assoc_list in candidate_groups.items():
        claims_info = build_claims_info(conc_short, assoc_list, claim_texts, instances_by_key)
        conclusion_content = conclusion_contents[conc_short]

        result, status = process_conclusion(conclusion_content, claims_info)

        n_changed_here = 0
        for claim in claims_info:
            new_content = result[claim["number"]]
            if content_changed(claim["text"], new_content):
                special_claims.append({
                    "conclusion": conc_short,
                    "claim_number": claim["number"],
                    "text": new_content,
                    "certain_instances_given": [d["instance_number"] for d in claim["certain"]],
                    "uncertain_instances_given": [d["instance_number"] for d in claim["uncertain"]],
                })
                n_changed_here += 1

        print(f"  {conc_short}({len(claims_info)}条待处理) -> {status}, {n_changed_here}条真的发生了实例化改写")

        # 每处理完一个conclusion就整体重写一次,防止中途被打断丢失进度
        out_path.write_text(json.dumps(special_claims, ensure_ascii=False, indent=2), encoding="utf-8")

    # 哪怕全篇论文一条特殊claim都没有(包括压根没有待处理claim的情况),
    # 也要写这个文件、内容是空列表——用同名文件+空列表明确表示"这篇论文
    # 没有特殊claim",不是干脆不产生这个文件让下游猜不到"跑没跑过"。
    out_path.write_text(json.dumps(special_claims, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n全部完成,共 {len(special_claims)} 条claim真的发生了实例化改写,已写入 {out_path}")


if __name__ == "__main__":
    main()
