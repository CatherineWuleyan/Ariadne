"""
step1b_organize_labeled_content.py

对 step1a 已经打好标签的 conclusion(conclusions_labeled.json)做进一步整理:
  - 修复segments原始切分留下的接缝问题(转义符/公式/引号/括号被切断)
  - 把同一个claim(assertion/论据/example三类共享同一个编号池,旧编号相同
    即视为同一个claim)的所有片段合并成一条
  - 废弃旧编号,按所有part在原文中的起始顺序统一重新编号(不分类型)
  - 论据.supports / 论证.supports / example.illustrates / elaboration.of
    (如果有)/ relation.connects+expression 里引用的编号,同步换算成新编号
  - 清理因为合并而产生的冗余定界符,以及内容为空的公式/引号/括号

具体每一步的规则见 content_organizer/ 目录下各模块的说明。

用法:
    python step1b_organize_labeled_content.py <paper_id>
或不带参数运行,会提示输入。

输出:
    在 data/<paper_id>/ 目录下写一个 organized_content.json,结构是一个列表,
    每条conclusion一个块:
        {
          "id": ..., "global_id": ..., "order": ..., "title": ..., "content": ...,
          "organized_parts": [
            {
              "number": 1, "label": "...", "content": "...",
              "source_spans": [{"start": 12, "end": 45}, ...]
            },
            ...
          ]
        }
    source_spans 记录这个part由 conclusions_labeled.json 里原始的哪几个片段
    组成、每个片段在原始 content 字段里精确的起止字符位置(前闭后开,不存
    文本,只存位置)。非claim类型(不是assertion/论据/example)永远只有1个
    元素;claim如果在原文中不连续,就有多个,按原文中出现的先后顺序排列。

    除了 number/label/content/source_spans 这四个每条part都有的字段,部分
    part按自己的label还会带下面这些字段(编号都已经换算成上面这套新编号,
    of_terms 例外——它存的是短语,不涉及编号):
        "论据"        -> "supports": [int, ...]
        "论证"        -> "supports": [int, ...]
        "example"     -> "illustrates": [int, ...]
        "elaboration" -> 可能带 "of": [int, ...] 或 "of_terms": [str, ...] 之一
                          (二者最多出现一个,也可能两个都没有)
        "instance"    -> "of_terms": [str, ...]
        "relation"    -> "connects": [int, ...] 和 "expression": str
    assertion/motivation/framing/connection/other 这五类没有额外字段。

路径解析基于本文件自身所在位置,没有写死绝对路径,paper_graph2ir 整个文件夹
挪到哪里都能正常运行。

预期目录结构:
    paper_graph2ir/
    ├── data/
    │   └── <paper_id>/
    │       ├── graph.json
    │       ├── conclusions_labeled.json
    │       └── organized_content.json      <- 本脚本生成
    └── step1_process_conclusions/
        ├── step1b_organize_labeled_content.py   <- 本文件
        └── content_organizer/
            ├── formula_scanner.py          (用户提供,未修改)
            ├── test_formula_scanner.py     (用户提供,未修改)
            ├── quote_paren_scanner.py
            ├── position_alignment.py
            ├── boundary_patcher.py
            ├── claim_organizer.py
            └── final_cleanup.py
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ORGANIZER_DIR = SCRIPT_DIR / "content_organizer"

for _p in (PROJECT_ROOT, SCRIPT_DIR, ORGANIZER_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from boundary_patcher import patch_all_boundaries  # noqa: E402
from claim_organizer import organize_claims  # noqa: E402
from final_cleanup import cleanup_assertion_seams, remove_empty_wrappers  # noqa: E402
from position_alignment import compute_original_segment_spans  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"

# 除了 number/label/content/source_spans 这四个每条part都有的字段之外,
# 按label可能还带的额外语义字段——显式列白名单,而不是"排除掉已知的内部
# 追踪字段",这样即使上游以后新增了某个纯内部字段(不小心忘了排除),也
# 不会被这里意外带进最终输出。
_EXTRA_OUTPUT_FIELDS = (
    "supports", "illustrates", "of", "of_terms", "connects", "expression",
    "of_terms_self_only",  # of_terms里"只在自己这段找到、原文别处没印证"的警告标记
)


def organize_one_conclusion(conclusion: dict) -> list:
    """
    conclusion: conclusions_labeled.json 里的一条(带 labeled_segments)。
    返回整理后的 organized_parts 列表,每项额外带一个 source_spans 字段:
    这个part由 conclusions_labeled.json 里原始的哪几个片段组成、每个片段
    在原始 content 里精确的起止字符位置(不存文本,只存位置)。
    """
    segments = conclusion["labeled_segments"]["segments"]
    original_content = conclusion["content"]

    # 先算出每个原始片段(未经任何加工)在 content 里精确、不重叠、不留
    # 空档的起止位置,供后面组装 source_spans 用
    raw_texts = [s["text"] for s in segments]
    original_segment_spans = compute_original_segment_spans(original_content, raw_texts)

    patched, end_added, start_added = patch_all_boundaries(segments, original_content)
    final_parts_raw = organize_claims(patched, end_added, start_added)

    # 在 cleanup_assertion_seams 丢掉 piece_orig_indices/orig_idx 这些追踪
    # 字段之前,先按 number 记录好每个part对应哪些原始片段的位置
    number_to_source_spans = {}
    for p in final_parts_raw:
        if "piece_orig_indices" in p:
            idxs = p["piece_orig_indices"]
        else:
            idxs = [p["orig_idx"]]
        number_to_source_spans[p["number"]] = [original_segment_spans[i] for i in idxs]

    final_parts = cleanup_assertion_seams(final_parts_raw, segments, original_content)
    final_parts = [
        {
            "number": p["number"],
            "label": p["label"],
            "content": remove_empty_wrappers(p["content"]),
            "source_spans": number_to_source_spans[p["number"]],
            **{k: p[k] for k in _EXTRA_OUTPUT_FIELDS if k in p},
        }
        for p in final_parts
    ]
    return final_parts


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    labeled_path = paper_dir / "conclusions_labeled.json"

    if not paper_dir.exists():
        print(f"跳过:找不到 paper_id 对应的文件夹 {paper_dir}")
        sys.exit(0)

    if not labeled_path.exists():
        print(f"跳过:{paper_dir} 里没有 conclusions_labeled.json")
        sys.exit(0)

    conclusions = json.loads(labeled_path.read_text(encoding="utf-8"))
    print(f"共读到 {len(conclusions)} 条 conclusion")

    out_path = paper_dir / "organized_content.json"
    results = []

    for c in conclusions:
        name = c["id"].split("::")[-1]
        print(f"整理 {name} ...")
        try:
            organized_parts = organize_one_conclusion(c)
        except Exception as e:
            print(f"  !! 整理失败: {e}")
            raise

        results.append({
            "id": c["id"],
            "global_id": c["global_id"],
            "order": c.get("order"),
            "title": c["title"],
            "content": c["content"],
            "organized_parts": organized_parts,
        })

        out_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(f"\n全部整理完成,共 {len(results)} 条,已写入 {out_path}")


if __name__ == "__main__":
    main()
