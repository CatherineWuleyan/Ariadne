"""
step3b_classify_claim_instances.py

用法:
    python step3b_classify_claim_instances.py <paper_id>
    python step3b_classify_claim_instances.py all
或不带参数运行,会提示输入。

step3(instance提取)的第二步:给每一条claim(label在assertion/论据/example
之一)找出它的"确定性instance"和"不确定性instance"。

============================== 两个定义 ==============================
- 确定性instance:这个instance全部target的全部位置(span),都被这条claim
  的span完整包含。
- 不确定性instance:这个instance全部target的全部位置合并起来的并集,跟
  这条claim的span有交集,但又够不上"确定性"那条线。

一个instance可能有多个target(对应of_terms里的多个短语),每个target
本身在step3a里又可能是certain(1个位置)、uncertain(多个候选位置)、或
not_found(没有位置)。这里说的"target的span"是指:
  - certain的target -> 它那1个位置
  - uncertain的target -> 它的全部候选位置(候选列表里每一个都算,不是
    随便挑一个,也不是要求先在候选里选出"正确"的那个再判断)
  - not_found的target -> 没有位置,这个target对这次判断没有意义,既不
    会阻止"确定性"成立,也不贡献"有交集"的证据
如果一个instance的全部target都是not_found(压根没找到任何位置),这个
instance跟任何claim都不会有关联,不会出现在任何claim的确定性/不确定性
列表里。

这两个定义是独立于step3a自己的certain/uncertain的——step3a的certain/
uncertain问的是"这个term在原文里的位置找得准不准";这里问的是"不管
step3a当初找位置的时候准不准,这个instance到底是不是能跟这条claim对上"。
一个target即使在step3a里是uncertain(有好几个候选位置,不知道哪个是真的),
只要这几个候选**全部**都恰好落在同一条claim的span里,这个instance对这条
claim来说也是确定性的(不管哪个候选是"真的",反正都在这条claim里)。

============================== claim的span怎么算 ==============================
一条claim(assertion/论据/example)如果因为被打断,source_spans里有多段
(不连续的多个片段),这里"包含"/"有交集"是跟这些片段逐个比较、只要有
任意一段满足就算,不是把多段强行拼成一个连续区间再比较(片段之间往往
隔着别的内容,不能当成连续的)。

============================== 处理逻辑 ==============================
  1. 读 organized_content.json,取每个conclusion里label在
     {assertion, 论据, example}的part(number/label/source_spans),
     这些是要判断的claim。
  2. 读 step3a的产物 instance_target_positions.json,取每个conclusion的
     全部instance记录。
  3. 对每个conclusion,每条claim,遍历这个conclusion里的全部instance:
       - 展平这个instance全部target的全部位置(跳过not_found的target)。
       - 如果一个位置都没有 -> 这个instance跳过,不出现在任何claim的
         结果里。
       - 全部位置都被这条claim的某一段source_span完整包含 -> 确定性。
       - 不是确定性,但至少有一个位置跟这条claim的某一段source_span有
         交集 -> 不确定性。
       - 否则(完全不沾边)-> 这条claim的结果里不会出现这个instance。
  4. 每条claim的结果里都会出现(哪怕两个列表都是空的),不是只有非空的
     才出现,方便下游区分"这条claim真的没有关联instance"和"这条claim
     压根没被检查过"。

============================== 输出格式 ==============================
data/<paper_id>/claim_instance_associations.json,一个列表,每条claim
一条:
    {
      "conclusion": "conclusion_5",
      "claim_number": 6,
      "claim_label": "assertion",
      "certain_instances": [8],
      "uncertain_instances": []
    }
"certain_instances"/"uncertain_instances"里只有instance编号,不带
instance_content等其它信息——要查某个编号具体是什么内容,去
instance_target_positions.json或organized_content.json按编号查,这里
不重复存一份。

路径解析基于本文件自身位置,预期跟 step3a_locate_instance_targets.py 同放
在 step3_instance_extraction/ 目录下。
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"

_CLAIM_LABELS = ("assertion", "论据", "example")


def get_target_spans(target: dict) -> list:
    """从step3a的target记录里取出这个target的全部候选位置:certain是1个,
    uncertain是候选列表里全部,not_found是空列表。返回[(start,end),...]。"""
    if target["certainty"] == "certain":
        return [(target["start"], target["end"])]
    if target["certainty"] == "uncertain":
        return [(c["start"], c["end"]) for c in target["candidates"]]
    return []  # not_found


def instance_all_spans(instance_record: dict) -> list:
    """这个instance全部target的全部候选位置,跨target合并展平成一个列表
    (哪个target给出的不重要,这一步只关心"这个instance可能在原文哪些
    位置")。"""
    spans = []
    for t in instance_record["targets"]:
        spans.extend(get_target_spans(t))
    return spans


def _contained_in_any_piece(span: tuple, pieces: list) -> bool:
    s, e = span
    return any(ps <= s and e <= pe for ps, pe in pieces)


def _overlaps_any_piece(span: tuple, pieces: list) -> bool:
    s, e = span
    return any(s < pe and ps < e for ps, pe in pieces)


def classify_instance_for_claim(instance_record: dict, claim_pieces: list):
    """返回 "certain" / "uncertain" / None(这个instance跟这条claim完全
    没关系)。判断规则见模块文档"两个定义"一节。"""
    spans = instance_all_spans(instance_record)
    if not spans:
        return None  # 全部target都是not_found,跟谁都不沾边

    if all(_contained_in_any_piece(span, claim_pieces) for span in spans):
        return "certain"

    if any(_overlaps_any_piece(span, claim_pieces) for span in spans):
        return "uncertain"

    return None


def load_claims_and_instances(paper_dir: Path) -> dict:
    """返回 {conclusion短id: {"claims": [...], "instances": [...]}}。
    claims里每条是 {"number", "label", "pieces": [(start,end), ...]};
    instances是 instance_target_positions.json 里这个conclusion的全部
    记录,原样保留。"""
    organized_path = paper_dir / "organized_content.json"
    targets_path = paper_dir / "instance_target_positions.json"

    conclusions = json.loads(organized_path.read_text(encoding="utf-8"))
    result = {}
    for c in conclusions:
        conc_short = c["id"].split("::")[-1]
        claims = [
            {
                "number": p["number"],
                "label": p["label"],
                "pieces": [(sp["start"], sp["end"]) for sp in p["source_spans"]],
            }
            for p in c["organized_parts"] if p["label"] in _CLAIM_LABELS
        ]
        result[conc_short] = {"claims": claims, "instances": []}

    instance_targets = json.loads(targets_path.read_text(encoding="utf-8"))
    for item in instance_targets:
        conc_short = item["conclusion"]
        if conc_short in result:
            result[conc_short]["instances"].append(item)

    return result


def process_paper(paper_id: str) -> list:
    paper_dir = DATA_DIR / paper_id
    organized_path = paper_dir / "organized_content.json"
    targets_path = paper_dir / "instance_target_positions.json"

    if not organized_path.exists():
        print(f"跳过 {paper_id}: 找不到 {organized_path}")
        return []
    if not targets_path.exists():
        print(f"跳过 {paper_id}: 找不到 {targets_path}(需要先跑完 step3a_locate_instance_targets.py)")
        return []

    data = load_claims_and_instances(paper_dir)

    results = []
    for conc_short, info in data.items():
        for claim in info["claims"]:
            certain, uncertain = [], []
            for inst in info["instances"]:
                status = classify_instance_for_claim(inst, claim["pieces"])
                if status == "certain":
                    certain.append(inst["instance_number"])
                elif status == "uncertain":
                    uncertain.append(inst["instance_number"])

            results.append({
                "conclusion": conc_short,
                "claim_number": claim["number"],
                "claim_label": claim["label"],
                "certain_instances": certain,
                "uncertain_instances": uncertain,
            })

    return results


def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
    else:
        arg = input("请输入 paper_id(或输入 all 处理 data/ 下全部论文): ").strip()

    if arg == "all":
        if not DATA_DIR.exists():
            print(f"找不到目录: {DATA_DIR}")
            sys.exit(1)
        paper_ids = sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir())
    else:
        paper_ids = [arg]

    total_claims = 0
    total_with_certain = 0
    total_with_uncertain = 0

    for paper_id in paper_ids:
        results = process_paper(paper_id)
        if not results:
            continue

        out_path = DATA_DIR / paper_id / "claim_instance_associations.json"
        out_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        n_certain = sum(1 for r in results if r["certain_instances"])
        n_uncertain = sum(1 for r in results if r["uncertain_instances"])
        total_claims += len(results)
        total_with_certain += n_certain
        total_with_uncertain += n_uncertain

        print(f"{paper_id}: {len(results)} 条claim,{n_certain} 条有确定性instance,"
              f"{n_uncertain} 条有不确定性instance,写入 {out_path}")

    print(f"\n全部完成,共 {total_claims} 条claim,{total_with_certain} 条带确定性instance,"
          f"{total_with_uncertain} 条带不确定性instance")


if __name__ == "__main__":
    main()
