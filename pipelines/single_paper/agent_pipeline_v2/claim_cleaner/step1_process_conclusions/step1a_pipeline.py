"""
step1a_pipeline.py

用法:
    python step1a_pipeline.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

做的事:
  1. 在项目根目录(本文件所在目录的上一级)下的 data/<paper_id>/ 里找 graph.json
  2. 抽取所有 kind=="conclusion" 的节点
  3. 对每条 conclusion:读 step1a_prompt_template.txt 的内容,跟这条 conclusion
     的 content 拼接成最终输入文本,调用 Claude API(通过项目根目录下的
     claude_api_call.call_claude)
  4. 校验+矫正返回结果的格式(具体校验/矫正规则见同目录下的 label_validator.py):
       - 拿到 None(被跳过) -> 直接走"trivial兜底":整个 content 标成一条 assertion(number设为1)
       - 格式不对(不是 None,但没通过校验) -> 依次尝试:
           第一次: Sonnet 5,思考深度xhigh,max_tokens=50000
           第二次: Opus 5,关闭思考,max_tokens=10000
           第三次: Sonnet 5,关闭思考,max_tokens=50000
         每一步都在prompt末尾加一句强调格式的提醒;只要某一步拿到的是 None(被跳过),
         或者三次都没能通过格式校验,就按 trivial 兜底处理
       - 格式对了(不管是哪一次)-> 采用矫正后的结果(label已被
         label_validator 改写成统一的标准形式)
  5. 把所有 conclusion 的处理结果(原字段 + 新增字段)汇总成一个列表,写成
     **一个** 文件 conclusions_labeled.json,存到 data/<paper_id>/ 目录下。
     每处理完一条就把当前累积结果整体重写一次,防止中途(比如卡在人工重试/跳过
     的暂停上)被打断导致已处理的进度丢失。

路径全部基于本脚本自身所在位置计算,没有写死任何绝对路径。只要本脚本、
step1a_prompt_template.txt、label_validator.py、上一级目录里的 claude_api_call.py、
上一级目录里的 data/ 之间的相对位置不变,整个 paper_graph2ir 项目文件夹挪到哪个盘、
哪个路径都能正常运行。

预期的目录结构:
    paper_graph2ir/
    ├── claude_api_call.py
    ├── data/
    │   └── <paper_id>/
    │       └── graph.json
    └── step1_process_conclusions/
        ├── step1a_pipeline.py           <- 本文件
        ├── step1a_prompt_template.txt
        └── label_validator.py
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

for _p in (PROJECT_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from claude_api_call import call_claude  # noqa: E402  (需要先把PROJECT_ROOT加进sys.path再import)
from label_validator import validate_and_correct  # noqa: E402  (需要先把SCRIPT_DIR加进sys.path再import)

DATA_DIR = PROJECT_ROOT / "data"
PROMPT_TEMPLATE_PATH = SCRIPT_DIR / "step1a_prompt_template.txt"

# 格式不对(非None)时,第二次调用在prompt末尾追加的强调语
RETRY_REMINDER = (
    "\n\nIMPORTANT: your previous reply did not follow the required JSON format. "
    "Please strictly follow the exact JSON output format specified above, and output nothing else."
)


def extract_conclusions(graph_path: Path) -> list[dict]:
    """从 graph.json 里抽取所有 kind=="conclusion" 的节点。"""
    with open(graph_path, encoding="utf-8-sig") as f:
        graph = json.load(f)
    paper = graph["data"]["papers"][0]
    nodes = paper["graph"]["nodes"]
    conclusions = [n for n in nodes if n.get("kind") == "conclusion"]
    conclusions.sort(key=lambda n: n.get("order", 0))
    return [
        {
            "id": n["id"],
            "global_id": n["global_id"],
            "order": n.get("order"),
            "title": n["title"],
            "content": n["content"],
        }
        for n in conclusions
    ]


def build_prompt(content: str, extra_reminder: str = "") -> str:
    """读模板文件 + 拼接conclusion的content(+可选的强调提醒),组成最终输入文本。"""
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.rstrip("\n") + "\n" + content + extra_reminder


def trivial_fallback(content: str) -> dict:
    """兜底答案:整段content标成一条 assertion,number设为1。"""
    return {"segments": [{"text": content, "label": "assertion", "number": 1}]}


def process_conclusion(conclusion: dict) -> dict:
    content = conclusion["content"]

    # 第一次: Sonnet 5,思考深度xhigh,max_tokens=50000
    raw_answer = call_claude(
        build_prompt(content),
        model="claude-sonnet-5",
        thinking={"type": "adaptive", "effort": "xhigh"},
        max_tokens=50000,
    )
    parsed = validate_and_correct(raw_answer, content)
    status = "ok"

    if parsed is None and raw_answer is not None:
        # 第一次拿到了回复但格式不对(不是None) -> 第二次: Opus 5,关闭思考,max_tokens=10000
        raw_answer = call_claude(
            build_prompt(content, RETRY_REMINDER),
            model="claude-opus-5",
            thinking={"type": "disabled"},
            max_tokens=10000,
        )
        parsed = validate_and_correct(raw_answer, content)
        status = "ok_after_retry_opus5"

        if parsed is None and raw_answer is not None:
            # 第二次拿到了回复但格式还是不对(不是None) -> 第三次: Sonnet 5,关闭思考,max_tokens=50000
            raw_answer = call_claude(
                build_prompt(content, RETRY_REMINDER),
                model="claude-sonnet-5",
                thinking={"type": "disabled"},
                max_tokens=50000,
            )
            parsed = validate_and_correct(raw_answer, content)
            status = "ok_after_retry_sonnet5_nothink"

    if parsed is None:
        # 三次里任何一次拿到的是None(被跳过),或者三次都没能通过格式校验 -> 一律兜底
        parsed = trivial_fallback(content)
        status = "fallback_trivial"

    result = dict(conclusion)
    result["labeled_segments"] = parsed
    result["labeling_status"] = status
    return result


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    graph_path = paper_dir / "graph.json"

    if not graph_path.exists():
        print(f"找不到文件: {graph_path}")
        sys.exit(1)

    conclusions = extract_conclusions(graph_path)
    print(f"共找到 {len(conclusions)} 条 conclusion")

    out_path = paper_dir / "conclusions_labeled.json"
    results = []

    for c in conclusions:
        name = c["id"].split("::")[-1]
        print(f"\n处理 {name} ...")
        result = process_conclusion(c)
        results.append(result)

        # 每处理完一条就整体重写一次,避免中途被打断(比如卡在人工重试/跳过)导致进度丢失
        out_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  -> 状态: {result['labeling_status']}")

    print(f"\n全部处理完成,共 {len(results)} 条,已写入 {out_path}")


if __name__ == "__main__":
    main()
