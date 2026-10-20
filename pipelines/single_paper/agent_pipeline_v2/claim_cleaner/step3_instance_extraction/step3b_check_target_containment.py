"""
step3b_check_target_containment.py

step3(instance 提取)的检查步骤:验证 step3a 定位出的每个 instance target
位置(certain 情况下的那一个位置,uncertain 情况下的每一个 candidate),
是否完全落在 organized_content.json 里某一个 labeled part 的 source_spans
范围之内,并报告落在了哪个/哪些 label 里。

用法:
    python step3b_check_target_containment.py <paper_id>
    python step3b_check_target_containment.py all
或不带参数运行,会提示输入。

============================== 为什么要做这个检查 ==============================
organized_content.json 里全部 organized_parts 的 source_spans 合起来,应该
精确无缝、不重叠地切分整条conclusion的原始content(compute_original_segment_
spans 保证了这一点,见 content_organizer/position_alignment.py)。也就是说,
content 里的每一个字符,理论上都唯一属于某一个 part 的某一段 source_span。

但 step3a 的定位算法(target_locator.py)是直接在整条 content 上做归一化+
子串搜索,并不知道 organized_parts 的边界在哪里——如果归一化过程(尤其是
会整体去掉空白的L1/L2两层)恰好把两个相邻 part 之间的空白抹掉,理论上
存在"定位出来的位置,一头扎进A part的尾巴、另一头连到B part的开头"这种
跨越两个part边界的可能,这样的位置虽然文字上能通过归一化匹配,但并不
对应原始标注里任何一个真实的语义单元,应该被当成可疑结果处理。

这个脚本就是用来检测这种情况:对 step3a 产出的每一个位置,看它是否完整地
落在某一个 part 的某一段 source_span 内。落在某个span内 -> 记录这个
part的label/number;哪个span都装不下(说明跨越了part边界)-> 明确报告
出来,不要沉默地当成没发生过。

============================== 处理逻辑 ==============================
  1. 读 organized_content.json,把每条conclusion里所有 organized_parts 的
     source_spans 展开成"逐段"的列表(一个claim如果因为被打断产生了多个
     source_spans,这里拆成多条,每条各自的label/number/content都跟原来
     的part一致,只是start/end取这一段自己的范围——用来做包含关系检测,
     不代表这一段区间单独的文字)。
  2. 读 step3a 的产物 instance_target_positions.json。
  3. 对每个 target:
       - certainty=="not_found" -> 跳过,没有位置可查。
       - certainty=="certain"   -> 检查这一个位置。
       - certainty=="uncertain" -> 对 candidates 里每一个位置分别检查。
     每个位置检查:在展开后的span列表里,找出所有"start<=位置start 且
     位置end<=span的end"的片段,记录它们的label/number/content。
  4. 汇总写入 data/<paper_id>/instance_target_containment_check.json。

============================== 输出格式 ==============================
一个列表,每个"instance的某个target"一条:
    {
      "conclusion": "conclusion_1",
      "instance_number": 13,
      "term": "$k$",
      "certainty": "uncertain",
      "checks": [
        {
          "content": "$k$", "start": 571, "end": 574,
          "contained_in": [{"label": "论证", "number": 12, "content": "..."}]
        },
        {
          "content": "$k$", "start": 651, "end": 654,
          "contained_in": [{"label": "elaboration", "number": 12, "content": "..."}]
        }
      ]
    }
"checks" 在 certainty=="certain" 时只有1项(对应那一个位置),在
certainty=="uncertain" 时跟 candidates 一一对应。"contained_in" 正常情况
应该恰好有1个元素;0个元素代表这个位置没有完整落在任何一个part的span内
(跨越了part边界,需要人工核查);理论上不应该出现1个以上(source_spans
本该是无缝不重叠的切分),如果出现了,同样如实列出来,不隐藏。

路径解析基于本文件自身所在位置,预期跟 step3a_locate_instance_targets.py
同放在 step3_instance_extraction/ 目录下。
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"


def build_span_index(conclusion: dict) -> list:
    """把一条conclusion里全部 organized_parts 的 source_spans 展开成逐段
    列表:[{"label":..., "number":..., "content":..., "start":..., "end":...}, ...]
    一个part如果有多段 source_spans(claim被打断产生多个不连续片段的情况),
    这里按段拆开,每段各自带上同一个 label/number/content,只是 start/end
    是这一段自己的范围。
    """
    spans = []
    for part in conclusion["organized_parts"]:
        for sp in part["source_spans"]:
            spans.append({
                "label": part["label"],
                "number": part["number"],
                "content": part["content"],
                "start": sp["start"],
                "end": sp["end"],
            })
    return spans


def find_containing_spans(start: int, end: int, spans: list) -> list:
    """找出 spans 里所有完整包含 [start, end) 的片段,返回它们的
    label/number/content(不含start/end——那是片段自己的范围,跟"被包含的
    这个位置"是两回事,调用方已经知道被检查位置的start/end了)。"""
    return [
        {"label": s["label"], "number": s["number"], "content": s["content"]}
        for s in spans
        if s["start"] <= start and end <= s["end"]
    ]


def process_paper(paper_id: str) -> list:
    organized_path = DATA_DIR / paper_id / "organized_content.json"
    targets_path = DATA_DIR / paper_id / "instance_target_positions.json"

    if not organized_path.exists():
        print(f"跳过 {paper_id}: 找不到 {organized_path}")
        return []
    if not targets_path.exists():
        print(f"跳过 {paper_id}: 找不到 {targets_path}(需要先跑完 step3a_locate_instance_targets.py)")
        return []

    conclusions = json.loads(organized_path.read_text(encoding="utf-8"))
    span_index_by_conclusion = {
        c["id"].split("::")[-1]: build_span_index(c) for c in conclusions
    }

    instance_targets = json.loads(targets_path.read_text(encoding="utf-8"))
    results = []

    for item in instance_targets:
        conc_short = item["conclusion"]
        spans = span_index_by_conclusion.get(conc_short, [])

        for t in item["targets"]:
            if t["certainty"] == "not_found":
                continue  # 没有位置,没什么可查的

            if t["certainty"] == "certain":
                positions = [{"content": t["content"], "start": t["start"], "end": t["end"]}]
            else:  # uncertain
                positions = t["candidates"]

            checks = []
            for pos in positions:
                contained_in = find_containing_spans(pos["start"], pos["end"], spans)
                checks.append({
                    "content": pos["content"],
                    "start": pos["start"],
                    "end": pos["end"],
                    "contained_in": contained_in,
                })

            results.append({
                "conclusion": conc_short,
                "instance_number": item["instance_number"],
                "term": t["term"],
                "certainty": t["certainty"],
                "checks": checks,
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

    total_checks = 0
    total_zero = 0     # 哪个span都装不下(跨越了part边界)
    total_multi = 0    # 装进了1个以上的span(理论上不该发生,source_spans该是无缝不重叠的)

    for paper_id in paper_ids:
        results = process_paper(paper_id)
        if not results:
            continue

        out_path = DATA_DIR / paper_id / "instance_target_containment_check.json"
        out_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        n_checks = sum(len(r["checks"]) for r in results)
        zero = [
            (r["conclusion"], r["instance_number"], r["term"], c["content"], c["start"], c["end"])
            for r in results for c in r["checks"] if len(c["contained_in"]) == 0
        ]
        multi = [
            (r["conclusion"], r["instance_number"], r["term"], c["content"], [ci["label"] for ci in c["contained_in"]])
            for r in results for c in r["checks"] if len(c["contained_in"]) > 1
        ]

        total_checks += n_checks
        total_zero += len(zero)
        total_multi += len(multi)

        msg = f"{paper_id}: {n_checks} 个位置检查,写入 {out_path}"
        if zero:
            msg += f"\n  !! {len(zero)} 个位置没落在任何part的span内(跨越了part边界): {zero}"
        if multi:
            msg += f"\n  !! {len(multi)} 个位置同时落在多个part的span内(不应该发生): {multi}"
        print(msg)

    print(
        f"\n全部完成,共检查 {total_checks} 个位置"
        + (f",其中 {total_zero} 个没有落在任何span内、{total_multi} 个落在多个span内,需要核查"
           if (total_zero or total_multi)
           else "，全部位置都恰好完整落在某一个part的span内")
    )


if __name__ == "__main__":
    main()
