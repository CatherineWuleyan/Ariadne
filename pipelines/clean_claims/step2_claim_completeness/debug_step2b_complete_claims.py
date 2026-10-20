"""
debug_step2b_complete_claims.py

对一个指定的conclusion,跑一遍完整的三档"补全完整表述"流程(Sonnet5思考
medium -> Opus5不思考 -> Sonnet5不思考,失败则原文照抄兜底),打印出实际
发给模型的prompt、模型的原始返回、以及校验通过后的最终结果——只读,不会
写回 claim_completeness_analysis.json,单纯用来看某一个conclusion的
实际表现。

跟正式流程(step2b_complete_claims.py的main())的区别:
  - 只处理一个指定的conclusion,不是整篇论文
  - 不写文件,跑完就完了,方便反复试同一个conclusion而不用担心覆盖已有
    结果
  - 用 interrupt_on_error=False 调用,报错会直接打印完整异常信息,不会
    弹交互式菜单卡住

用法:
    python debug_step2b_complete_claims.py <paper_id> [conclusion]
其中 conclusion 可以是:
    - 一个整数(默认0):第几个"至少含一条需要补全的claim"的conclusion,
      按它们在organized_content.json里出现的顺序数
    - 一个conclusion的短id(比如"conclusion_2"):按id精确匹配

例如:
    python debug_step2b_complete_claims.py 1032903864883347458 conclusion_2
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

for _p in (PROJECT_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import step2b_complete_claims as s2b  # noqa: E402  (复用已经写好、测过的逻辑,不重复造轮子)

DATA_DIR = s2b.DATA_DIR


def select_group(groups: list, selector: str):
    if selector.isdigit():
        idx = int(selector)
        if idx >= len(groups):
            print(f"索引{idx}超出范围,这篇论文一共只有{len(groups)}个含待补全claim的conclusion")
            sys.exit(1)
        return groups[idx]

    for conc_short, all_parts, target_numbers in groups:
        if conc_short == selector:
            return conc_short, all_parts, target_numbers

    available = [g[0] for g in groups]
    print(f"没找到conclusion短id为{selector!r}的分组,可选的有: {available}")
    sys.exit(1)


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    selector = sys.argv[2] if len(sys.argv) > 2 else "0"

    organized_path = DATA_DIR / paper_id / "organized_content.json"
    completeness_path = DATA_DIR / paper_id / "claim_completeness_analysis.json"

    if not organized_path.exists():
        print(f"找不到文件: {organized_path}")
        sys.exit(1)
    if not completeness_path.exists():
        print(f"找不到文件: {completeness_path}(需要先跑完 step2a_check_pure_data.py)")
        sys.exit(1)

    records = json.loads(completeness_path.read_text(encoding="utf-8"))
    pure_data_lookup = {(r["conclusion"], r["number"]): r["是纯实验数据"] for r in records}

    groups = s2b.group_conclusions(organized_path, pure_data_lookup)
    if not groups:
        print(f"{paper_id} 里没有任何含待补全claim的conclusion")
        sys.exit(1)

    conc_short, all_parts, target_numbers = select_group(groups, selector)

    print(f"论文: {paper_id}")
    print(f"conclusion: {conc_short}")
    print(f"需要补全的claim编号: {target_numbers}")
    print()

    print("=== 这个conclusion的全部parts(原文,含label) ===")
    for p in all_parts:
        marker = " <== 需补全" if p["number"] in target_numbers else ""
        print(f"  [{p['number']}] ({p['label']}) {p['content'][:100]}{marker}")
    print()

    prompt = s2b.build_prompt(all_parts, target_numbers)
    print("=== 实际发给模型的prompt ===")
    print(prompt)
    print()

    print(
        f"发起API调用(process_conclusion,完整三档链路: "
        f"{s2b.MODEL}+thinking -> {s2b.RETRY_MODEL_1}不思考 -> {s2b.RETRY_MODEL_2}不思考) ...\n"
    )
    completed, status = s2b.process_conclusion(all_parts, target_numbers, interrupt_on_error=False)

    print(f"=== 最终结果(状态: {status}) ===")
    for number in target_numbers:
        fields = completed[number]
        print(f"\n[{number}] 完整表述:")
        print(f"  {fields['完整表述']}")
        if fields["需要更多上下文"]:
            print(f"  需要更多上下文: {fields['需要更多上下文']}")


if __name__ == "__main__":
    main()
