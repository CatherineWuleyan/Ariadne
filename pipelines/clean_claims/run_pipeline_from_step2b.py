"""
run_pipeline_from_step2b.py

用法:
    python run_pipeline_from_step2b.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

跑一篇论文的流水线,但从step2b开始,跳过step1a/step1b/step2a——适用于
"这篇论文的organized_content.json和claim_completeness_analysis.json
(带着step2a已经写好的是纯实验数据字段)都已经跑过了,不需要重新跑,
只想从step2b(或者step2b用的prompt改过、想验证改动效果)往后重新跑
到底"这种场景,不用为了测step2b以后的改动而把step1a/step1b/step2a
这几步不必要地重新跑一遍(既浪费时间也浪费API调用)。

============================== 跟run_full_pipeline.py的关系 ==============================
不重新写一遍"跑一串脚本、检查退出码、检查预期文件是否生成"这套逻辑,
直接import run_full_pipeline.py,复用它的STEPS列表(切掉前3项step1a/
step1b/step2a,只留下从step2b开始的11步)和run_step()函数——两个脚本
用的是完全一样的判断"这一步是不是真的成功了"的标准(退出码0且预期
文件确实存在,不是只看退出码,原因见run_full_pipeline.py自己的文档
说明)。

============================== 前提 ==============================
这篇论文的 data/<paper_id>/organized_content.json 和
claim_completeness_analysis.json(带着is_pure_data/是纯实验数据字段)
必须已经存在——这两个不是这个脚本产出的,是step1b/step2a的产出。缺了
的话,第一步(step2b)自己就会打印"找不到文件"退出,这个脚本会照常
把它当成失败处理、停在这里,不需要额外的检查逻辑。

路径解析基于本文件自身位置,预期放在项目根目录下,跟
run_full_pipeline.py 同级。
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import run_full_pipeline as sync_pipeline  # noqa: E402

STEPS_FROM_STEP2B = sync_pipeline.STEPS[3:]  # 跳过step1a/step1b/step2a,留下step2b起的11步


def run_from_step2b(paper_id: str) -> bool:
    """从step2b开始跑完剩下11步,返回是不是全部成功了。任何一步失败就
    停止、返回False,不会继续跑后面依赖它的步骤。"""
    paper_dir = sync_pipeline.DATA_DIR / paper_id
    total = len(STEPS_FROM_STEP2B)

    for i, (script_rel_path, expected_output, needs_api) in enumerate(STEPS_FROM_STEP2B, start=1):
        script_path = PROJECT_ROOT / script_rel_path
        if not script_path.exists():
            print(f"\n!! 找不到脚本: {script_path},流程在这里停止")
            return False

        tag = "[调API]" if needs_api else ""
        print(f"\n{'=' * 70}\n[{i}/{total}] {script_rel_path} {tag}\n{'=' * 70}")

        ok = sync_pipeline.run_step(script_path, paper_dir / expected_output, paper_id)
        if not ok:
            print(f"\n在第 {i}/{total} 步(从step2b算起,即{script_rel_path})失败,已停止,paper_id={paper_id}")
            return False

    print(f"\n{'=' * 70}\n从step2b开始的全部 {total} 步跑完,paper_id={paper_id}\n{'=' * 70}")
    return True


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("流水线里有几步需要调用API,但没检测到 ANTHROPIC_API_KEY 环境变量,先设置好再跑。")
        sys.exit(1)

    ok = run_from_step2b(paper_id)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
