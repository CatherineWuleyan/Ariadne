"""
run_full_pipeline.py

用法:
    python run_full_pipeline.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

跑一篇论文的完整流水线,从step1a一路跑到step4d,按顺序依次调用每一步
脚本。用subprocess跑,不是import——这14个脚本各自都是"main()自己从
sys.argv/input()读paper_id"这个既定写法,互相之间又有一些同名的模块级
常量/函数(比如好几个文件都有MODEL/THINKING这些名字,step3c和step4c还
各自import了step2d的常量),直接import全部塞进一个Python进程风险更高,
不如老老实实按平时手动跑的方式,一个个用subprocess调用——顺序、环境、
行为都跟手动一步步敲命令完全一致,子进程的输出也会直接透传到这边的
终端上,不会被吞掉或者延迟显示。

============================== 完整流水线,共14步 ==============================
 1. step1_process_conclusions/step1a_pipeline.py                    [调API]
 2. step1_process_conclusions/step1b_organize_labeled_content.py
 3. step2_claim_completeness/step2a_check_pure_data.py               [调API]
 4. step2_claim_completeness/step2b_complete_claims.py               [调API]
 5. step2_claim_completeness/step2c_check_instance_containment.py    [调API]
 6. step2_claim_completeness/step2d_remove_instance_content.py       [调API]
 7. step2_claim_completeness/step2e_finalize_claims.py
 8. step3_instance_extraction/step3a_locate_instance_targets.py
 9. step3_instance_extraction/step3b_classify_claim_instances.py
10. step3_instance_extraction/step3c_instantiate_claims.py           [调API]
11. step4_relation_construction/step4a_build_claims_final.py
12. step4_relation_construction/step4b_add_instantiated_claims.py
13. step4_relation_construction/step4c_review_example_relations.py   [调API]
14. step4_relation_construction/step4d_normalize_instance_relations.py

第1步还依赖 data/<paper_id>/graph.json 这个更上游的输入(不是本流水线
任何一步产出的,是外部系统给的),这个文件必须已经存在,不然第1步自己会
报"找不到文件"退出,流程到这就停了。

============================== 怎么判断某一步"真的成功了" ==============================
不能只看子进程退出码是不是0——step1b_organize_labeled_content.py在缺输入
文件(conclusions_labeled.json不存在)时,用的是sys.exit(0)表示"正常跳过",
不是sys.exit(1)那种"出错"的退出码。如果只看退出码,这一步"其实什么都
没做"会被误判成"成功了",紧接着第2步会拿着一个根本不存在的
organized_content.json去跑,报出一个跟真正原因(其实是第1步没有产出)
不直接相关、更难排查的错误。

所以每一步跑完,除了检查退出码,还会额外检查这一步理应产出/更新的那个
文件是不是真的存在;哪怕退出码是0,这个文件没生成,也当成这一步失败,
把两方面的情况都打印清楚,不是只报退出码。这个检查对"每次都会更新同一份
文件"的那几步(比如step2b/c/d都在改claim_completeness_analysis.json,
step4b/c/d都在改claims_final.json)覆盖没那么精确(没法确认"这一步自己
真的往里面加了东西",只能确认"这个文件还在"),但至少能兜住"这一步完全
没跑起来、文件从始至终没被建出来"这种最坏的情况。

============================== 失败即停 ==============================
任何一步没有"真的成功",整个流程就在这一步停下,不会硬着头皮继续跑
后面的步骤——后面每一步都依赖前面的产出,带着一个没做完的上游状态往下
跑,只会跑出一堆更难排查的连锁错误,不如直接停在问题发生的地方。

路径解析基于本文件自身位置,预期放在项目根目录下,跟
step1_process_conclusions/、step2_claim_completeness/、
step3_instance_extraction/、step4_relation_construction/ 四个目录同级。
"""

import os
import sys
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"

# (脚本相对路径, 这一步跑完理应存在的文件名(相对data/<paper_id>/), 是否调用API)
STEPS = [
    ("step1_process_conclusions/step1a_pipeline.py", "conclusions_labeled.json", True),
    ("step1_process_conclusions/step1b_organize_labeled_content.py", "organized_content.json", False),
    ("step2_claim_completeness/step2a_check_pure_data.py", "claim_completeness_analysis.json", True),
    ("step2_claim_completeness/step2b_complete_claims.py", "claim_completeness_analysis.json", True),
    ("step2_claim_completeness/step2c_check_instance_containment.py", "claim_completeness_analysis.json", True),
    ("step2_claim_completeness/step2d_remove_instance_content.py", "claim_completeness_analysis.json", True),
    ("step2_claim_completeness/step2e_finalize_claims.py", "step2_output_claims.json", False),
    ("step3_instance_extraction/step3a_locate_instance_targets.py", "instance_target_positions.json", False),
    ("step3_instance_extraction/step3b_classify_claim_instances.py", "claim_instance_associations.json", False),
    ("step3_instance_extraction/step3c_instantiate_claims.py", "special_claims.json", True),
    ("step4_relation_construction/step4a_build_claims_final.py", "claims_final.json", False),
    ("step4_relation_construction/step4b_add_instantiated_claims.py", "claims_final.json", False),
    ("step4_relation_construction/step4c_review_example_relations.py", "claims_final.json", True),
    ("step4_relation_construction/step4d_normalize_instance_relations.py", "claims_final.json", False),
]


def run_step(script_path: Path, expected_output_path: Path, paper_id: str, *, non_interactive: bool = False) -> bool:
    """跑一步,返回是不是"真的成功了"(退出码0,且预期文件确实存在)。
    子进程的stdout/stderr直接透传到当前终端,不做捕获或转发,所以这一步
    自己打印的进度/结果信息用户能实时看到,不需要这里重复搬运。"""
    options = {"stdin": subprocess.DEVNULL} if non_interactive else {}
    result = subprocess.run([sys.executable, str(script_path), paper_id], **options)

    if result.returncode != 0:
        print(f"\n!! {script_path.name} 退出码 {result.returncode},流程在这里停止")
        return False

    if not expected_output_path.exists():
        print(
            f"\n!! {script_path.name} 退出码是0,但没有生成预期的 {expected_output_path}"
            "(比如上游输入缺失、这一步判定'没什么可做'就直接跳过了)"
        )
        print("流程在这里停止,不继续跑后面依赖这个文件的步骤")
        return False

    return True


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    key_name = "DEEPSEEK_API_KEY" if os.environ.get("DEEPSEEK_MODEL") else "ANTHROPIC_API_KEY"
    if not os.environ.get(key_name):
        print(f"流水线需要配置 {key_name} 环境变量。")
        sys.exit(1)

    paper_dir = DATA_DIR / paper_id
    total = len(STEPS)

    for i, (script_rel_path, expected_output, needs_api) in enumerate(STEPS, start=1):
        script_path = PROJECT_ROOT / script_rel_path
        if not script_path.exists():
            print(f"\n!! 找不到脚本: {script_path},流程在这里停止")
            sys.exit(1)

        tag = "[调API]" if needs_api else ""
        print(f"\n{'=' * 70}")
        print(f"[{i}/{total}] {script_rel_path} {tag}")
        print("=" * 70)

        ok = run_step(script_path, paper_dir / expected_output, paper_id)
        if not ok:
            print(f"\n在第 {i}/{total} 步({script_rel_path})失败,已停止,paper_id={paper_id}")
            sys.exit(1)

    print(f"\n{'=' * 70}")
    print(f"全部 {total} 步跑完,paper_id={paper_id}")
    print("=" * 70)


if __name__ == "__main__":
    main()
