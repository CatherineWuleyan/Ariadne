"""
step1a_single_conclusion.py

用法:
    python step1a_single_conclusion.py <paper_id> <conclusion_id>

例如:
    python step1a_single_conclusion.py 867769419944689875 conclusion_4

也支持不传参数,运行后会提示你输入。

做的事:
  1. 在 data/<paper_id>/ 里找 graph.json,找到 kind=="conclusion" 且 id 匹配
     <conclusion_id> 的那个节点(conclusion_id 可以传短名如 "conclusion_4",
     也可以传完整id如 "paper:xxx::conclusion_4",都能匹配到)
  2. 取出它的 content,用跟 step1a_pipeline.py 完全一样的方法处理——直接复用
     step1a_pipeline.process_conclusion(),同一套 prompt 模板、同一套三段式
     重试链(Sonnet 5思考 -> Opus 5不思考 -> Sonnet 5不思考)、同一套
     label_validator 校验+矫正逻辑,不重复造轮子
  3. 把处理结果写进 data/<paper_id>/conclusions_labeled.json:
       - 这个conclusion已经在里面 -> 用新结果覆盖那一条
       - 不在里面 -> 追加一条
       - 这个文件根本不存在 -> 新建一个,格式跟 step1a_pipeline.py 生成的一样,
         只是里面只有这一条

路径解析方式跟 step1a_pipeline.py 一样,基于本文件自身位置计算,没有写死
绝对路径,放在同一个 step1_process_conclusions/ 目录下即可。
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

for _p in (PROJECT_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# 复用 step1a_pipeline.py 里已经写好、测过的处理逻辑,不重复实现
import step1a_pipeline as p1a  # noqa: E402

DATA_DIR = p1a.DATA_DIR


def find_conclusion_node(graph_path: Path, conclusion_id: str) -> dict:
    """
    从 graph.json 里找到 kind=="conclusion" 且 id 匹配 conclusion_id 的节点,
    返回跟 step1a_pipeline.extract_conclusions() 里同样结构的 dict。
    conclusion_id 可以是短名(如 "conclusion_4")或完整id(如 "paper:xxx::conclusion_4")。
    """
    short_id = conclusion_id.split("::")[-1]
    suffix = f"::{short_id}"

    with open(graph_path, encoding="utf-8-sig") as f:
        graph = json.load(f)
    paper = graph["data"]["papers"][0]
    nodes = paper["graph"]["nodes"]

    for n in nodes:
        if n.get("kind") == "conclusion" and n["id"].endswith(suffix):
            return {
                "id": n["id"],
                "global_id": n["global_id"],
                "order": n.get("order"),
                "title": n["title"],
                "content": n["content"],
            }

    raise ValueError(f"在 {graph_path} 里没找到 id 以 {suffix!r} 结尾的 conclusion 节点")


def update_labeled_file(out_path: Path, new_result: dict) -> bool:
    """
    把 new_result 写进 out_path:
      - 文件不存在 -> 新建,只包含这一条
      - 文件存在但没有这个conclusion -> 追加
      - 文件存在且已经有这个conclusion(按id匹配)-> 覆盖那一条
    返回 True 表示覆盖了已有结果,False 表示新增。
    """
    if out_path.exists():
        results = json.loads(out_path.read_text(encoding="utf-8"))
    else:
        results = []

    target_id = new_result["id"]
    replaced = False
    for i, r in enumerate(results):
        if r["id"] == target_id:
            results[i] = new_result
            replaced = True
            break
    if not replaced:
        results.append(new_result)

    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return replaced


def main():
    if len(sys.argv) >= 3:
        paper_id = sys.argv[1]
        conclusion_id = sys.argv[2]
    else:
        paper_id = input("请输入 paper_id: ").strip()
        conclusion_id = input("请输入 conclusion id(比如 conclusion_4): ").strip()

    paper_dir = DATA_DIR / paper_id
    graph_path = paper_dir / "graph.json"

    if not graph_path.exists():
        print(f"找不到文件: {graph_path}")
        sys.exit(1)

    conclusion = find_conclusion_node(graph_path, conclusion_id)
    print(f"找到节点: {conclusion['id']}")

    print("正在处理...")
    result = p1a.process_conclusion(conclusion)
    print(f"  -> 状态: {result['labeling_status']}")

    out_path = paper_dir / "conclusions_labeled.json"
    replaced = update_labeled_file(out_path, result)
    action = "覆盖了已有结果" if replaced else "新增了一条结果"
    print(f"{action},已写入 {out_path}")


if __name__ == "__main__":
    main()
