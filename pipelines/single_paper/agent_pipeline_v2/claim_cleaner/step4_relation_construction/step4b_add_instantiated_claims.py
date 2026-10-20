"""
step4b_add_instantiated_claims.py

用法:
    python step4b_add_instantiated_claims.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

step4(relation构建)的第二步:把 step3c_instantiate_claims.py 产出的
special_claims.json(替换了instance之后的claim)追加进 step4a已经建好的
claims_final.json——不是重新生成整个claims_final.json,是在它现有的
claim/relation两个部分后面追加新内容,note部分不动(实例化claim不会引入
任何新的note,见下文说明)。

============================== 为什么要重新算一遍claim_number_map ==============================
claims_final.json里的claim条目只有text/is_pure_data/number三个字段,
原始的(conclusion, 原始编号)这个身份信息在step4a构建的时候就已经被丢弃
了(整个设计目的就是让claim部分不再依赖那套旧编号)。但
special_claims.json里的每一条,是用(conclusion, claim_number)这套旧
编号体系记录"这是哪条claim的实例化版本"的,所以这一步要拿到"旧编号
->新编号"这套映射才能正确接上——办法不是去claims_final.json里反查
(反查不出来,信息已经丢了),而是照着跟step4a一模一样的逻辑,从
organized_content.json + step2_output_claims.json 重新算一遍
claim_number_map/note_number_map(确定性计算,只要这两个源文件没变,
重新算出来的映射跟step4a当初算出来的完全一样——两边用的是同一份
代码,直接从step4a import过来复用,不是照着抄一份可能跑偏的逻辑)。

============================== 实例化claim会不会引入新的note ==============================
不会。special_claims.json里每条实例化claim的文本,是在step2_output_
claims.json对应那条claim的文本基础上,只把instance的target替换掉、
其它文字不动改出来的——原来文本里带的【N】引用,实例化之后还在原样
留着(替换instance不会动到引用记号)。这些引用在step4a第一遍扫描全部
原始claim的时候已经全部收录进note_number_map了,所以这一步只需要复用
现成的映射做文本替换,不需要重新做一次note收集。

============================== 处理逻辑 ==============================
  1. 读 claims_final.json(step4a的产物),取出现有的claim/note/relation
     三个列表。
  2. 用跟step4a完全一样的函数(直接import复用),从organized_content.json
     + step2_output_claims.json重新算出claim_number_map和
     note_number_map。
  3. 防重复保护:如果现有claim列表条数已经比重新算出来的原始claim条数
     多,说明这一步之前已经跑过、加过一次实例化claim了,直接报错退出,
     不重复追加。
  4. 读 special_claims.json(step3c产物)。是空列表就什么都不做
     (claims_final.json原样不动,不重写)。
  5. 把special_claims.json里的条目,按"conclusion在organized_content
     .json里出现的顺序、conclusion内claim_number升序"重新显式排一遍
     (不依赖special_claims.json文件自身写入时的顺序),保证跟原始claim
     用的是同一套排序原则。
  6. 依次给每条实例化claim分配新编号,从"现有claim最大编号+1"开始连续
     累加;文本里的【N】引用用claim_number_map/note_number_map换算成
     "claim M"/"note M"(复用step4a的rewrite_claim_text_citations);
     is_pure_data取自它对应原始claim在step2_output_claims.json里的值
     (实际上恒为False——能走到实例化这一步的claim,在更早的step2c就
     已经因为"是纯实验数据"被跳过、不会有instance_containment,所以
     不会进入special_claims.json;这里还是老老实实查一遍,不写死这个
     假设);needs_more_context同样直接继承自它对应的原始claim(实例化
     只是把instance的target替换掉,不会改变这条claim本身缺不缺上下文
     这件事,所以原样沿用原claim的判断,不重新算);conclusion字段取自
     special_claims.json里这条自己的conclusion(实例化claim跟它的原始
     claim必然属于同一个conclusion)。
  7. 每条实例化claim追加一条关系:
     {"conclusion": 同上, "connects": [新实例化编号, 对应原始claim的新编号],
      "expression": "[新实例化编号] 是 [对应原始claim的新编号] 的instance"}
     (这里"instance"三个字母按原话保留英文,不翻译成中文,跟论据/example
     那条"...的例子或证据"用中文的写法不一样,是两种不同的既定格式)。
  8. 把追加后的claim/relation列表(note不变)写回claims_final.json。

============================== 输出 ==============================
覆盖写回 data/<paper_id>/claims_final.json,在原有claim/relation列表
后面追加新内容,note列表不变。

路径解析基于本文件自身位置,预期跟 step4a_build_claims_final.py 同放在
step4_relation_construction/ 目录下,直接import它复用同一套映射计算和
引用替换函数,不重新写一份。
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from step4a_build_claims_final import (  # noqa: E402
    load_organized_conclusions,
    load_claim_texts,
    build_claim_number_map,
    build_note_number_map,
    rewrite_claim_text_citations,
    _conclusion_short_id,
)


def load_special_claims(paper_dir: Path) -> list:
    return json.loads((paper_dir / "special_claims.json").read_text(encoding="utf-8"))


def build_conclusion_order_index(organized_conclusions: list) -> dict:
    return {_conclusion_short_id(c): i for i, c in enumerate(organized_conclusions)}


def add_instantiated_claims(paper_dir: Path) -> dict:
    """返回更新后的完整claims_final结构(claim/relation追加了新内容,
    note不变)。"""
    claims_final = json.loads((paper_dir / "claims_final.json").read_text(encoding="utf-8"))

    organized_conclusions = load_organized_conclusions(paper_dir)
    claim_texts = load_claim_texts(paper_dir)

    claim_number_map, ordered_claim_keys = build_claim_number_map(organized_conclusions)

    labels_by_key = {}
    for c in organized_conclusions:
        conc_short = _conclusion_short_id(c)
        for p in c["organized_parts"]:
            labels_by_key[(conc_short, p["number"])] = p["label"]

    note_number_map, _ = build_note_number_map(
        ordered_claim_keys, claim_texts, claim_number_map, labels_by_key
    )

    if len(claims_final["claim"]) > len(ordered_claim_keys):
        raise RuntimeError(
            f"claims_final.json 里已经有 {len(claims_final['claim'])} 条claim,"
            f"比重新算出来的原始claim数({len(ordered_claim_keys)})还多——"
            "看起来这一步之前已经跑过了,不重复追加。"
        )

    special_claims = load_special_claims(paper_dir)
    if not special_claims:
        return claims_final  # 没有实例化claim,原样不动

    conclusion_order_index = build_conclusion_order_index(organized_conclusions)
    special_claims_sorted = sorted(
        special_claims,
        key=lambda e: (conclusion_order_index[e["conclusion"]], e["claim_number"]),
    )

    max_existing_number = len(claims_final["claim"])
    new_claim_entries = []
    new_relation_entries = []

    for i, entry in enumerate(special_claims_sorted):
        conc_short = entry["conclusion"]
        orig_key = (conc_short, entry["claim_number"])
        original_new_number = claim_number_map[orig_key]

        new_number = max_existing_number + 1 + i
        rewritten_text = rewrite_claim_text_citations(
            entry["text"], conc_short, claim_number_map, note_number_map
        )
        is_pure_data = claim_texts[orig_key]["is_pure_data"]
        needs_more_context = claim_texts[orig_key]["needs_more_context"]

        new_claim_entries.append({
            "conclusion": conc_short,
            "text": rewritten_text,
            "is_pure_data": is_pure_data,
            "needs_more_context": needs_more_context,
            "number": new_number,
        })
        new_relation_entries.append({
            "conclusion": conc_short,
            "connects": [new_number, original_new_number],
            "expression": f"[{new_number}] 是 [{original_new_number}] 的instance",
        })

    claims_final["claim"] = claims_final["claim"] + new_claim_entries
    claims_final["relation"] = claims_final["relation"] + new_relation_entries
    return claims_final


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    required = (
        "claims_final.json", "organized_content.json",
        "step2_output_claims.json", "special_claims.json",
    )
    for fname in required:
        if not (paper_dir / fname).exists():
            print(f"找不到文件: {paper_dir / fname}")
            sys.exit(1)

    n_before = len(json.loads((paper_dir / "claims_final.json").read_text(encoding="utf-8"))["claim"])

    result = add_instantiated_claims(paper_dir)

    (paper_dir / "claims_final.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    n_added = len(result["claim"]) - n_before
    print(
        f"追加了 {n_added} 条实例化claim(及对应的{n_added}条instance关系),"
        f"claim部分从{n_before}条变成{len(result['claim'])}条,已写回 {paper_dir / 'claims_final.json'}"
    )


if __name__ == "__main__":
    main()
