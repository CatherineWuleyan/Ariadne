"""
step3a_locate_instance_targets.py

step3(instance 提取)的第一步:给 organized_content.json 里所有
label=="instance" 的 part,对它的每个 of_terms 短语,在这条 conclusion 的
原始 content 里定位一个精确的目标字符区间。

用法:
    python step3a_locate_instance_targets.py <paper_id>
    python step3a_locate_instance_targets.py all       # 处理data/下全部paper_id
或不带参数运行,会提示输入。

============================== 依赖 ==============================
只需要 organized_content.json(step1b_organize_labeled_content.py 的产物)——
这个文件本身已经同时包含了:
  - 每条 conclusion 的原始 content 字段(未切分的完整原文)
  - 每个 organized_part(含 instance)的 source_spans:该 part 在 content
    里精确的起止字符位置,由 step1b 阶段的
    content_organizer/position_alignment.py 算出、原样写进了输出
不需要重新读 graph.json 或 conclusions_labeled.json。

instance 是非 claim 标签,按 step1b_organize_labeled_content.py 的既有约定,
非 claim part 的 source_spans 永远只有1个元素——直接取 [0] 作为 instance
自己的位置;如果这个假设被破坏,下面会直接 assert 失败,不会静默算错。

============================== 匹配与选择规则 ==============================
具体的归一化+定位+"多个匹配时怎么选"规则,见同目录下 target_locator.py
的文档字符串,这里只概述:排除跟instance自己重叠的位置后,让匹配规则从
"跟label_validator一致的松匹配"逐步收紧到"精确匹配"一共四层,哪一层先
收紧到恰好1个匹配就用哪个(certainty="certain");都收不到唯一就把
"最后一层还有匹配"的那一层的全部位置作为候选列表给出来
(certainty="uncertain"),不瞎选一个;排除自身后连最松的一层都找不到,
就是certainty="not_found"。

============================== 输出 ==============================
在 data/<paper_id>/ 目录下写 instance_target_positions.json,一个列表,
每条 instance 一个块:
    {
      "conclusion": "conclusion_2",
      "instance_number": 6,
      "instance_content": "...",
      "instance_span": {"start": int, "end": int},
      "targets": [
        {
          "term": "...",
          "certainty": "certain"/"uncertain"/"not_found",
          "content": "..." 或 null,   (只有certain时有值)
          "start": int 或 null,        (只有certain时有值)
          "end": int 或 null,
          "candidates": [              (只有uncertain时有内容)
            {"content": "...", "start": int, "end": int}, ...
          ]
        },
        ...
      ]
    }
"targets" 跟这个 instance 的 of_terms 一一对应、顺序一致。目前实测数据里
所有 instance 的 of_terms 都只有1项,但这里按"可以有多项"设计,任何时候
出现多目标的 instance 都能正常处理,不需要改代码。

路径解析基于本文件自身所在位置,预期跟 step1_process_conclusions/、
step2_claim_completeness/ 同级放在项目根目录下:

    paper_graph2ir/
    ├── data/
    │   └── <paper_id>/
    │       ├── organized_content.json           (step1b产物,本脚本的输入)
    │       └── instance_target_positions.json   (本脚本的输出)
    ├── step1_process_conclusions/
    │   └── label_validator.py                   (本脚本经由target_locator复用它的_term_normalize)
    ├── step2_claim_completeness/
    └── step3_instance_extraction/
        ├── step3a_locate_instance_targets.py     <- 本文件
        └── target_locator.py
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

for _p in (PROJECT_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from target_locator import locate_target  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"


def process_paper(paper_id: str) -> list:
    """
    处理单篇论文,返回这篇论文全部 instance 的定位结果列表(不写文件,
    方便 main() 统一决定写不写、以及调试/测试时直接复用)。
    """
    organized_path = DATA_DIR / paper_id / "organized_content.json"
    if not organized_path.exists():
        print(f"跳过 {paper_id}: 找不到 {organized_path}(需要先跑完 step1b_organize_labeled_content.py)")
        return []

    conclusions = json.loads(organized_path.read_text(encoding="utf-8"))
    results = []

    for conc in conclusions:
        content = conc["content"]
        conc_short = conc["id"].split("::")[-1]

        for part in conc["organized_parts"]:
            if part["label"] != "instance":
                continue

            spans = part["source_spans"]
            assert len(spans) == 1, (
                f"{paper_id}/{conc_short} 的 instance[{part['number']}] "
                f"source_spans 有 {len(spans)} 个元素,预期非claim标签永远只有1个"
                "(step1b_organize_labeled_content.py 的既有约定被破坏了,需要检查)"
            )
            instance_start = spans[0]["start"]
            instance_end = spans[0]["end"]

            targets = [
                locate_target(content, term, instance_start, instance_end)
                for term in part["of_terms"]
            ]

            results.append({
                "conclusion": conc_short,
                "instance_number": part["number"],
                "instance_content": part["content"],
                "instance_span": {"start": instance_start, "end": instance_end},
                "targets": targets,
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

    total_instances = 0
    total_uncertain = 0
    total_not_found = 0

    for paper_id in paper_ids:
        results = process_paper(paper_id)
        if not results:
            continue

        out_path = DATA_DIR / paper_id / "instance_target_positions.json"
        out_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        n_instances = len(results)
        uncertain = [
            (r["conclusion"], r["instance_number"], t["term"])
            for r in results for t in r["targets"] if t["certainty"] == "uncertain"
        ]
        not_found = [
            (r["conclusion"], r["instance_number"], t["term"])
            for r in results for t in r["targets"] if t["certainty"] == "not_found"
        ]
        total_instances += n_instances
        total_uncertain += len(uncertain)
        total_not_found += len(not_found)

        msg = f"{paper_id}: {n_instances} 条instance,写入 {out_path}"
        if uncertain:
            msg += f"(uncertain {len(uncertain)}个: {uncertain})"
        if not_found:
            msg += f"(not_found {len(not_found)}个: {not_found})"
        print(msg)

    print(
        f"\n全部完成,共处理 {total_instances} 条instance"
        + (f",uncertain {total_uncertain}个、not_found {total_not_found}个,建议逐条检查"
           if (total_uncertain or total_not_found)
           else "，全部target都在某一层收紧到了唯一匹配(certain)")
    )


if __name__ == "__main__":
    main()
