"""
resume_paper_with_existing_batch.py

用法:
    python resume_paper_with_existing_batch.py <paper_id> <batch_id>

给一篇论文接上一个已经提交过、但脚本没能正常处理完(比如中途崩了)的
batch,不重新提交,直接用这个batch_id去查、校验、按正常流程走完剩下的
升级/下游步骤——用来处理"batch其实已经在Anthropic那边正常提交/处理,
但本地脚本没能跟上"这种情况,避免重复提交、浪费已经花出去的钱。

============================== 只支持接续"第1档"提交的batch ==============================
这个脚本假定给的batch_id是这篇论文第1档(Sonnet 5, thinking=xhigh)
提交的那个批次——如果崩溃发生在更晚的阶段(已经升级到第2、3档之后),
这个脚本会把它当成第1档处理,状态标签("ok"/"ok_after_retry_..."等)
会不准确(虽然不影响后续实例化/关系构建这些下游步骤,只是这个字段
记录的"是第几档过的"这个溯源信息会不对)。目前这个工具只覆盖"崩溃
发生在第1档提交之后、还没来得及升级"这种最常见的情况;如果你崩溃的
时候已经在第2或第3档,告诉我,我可以加一个显式指定tier的参数。

复用 run_multi_paper_step1a_batch.py 的 run_multi_paper_pipeline(),
只是多传了 existing_batch_ids 这个参数让它跳过重新提交这一步,后面
校验/升级/下游全部走完全一样的代码。
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import run_multi_paper_step1a_batch as batch_pipeline  # noqa: E402


def main():
    if len(sys.argv) < 3:
        print("用法: python resume_paper_with_existing_batch.py <paper_id> <batch_id>")
        sys.exit(1)

    paper_id, batch_id = sys.argv[1], sys.argv[2]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("没检测到 ANTHROPIC_API_KEY 环境变量,先设置好再跑。")
        sys.exit(1)

    batch_pipeline.run_multi_paper_pipeline([paper_id], existing_batch_ids={paper_id: batch_id})


if __name__ == "__main__":
    main()
