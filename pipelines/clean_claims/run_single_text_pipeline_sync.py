"""
run_single_text_pipeline_sync.py

用法:
    python run_single_text_pipeline_sync.py "一段文本"
    python run_single_text_pipeline_sync.py --file mytext.txt

对一段文本跑完整的14步流水线,step1a走同步/流式调用(跟
run_full_pipeline.py处理一整篇论文时用的是同一份代码,不是另外写的
简化版)。

============================== 怎么把一段文本接进"按paper_id组织"的流水线 ==============================
调 build_single_text_graph.build_graph_for_text(),把这段文本包装成
只有1个conclusion节点的graph.json,用时间戳生成一个paper_id,写到
data/<paper_id>/graph.json——之后这个paper_id在14步流水线眼里,跟一篇
有很多conclusion的真实论文没有任何区别,不需要对这14步本身做任何改动
或者特殊判断。

============================== 复用run_full_pipeline.py,不重新写一遍14步 ==============================
直接import它的STEPS列表(全部14步,含step1a)和run_step()函数,原样
按顺序跑一遍——任何一步失败就停止,打印在第几步失败,不会硬着头皮
继续跑后面注定失败的步骤。

路径解析基于本文件自身位置,预期放在项目根目录下,跟
build_single_text_graph.py、run_full_pipeline.py 同级。
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
import run_full_pipeline as sync_pipeline  # noqa: E402


def run_single_text(text: str, *, raise_on_failure: bool = False) -> str:
    """跑完整个流水线,返回这次生成的paper_id(不管中途有没有失败都会
    返回,方便调用方去data/<paper_id>/下面看跑到哪一步、结果是什么)。
    自动化调用可设 raise_on_failure=True：禁止交互重试，任一步失败即抛错，
    避免把 step4a 已生成但后续清洗未完成的 claims_final.json 当成成功。"""
    paper_id = build_graph_for_text(text, DATA_DIR)
    print(f"已生成 paper_id={paper_id}(对应 data/{paper_id}/graph.json)")

    paper_dir = DATA_DIR / paper_id
    total = len(sync_pipeline.STEPS)

    for i, (script_rel_path, expected_output, needs_api) in enumerate(sync_pipeline.STEPS, start=1):
        script_path = PROJECT_ROOT / script_rel_path
        tag = "[调API]" if needs_api else ""
        print(f"\n{'=' * 70}\n[{i}/{total}] {script_rel_path} {tag}\n{'=' * 70}")

        options = {"non_interactive": True} if raise_on_failure else {}
        ok = sync_pipeline.run_step(script_path, paper_dir / expected_output, paper_id, **options)
        if not ok:
            print(f"\n在第 {i}/{total} 步({script_rel_path})失败,已停止,paper_id={paper_id}")
            if raise_on_failure:
                raise RuntimeError(f"Claim cleaning failed at {script_rel_path}; paper_id={paper_id}")
            return paper_id

    print(f"\n{'=' * 70}\n全部 {total} 步跑完,paper_id={paper_id}\n{'=' * 70}")
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

    key_name = "DEEPSEEK_API_KEY" if os.environ.get("DEEPSEEK_MODEL") else "ANTHROPIC_API_KEY"
    if not os.environ.get(key_name):
        print(f"流水线需要配置 {key_name} 环境变量。")
        sys.exit(1)

    run_single_text(text)


if __name__ == "__main__":
    main()
