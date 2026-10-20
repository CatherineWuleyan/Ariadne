"""
step4a_build_claims_final.py

用法:
    python step4a_build_claims_final.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

step4(relation构建)的第一步:在paper文件夹里建一份 claims_final.json,
这份文件会在step4后续步骤里持续维护(比如以后接上instance替换版本的claim
时,会再更新这份文件——这一步只负责搭出最初的版本)。

============================== 这一版claim用的是哪个文本 ==============================
"claim"部分里每条claim的文本,取的是 step2_output_claims.json 里的text——
也就是"已经剥离了误混入的instance重复内容,但还没有把instance替换回一般
词语"的版本(paper_graph2ir/step3_instance_extraction/step3c_instantiate_
claims.py 产出的special_claims.json 暂时不在这一步处理范围内)。

============================== 全文件三个部分 ==============================
claims_final.json 结构:
    {
      "claim": [...],
      "note": [...],
      "relation": [...]
    }

--- claim ---
全部claim(label在assertion/论据/example之一)按(conclusion在原文件里的
出现顺序,claim在conclusion内原有的编号顺序)重新从1开始连续编号(不分
conclusion,是整篇论文一份连续编号)。每条claim有五个字段:
    {"conclusion": "conclusion_4", "text": "...", "is_pure_data": false,
     "needs_more_context": [], "number": 1}
"needs_more_context"原样保留自step2b的"需要更多上下文"字段(list[str],
step2b没能从上下文/引用解决的术语列表;没有问题或者是纯数据claim没跑过
step2b时是空列表[])。
"conclusion"是这条claim原本所在的conclusion短id,方便回溯;claim文本里
原有的【N】(step2b用来引用其它part编号的全角方括号格式)会被替换成
"claim M"或"note M"的新编号形式(取决于N原来指向的是不是claim)。

--- note ---
"被claim引用过的非claim类型内容"——只收"至少被一条claim的【N】引用过"的
非claim part(不是organized_content.json里全部的非claim part都进来);
按"claim按新编号从小到大扫描、扫到某个引用第一次出现"的顺序,从1开始
连续编号。

额外规则:elaboration如果带着非空的数字形式"of"字段(指向别的claim编号,
不是词语形式的"of_terms"——这两个字段数据里互斥,不会同时出现),但这条
elaboration本身没有被任何claim的【N】引用过(所以没有被上面那条规则
自动收进note)——这种情况也要把它补收进note里,续在已有note后面连续
编号,前缀写"elaboration of claim {n}: "(n是它"of"指向的那个claim的
新编号,不是原始编号;"of"是个列表,可能有多个,就"elaboration of
claim {n1}, claim {n2}, ...: ")。只有"完全没被任何claim引用过"的才
用这条规则补收;只要已经被至少一条claim引用过、已经在note里了,就不用
再管这条规则,不会重复收录或者覆盖已有内容。

每条note是:
    {"conclusion": "conclusion_2", "text": "...", "number": 1}
文本按label分两种处理:
  - label=="elaboration" 且有非空的"of_terms"字段 -> 前面加一句
    "elaboration of {of_terms用逗号连接}: ",后面接content原文,不改动
    content本身。
  - 其余情况(非elaboration;或者elaboration但没有of_terms,不管有没有
    数字形式的"of"字段) -> 直接原样照抄content,不加任何前缀。
    (这两种情况怎么处理是问过用户确认的,不是我自己猜的默认值。)
note自己的content本身不做【N】替换(除了外面这层加的前缀,note内容原样
照抄,不做递归的引用改写)。

--- relation ---
两类关系合并成一份列表,relation-label的在前,example/论据关系在后:
  1. 原本label=="relation"的part:取它的connects(已验证过,数据里
     connects/expression引用的编号全部是claim,不会是note)和expression,
     把connects列表和expression字符串里的[N](半角方括号,relation自己
     的原生格式,跟step2b用的全角【N】是两套不同的记号)都换成新的claim
     编号,原样保留这套"[N]"的记号形式,不改写成"claim N"这种文字形式
     (跟claim部分的替换规则不一样,这里沿用relation自己已有的记号)。
  2. 每个"论据"的supports、每个"example"的illustrates:合并成一条relation
     (一个claim哪怕对应多个目标,也只出一条,不拆成多条1对1),expression
     只有1个目标时写"[m] 是 [n] 的例子或证据",多个目标时写
     "[m] 是 ([n1] 和 [n2] ...) 的例子或证据"(m是这个论据/example自己的
     新编号,n/n1/n2/...是它supports/illustrates的目标的新编号),不再
     区分论据和example这两种关系类型。
每条relation有三个字段:
    {"conclusion": "conclusion_2", "connects": [m, n], "expression": "[m] 是 [n] 的例子或证据"}
或(目标不止一个):
    {"conclusion": "conclusion_7", "connects": [m, n1, n2], "expression": "[m] 是 ([n1] 和 [n2]) 的例子或证据"}
或者(relation-label那种,可能连接2个以上编号):
    {"conclusion": "conclusion_2", "connects": [6, 9, 11], "expression": "([6] 且 [9]) 推出 [11]"}
"conclusion"是这条关系连接的这些编号原本所在的conclusion——relation-label
自己的connects/expression、以及论据/example的supports/illustrates,都已经
验证过只会在同一个conclusion内部引用,不会跨conclusion,所以每条relation
只对应唯一一个conclusion,不会有歧义。

============================== 输出 ==============================
data/<paper_id>/claims_final.json,内容就是上面这个
{"claim":[...], "note":[...], "relation":[...]}结构。

路径解析基于本文件自身位置,预期跟 step1_process_conclusions/、
step2_claim_completeness/、step3_instance_extraction/ 同放在项目根目录
下,自成一个 step4_relation_construction/ 目录。
"""

import sys
import re
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"

CLAIM_LABELS = ("assertion", "论据", "example")

# step2b写进完整表述里的引用记号:全角方括号包一个或多个用逗号分隔的
# 数字,比如【7】或者【1, 4】(step2b自己的prompt规则就是"引用多个part
# 时,把每个编号都写进同一对括号里,用逗号隔开",不是分开写成【1】【4】
# 这种形式——这一点最早被漏掉过,【1, 4】这种多编号引用完全不会被下面
# 这条正则匹配到,导致这种引用在最终输出里原样残留、没有被替换成"claim
# N"/"note N",是从写这个脚本起就有、但很长时间没被真实数据触发的一个
# bug)。跟relation part自己的connects/expression用的半角"[7]"是两套
# 不同的记号,不要混用。
CLAIM_TEXT_CITATION_RE = re.compile(r"【([\d,\s]+)】")

# relation part自己的connects/expression用的记号:半角方括号包一个数字。
RELATION_BRACKET_RE = re.compile(r"\[(\d+)\]")


def _parse_citation_numbers(raw: str) -> list:
    """把【N】或者【N1, N2, ...】括号里的原始字符串,解析成一个int列表。"""
    return [int(n.strip()) for n in raw.split(",") if n.strip()]


def _conclusion_short_id(c: dict) -> str:
    return c["id"].split("::")[-1]


def load_organized_conclusions(paper_dir: Path) -> list:
    return json.loads((paper_dir / "organized_content.json").read_text(encoding="utf-8"))


def load_claim_texts(paper_dir: Path) -> dict:
    """{(conclusion, number): {"text":..., "is_pure_data":...,
    "needs_more_context":...}} from step2_output_claims.json(step2最终
    产物,已剥离instance重复内容,还没替换instance)。"""
    records = json.loads((paper_dir / "step2_output_claims.json").read_text(encoding="utf-8"))
    return {
        (r["conclusion"], r["number"]): {
            "text": r["text"],
            "is_pure_data": r["is_pure_data"],
            "needs_more_context": r.get("needs_more_context", []),
        }
        for r in records
    }


def build_claim_number_map(organized_conclusions: list) -> tuple:
    """返回 (claim_number_map, ordered_claim_keys)。
    claim_number_map: {(conclusion, 原编号): 新claim编号(从1开始)}
    ordered_claim_keys: [(conclusion, 原编号), ...],就是新编号1,2,3...
    对应的原始key,按顺序排列,方便后面照这个顺序输出。
    编号顺序:按organized_content.json里conclusion出现的顺序,同一个
    conclusion内按原有part编号升序——不分conclusion,整篇论文连续编号。
    """
    ordered_claim_keys = []
    for c in organized_conclusions:
        conc_short = _conclusion_short_id(c)
        for p in c["organized_parts"]:
            if p["label"] in CLAIM_LABELS:
                ordered_claim_keys.append((conc_short, p["number"]))

    claim_number_map = {key: i + 1 for i, key in enumerate(ordered_claim_keys)}
    return claim_number_map, ordered_claim_keys


def build_note_number_map(ordered_claim_keys: list, claim_texts: dict, claim_number_map: dict,
                           labels_by_key: dict) -> tuple:
    """返回 (note_number_map, ordered_note_keys)。
    按claim的新编号从小到大扫描每条claim的text,在text里从左到右找
    【N】或者【N1, N2, ...】这种引用;每个引用到的编号如果不是claim
    (不在claim_number_map里),就是一条note——按"第一次被扫到"的顺序,
    从1开始连续编号(一个【N1, N2】里的N1、N2按左到右的书写顺序各自
    处理,不是整体当一个单位)。
    """
    note_number_map = {}
    ordered_note_keys = []

    for key in ordered_claim_keys:
        conc_short, _ = key
        text = claim_texts[key]["text"]
        for m in CLAIM_TEXT_CITATION_RE.finditer(text):
            for cited_number in _parse_citation_numbers(m.group(1)):
                cited_key = (conc_short, cited_number)
                if cited_key in claim_number_map:
                    continue  # 引用的是claim,不是note
                if cited_key not in labels_by_key:
                    continue  # 引用了一个不存在的编号(理论上不该发生),跳过不处理
                if cited_key not in note_number_map:
                    note_number_map[cited_key] = len(ordered_note_keys) + 1
                    ordered_note_keys.append(cited_key)

    return note_number_map, ordered_note_keys


def rewrite_claim_text_citations(text: str, conc_short: str, claim_number_map: dict, note_number_map: dict) -> str:
    """把text里的【N】或者【N1, N2, ...】(step1a/step2b用的全角引用记号)
    替换成"claim M"/"note M",多个编号之间用", "连接(比如【1, 4】可能
    变成"claim 2, note 1")。某个编号如果既不是claim也不是已收录的note
    (理论上不该发生,因为note_number_map是扫描全部claim的引用后建出来
    的,一定覆盖了这里会遇到的每一个非claim引用),这一个编号原样保留
    成【N】,不静默丢失信息,但不影响同一处引用里其它能正常解析的编号。"""
    def _replace(m):
        parts = []
        for cited_number in _parse_citation_numbers(m.group(1)):
            key = (conc_short, cited_number)
            if key in claim_number_map:
                parts.append(f"claim {claim_number_map[key]}")
            elif key in note_number_map:
                parts.append(f"note {note_number_map[key]}")
            else:
                parts.append(f"【{cited_number}】")
        return ", ".join(parts)

    return CLAIM_TEXT_CITATION_RE.sub(_replace, text)


def build_claim_entries(ordered_claim_keys: list, claim_number_map: dict, claim_texts: dict,
                         note_number_map: dict) -> list:
    entries = []
    for key in ordered_claim_keys:
        conc_short, _ = key
        raw = claim_texts[key]
        rewritten = rewrite_claim_text_citations(raw["text"], conc_short, claim_number_map, note_number_map)
        entries.append({
            "conclusion": conc_short,
            "text": rewritten,
            "is_pure_data": raw["is_pure_data"],
            "needs_more_context": raw["needs_more_context"],
            "number": claim_number_map[key],
        })
    return entries


def build_note_text(key: tuple, part: dict, claim_number_map: dict, of_claim_prefix_keys: set) -> str:
    """note的文本,按part自身字段情况+收录路径分三种:
      - label=="elaboration"且有非空"of_terms"(词语列表) -> 前面加
        "elaboration of {terms用逗号连接}: "
      - label=="elaboration"且有非空数字形式"of"(指向别的claim编号),
        且key在of_claim_prefix_keys里(意味着这条note是靠"没被任何claim
        引用过、但of指向了claim"这条规则单独补收进来的,不是被claim直接
        引用收进来的)-> 前面加"elaboration of claim {n1}, claim {n2},
        ...: "(n是claim的新编号)
      - 其余情况(非elaboration;elaboration但of_terms/of都没有;或者
        elaboration有数字形式的of、但这条note其实是被某条claim直接
        引用才收进来的,不在of_claim_prefix_keys里)-> 直接原样照抄
        content,不加任何前缀
    同一个"数字形式of"字段,在"被claim直接引用收进来"和"靠of单独补收
    进来"这两条不同收录路径下的展示规则不一样(前者按更早确认过的规则
    不加前缀,后者按这次新加的规则要加前缀),所以不能只看part自己有没有
    这个字段来决定要不要加前缀,必须靠of_claim_prefix_keys区分走的是
    哪条收录路径。
    """
    conc_short, _ = key
    content = part["content"]
    of_terms = part.get("of_terms")
    of_numbers = part.get("of")

    if part["label"] == "elaboration" and of_terms:
        terms_str = ", ".join(of_terms)
        return f"elaboration of {terms_str}: {content}"

    if part["label"] == "elaboration" and of_numbers and key in of_claim_prefix_keys:
        new_numbers = [claim_number_map[(conc_short, n)] for n in of_numbers]
        claims_str = ", ".join(f"claim {n}" for n in new_numbers)
        return f"elaboration of {claims_str}: {content}"

    return content


def add_unreferenced_of_claim_elaborations(organized_conclusions: list, claim_number_map: dict,
                                            note_number_map: dict, ordered_note_keys: list) -> tuple:
    """对每个label=="elaboration"且有非空数字"of"字段(指向claim编号)的
    part,如果它还没有被收进note_number_map(意味着没有被任何claim的
    【N】引用过),补收进来,续在已有note后面连续编号——只有"完全没被任何
    claim引用过"的才补收;已经被引用过、已经在note里的不用管(这是用户
    明确要求的行为,不是漏检了才补,是故意只处理这一种情况)。按
    (conclusion在文件里出现顺序,原始编号升序)遍历决定新收进来的这些
    note之间的先后顺序。

    返回 (note_number_map, ordered_note_keys, newly_added_keys)——
    newly_added_keys是这一步实际新收进来的key集合,传给build_note_text
    用来区分"这条note该不该加elaboration of claim前缀"(只有这一步新收
    进来的才加,原本就因为被claim引用而在note里的,即使也有数字形式的
    of字段,也不加前缀,维持更早确认过的规则不变)。不修改传入的原始
    字典/列表,返回的是新对象。
    """
    note_number_map = dict(note_number_map)
    ordered_note_keys = list(ordered_note_keys)
    newly_added_keys = set()

    for c in organized_conclusions:
        conc_short = _conclusion_short_id(c)
        for p in c["organized_parts"]:
            if p["label"] != "elaboration" or not p.get("of"):
                continue
            key = (conc_short, p["number"])
            if key in note_number_map:
                continue  # 已经被某条claim引用过、已经在note里了,不用管
            note_number_map[key] = len(ordered_note_keys) + 1
            ordered_note_keys.append(key)
            newly_added_keys.add(key)

    return note_number_map, ordered_note_keys, newly_added_keys


def build_note_entries(ordered_note_keys: list, note_number_map: dict, parts_by_key: dict,
                        claim_number_map: dict, of_claim_prefix_keys: set) -> list:
    entries = []
    for key in ordered_note_keys:
        conc_short, _ = key
        part = parts_by_key[key]
        entries.append({
            "conclusion": conc_short,
            "text": build_note_text(key, part, claim_number_map, of_claim_prefix_keys),
            "number": note_number_map[key],
        })
    return entries


def build_relations_from_relation_labels(organized_conclusions: list, claim_number_map: dict) -> list:
    """原本label=="relation"的part各自转成一条relation。connects/
    expression引用的编号已经验证过全部是claim,直接用claim_number_map
    换算;expression里替换的是半角"[N]"这套relation自己的原生记号,不是
    claim文本里那套全角【N】,两者不通用。"""
    relations = []
    for c in organized_conclusions:
        conc_short = _conclusion_short_id(c)
        for p in c["organized_parts"]:
            if p["label"] != "relation":
                continue

            new_connects = [claim_number_map[(conc_short, n)] for n in p["connects"]]

            def _replace(m, conc_short=conc_short):
                key = (conc_short, int(m.group(1)))
                new_n = claim_number_map.get(key)
                return f"[{new_n}]" if new_n is not None else m.group(0)

            new_expression = RELATION_BRACKET_RE.sub(_replace, p["expression"])

            relations.append({"conclusion": conc_short, "connects": new_connects, "expression": new_expression})

    return relations


def build_relations_from_claim_links(ordered_claim_keys: list, claim_number_map: dict,
                                      parts_by_key: dict) -> list:
    """每个论据的supports、每个example的illustrates,合并成一条relation
    (一个claim哪怕对应多个目标,也只出一条,不拆成多条1对1):只有1个目标
    时expression写"[m] 是 [n] 的例子或证据";多个目标时写
    "[m] 是 ([n1] 和 [n2] ...) 的例子或证据"。不再区分论据和example这两种
    关系类型。"""
    relations = []
    for key in ordered_claim_keys:
        conc_short, _ = key
        part = parts_by_key[key]
        new_m = claim_number_map[key]

        if part["label"] == "论据":
            targets = part.get("supports", [])
        elif part["label"] == "example":
            targets = part.get("illustrates", [])
        else:
            continue

        new_targets = []
        for t in targets:
            new_n = claim_number_map.get((conc_short, t))
            if new_n is not None:
                new_targets.append(new_n)  # 理论上supports/illustrates只指向claim,这里都能算出来

        if not new_targets:
            continue

        if len(new_targets) == 1:
            expression = f"[{new_m}] 是 [{new_targets[0]}] 的例子或证据"
        else:
            joined = " 和 ".join(f"[{n}]" for n in new_targets)
            expression = f"[{new_m}] 是 ({joined}) 的例子或证据"

        relations.append({
            "conclusion": conc_short,
            "connects": [new_m] + new_targets,
            "expression": expression,
        })

    return relations


def build_claims_final(paper_dir: Path) -> dict:
    organized_conclusions = load_organized_conclusions(paper_dir)
    claim_texts = load_claim_texts(paper_dir)

    parts_by_key = {}
    labels_by_key = {}
    for c in organized_conclusions:
        conc_short = _conclusion_short_id(c)
        for p in c["organized_parts"]:
            key = (conc_short, p["number"])
            parts_by_key[key] = p
            labels_by_key[key] = p["label"]

    claim_number_map, ordered_claim_keys = build_claim_number_map(organized_conclusions)

    missing = [key for key in ordered_claim_keys if key not in claim_texts]
    if missing:
        raise ValueError(
            f"step2_output_claims.json 里缺少这些claim: {missing}"
            "(需要先跑完 step2e_finalize_claims.py)"
        )

    note_number_map, ordered_note_keys = build_note_number_map(
        ordered_claim_keys, claim_texts, claim_number_map, labels_by_key
    )
    note_number_map, ordered_note_keys, of_claim_prefix_keys = add_unreferenced_of_claim_elaborations(
        organized_conclusions, claim_number_map, note_number_map, ordered_note_keys
    )

    claim_entries = build_claim_entries(ordered_claim_keys, claim_number_map, claim_texts, note_number_map)
    note_entries = build_note_entries(
        ordered_note_keys, note_number_map, parts_by_key, claim_number_map, of_claim_prefix_keys
    )
    relation_entries = (
        build_relations_from_relation_labels(organized_conclusions, claim_number_map)
        + build_relations_from_claim_links(ordered_claim_keys, claim_number_map, parts_by_key)
    )

    return {"claim": claim_entries, "note": note_entries, "relation": relation_entries}


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    for fname in ("organized_content.json", "step2_output_claims.json"):
        if not (paper_dir / fname).exists():
            print(f"找不到文件: {paper_dir / fname}")
            sys.exit(1)

    result = build_claims_final(paper_dir)

    out_path = paper_dir / "claims_final.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"claim: {len(result['claim'])} 条,note: {len(result['note'])} 条,"
        f"relation: {len(result['relation'])} 条,已写入 {out_path}"
    )


if __name__ == "__main__":
    main()
