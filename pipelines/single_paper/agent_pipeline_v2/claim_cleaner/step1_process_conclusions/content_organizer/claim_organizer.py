"""
claim_organizer.py

(原名 assertion_organizer.py——v1 里只有 assertion 会跨片段合并,v2 里
assertion/论据/example 三类都会,所以改名。)

对边界修复过的 segments(label_validator矫正、boundary_patcher修过接缝,
仍按原文顺序、尚未合并)做:

  1. 按旧编号合并同编号的 claim 片段(assertion/论据/example 三类共享同一个
     编号池,即使在原文里不连续也合并),拼接处不管原来有没有空格,强制加
     一个空格。其余八类(论证/motivation/elaboration/instance/relation/
     framing/connection/other)不合并,各自独立成一个part。
     - 如果同一个claim(论据或example)被切成了多段,而且各段自己的
       supports/illustrates没能完全一致,取这几段的并集,按它们在原文中
       出现的先后顺序去重(而不是报错或者只取某一段的值)。
  2. 对所有part统一去空格:claim 类以外的,掐头去尾空格删除、中间连续
     空白压成一个空格;claim 类的去空格留到 final_cleanup 处理完接缝之后
     再做。
  3. 把"所有claim(按其第一个旧片段在原文中的位置)+ 其余八类(按自己在
     原文中的位置)"混在一起,按位置统一重新编号(从1开始,不分类型)。
  4. 把所有引用旧编号的字段换算成新编号:
       - 论据.supports / 论证.supports / example.illustrates /
         elaboration.of(如果有)/ relation.connects:列表里每个数字按
         "旧编号->新编号"映射表换算。
       - relation.expression:字符串里每个"[旧编号]"原地替换成"[新编号]",
         其余文字(连接词、括号)原样保留。
       - elaboration/instance 的 of_terms 是短语,不涉及编号,原样保留。
     label 字段本身不需要改写(label_validator给出的已经是纯类别名)。

不再需要解析label文本——label_validator矫正后的segments已经是干净的
结构化字段(label是纯类别名,number/supports/illustrates/of/of_terms/
connects/expression都是规整过的int/list[int]/list[str]/str),这里直接
读取、直接换算编号即可。只从 label_validator 里引入 CLAIM_LABELS 这一个
共享常量,避免两个文件对"哪三类是claim"这件事各写一份、以后容易走样。
"""

import re
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PARENT_DIR = _THIS_DIR.parent
if str(_PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(_PARENT_DIR))

from label_validator import CLAIM_LABELS  # noqa: E402


def _collapse_whitespace(s: str) -> str:
    """开头/结尾空白删除,中间连续空白压成单个空格。"""
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _remap_numbers(nums, old_to_new: dict) -> list:
    return [old_to_new[n] for n in nums]


def _remap_expression(expr: str, old_to_new: dict) -> str:
    """把expression字符串里每个"[旧编号]"原地替换成"[新编号]",其余文字
    (连接词、括号)原样保留。"""
    return re.sub(r"\[(\d+)\]", lambda m: f"[{old_to_new[int(m.group(1))]}]", expr)


def organize_claims(patched_segments: list, end_added: list = None, start_added: list = None) -> list:
    """
    patched_segments: list[dict],label_validator矫正 + boundary_patcher边界
      修复过的segments,仍按原文顺序。每条至少有 text/label,按label_validator
      的输出契约,还可能有 number/supports/illustrates/of/of_terms/connects/
      expression(具体有哪些取决于label)。
    end_added / start_added: 跟patched_segments等长,记录每个片段结尾/开头
      被边界修复打了多少个补丁字符(boundary_patcher.patch_all_boundaries
      返回的那两个列表)。不传的话默认当全是0处理(向后兼容)。

    返回: list[dict],按新编号排序。
      - claim 类(assertion/论据/example):
          {'number': int, 'label': 'assertion'/'论据'/'example',
           'pieces': [str, ...], 'piece_orig_indices': [int, ...],
           'piece_end_added': [int, ...], 'piece_start_added': [int, ...]}
          另外论据带 'supports'(list[int],已换算新编号),
          example 带 'illustrates'(list[int],已换算新编号)。
      - 其余八类:
          {'number': int, 'label': str, 'content': str, 'orig_idx': int}
          另外按label各自可能带:论证.supports、elaboration.of 或
          elaboration.of_terms、instance.of_terms、relation.connects+
          relation.expression(均已换算新编号,of_terms除外)。
    """
    n = len(patched_segments)
    if end_added is None:
        end_added = [0] * n
    if start_added is None:
        start_added = [0] * n

    # old_num(int) -> 这个claim分组的累积信息
    claim_groups = {}
    combined = []

    for idx, seg in enumerate(patched_segments):
        label = seg["label"]
        if label in CLAIM_LABELS:
            old_num = seg["number"]
            if old_num not in claim_groups:
                claim_groups[old_num] = {
                    "first_idx": idx, "label": label,
                    "texts": [], "orig_idx": [], "end_added": [], "start_added": [],
                    "refs": [],  # 论据的supports / example的illustrates,按片段出现顺序收集,稍后合并去重
                }
            g = claim_groups[old_num]
            g["texts"].append(seg["text"])
            g["orig_idx"].append(idx)
            g["end_added"].append(end_added[idx])
            g["start_added"].append(start_added[idx])
            if label == "论据" and "supports" in seg:
                g["refs"].append(seg["supports"])
            elif label == "example" and "illustrates" in seg:
                g["refs"].append(seg["illustrates"])
        else:
            combined.append({
                "first_idx": idx,
                "label": label,
                "content_raw": seg["text"],
                "seg": seg,
            })

    for old_num, info in claim_groups.items():
        merged_refs = []
        for ref_list in info["refs"]:
            for x in ref_list:
                if x not in merged_refs:
                    merged_refs.append(x)
        combined.append({
            "first_idx": info["first_idx"],
            "label": info["label"],
            "old_num": old_num,
            "pieces": info["texts"],
            "piece_orig_indices": info["orig_idx"],
            "piece_end_added": info["end_added"],
            "piece_start_added": info["start_added"],
            "merged_refs": merged_refs,
        })

    combined.sort(key=lambda x: x["first_idx"])

    # 非claim类:统一去空格(claim类的去空格留到 final_cleanup 处理完接缝之后再做)
    for item in combined:
        if item["label"] not in CLAIM_LABELS:
            item["content_clean"] = _collapse_whitespace(item["content_raw"])

    old_to_new = {}
    for new_num, item in enumerate(combined, start=1):
        item["new_num"] = new_num
        if item["label"] in CLAIM_LABELS:
            old_to_new[item["old_num"]] = new_num

    final_parts = []
    for item in combined:
        label = item["label"]

        if label in CLAIM_LABELS:
            part = {
                "number": item["new_num"],
                "label": label,
                "pieces": item["pieces"],
                "piece_orig_indices": item["piece_orig_indices"],
                "piece_end_added": item["piece_end_added"],
                "piece_start_added": item["piece_start_added"],
            }
            if label == "论据":
                part["supports"] = _remap_numbers(item["merged_refs"], old_to_new)
            elif label == "example":
                part["illustrates"] = _remap_numbers(item["merged_refs"], old_to_new)
            final_parts.append(part)
            continue

        seg = item["seg"]
        part = {
            "number": item["new_num"],
            "label": label,
            "content": item["content_clean"],
            "orig_idx": item["first_idx"],
        }

        if label == "论证":
            part["supports"] = _remap_numbers(seg["supports"], old_to_new)
        elif label == "elaboration":
            if "of" in seg:
                part["of"] = _remap_numbers(seg["of"], old_to_new)
            elif "of_terms" in seg:
                part["of_terms"] = seg["of_terms"]
                if "of_terms_self_only" in seg:
                    part["of_terms_self_only"] = seg["of_terms_self_only"]
        elif label == "instance":
            part["of_terms"] = seg["of_terms"]
            if "of_terms_self_only" in seg:
                part["of_terms_self_only"] = seg["of_terms_self_only"]
        elif label == "relation":
            part["connects"] = _remap_numbers(seg["connects"], old_to_new)
            part["expression"] = _remap_expression(seg["expression"], old_to_new)
        # motivation / framing / connection / other: 没有额外字段

        final_parts.append(part)

    return final_parts
