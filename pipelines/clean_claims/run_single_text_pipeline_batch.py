"""
run_single_text_pipeline_batch.py

用法:
    python run_single_text_pipeline_batch.py "一段文本"
    python run_single_text_pipeline_batch.py --file mytext.txt

对一段文本跑完整的14步流水线,step1a走Batch API(复用
run_multi_paper_step1a_batch.py的调度逻辑,不是另外写的简化版)——
虽然只有1个conclusion,batch的三档重试/invalid_request换档/服务端
错误同档重试/canceled中止这些机制都照常生效,只是这一批里只有1篇
论文、1条conclusion。

============================== 怎么把一段文本接进"按paper_id组织"的流水线 ==============================
跟同步版(run_single_text_pipeline_sync.py)一样,调
build_single_text_graph.build_graph_for_text()生成一个时间戳paper_id
和对应的graph.json。

============================== 复用run_multi_paper_step1a_batch.py ==============================
直接调它的run_multi_paper_pipeline([paper_id]),传一个只有1个元素的
列表——那个函数本来就是设计成"给一批paper_id,各自独立推进"的,1篇也是
"批"的特例,不需要任何改动。

路径解析基于本文件自身位置,预期放在项目根目录下,跟
build_single_text_graph.py、run_multi_paper_step1a_batch.py 同级。
"""

import os
import sys
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from build_single_text_graph import build_graph_for_text  # noqa: E402
import run_multi_paper_step1a_batch as batch_pipeline  # noqa: E402


def run_single_text(text: str) -> str:
    paper_id = build_graph_for_text(text, DATA_DIR)
    print(f"已生成 paper_id={paper_id}(对应 data/{paper_id}/graph.json)")

    batch_pipeline.run_multi_paper_pipeline([paper_id])
    return paper_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="?", help="要处理的文本(跟--file二选一)")
    parser.add_argument("--file", help="包含待处理文本的文件路径(跟直接传text二选一)")
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8-sig")
    elif args.text:
        text = args.text
    else:
        print("需要传入文本内容(直接传参数,或者用 --file 指定一个文件)")
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("没检测到 ANTHROPIC_API_KEY 环境变量,先设置好再跑。")
        sys.exit(1)

    run_single_text(text)


if __name__ == "__main__":
    main()
