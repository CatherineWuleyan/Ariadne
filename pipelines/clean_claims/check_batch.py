"""
check_batch.py

用法:
    python check_batch.py <batch_id>

查一下某个batch_id现在的状态,跑完了就顺便把结果摘要打出来(每条
custom_id对应succeeded/errored/canceled/expired里的哪一种)。复用
run_multi_paper_step1a_batch.py里已经写好、带无限重试的
_poll_batch_status/_fetch_batch_results,不是另外写一套简化版查询
逻辑——如果查询过程中也遇到网络问题,同样会自动重试、不会直接崩掉。

这个脚本只是"看一眼状态",不会帮你把结果接回流水线继续跑下去——如果
batch已经跑完了、你想让这篇论文接着走完剩下的流程,需要另外处理(比如
重新跑一次完整的run_multi_paper_step1a_batch.py,注意这样会对这篇
论文重新提交新的batch,不会复用这个已经查到的旧batch结果)。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import run_multi_paper_step1a_batch as batch_pipeline  # noqa: E402


def main():
    if len(sys.argv) > 1:
        batch_id = sys.argv[1]
    else:
        batch_id = input("请输入 batch_id: ").strip()

    print(f"查询 {batch_id} ...")
    status = batch_pipeline._poll_batch_status(batch_id)
    print(f"当前状态: {status}")

    if status != "ended":
        print("还没跑完,过一会再来看看。")
        return

    results = batch_pipeline._fetch_batch_results(batch_id)
    print(f"\n跑完了,共 {len(results)} 条结果:")

    from collections import Counter
    kind_counts = Counter(outcome["kind"] for _cid, outcome in results)
    for kind, n in kind_counts.items():
        print(f"  {kind}: {n} 条")

    print()
    for custom_id, outcome in results:
        print(f"  {custom_id}: {outcome['kind']}")


if __name__ == "__main__":
    main()
