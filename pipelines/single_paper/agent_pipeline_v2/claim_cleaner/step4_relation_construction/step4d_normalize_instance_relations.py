"""
step4d_normalize_instance_relations.py

用法:
    python step4d_normalize_instance_relations.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

step4(relation构建)的最后一步:把全部instance型关系("[m] 是 [n] 的
instance",step4b产出的)的expression,改写成跟论据/example关系一样的
"[m] 是 [n] 的例子或证据"格式——不再单独区分"这是instantiation关系"
还是"这是论据/example关系",统一成同一种表达形式。

============================== 只改expression,不改connects ==============================
instance关系的connects本来就是[实例化claim编号, 原claim编号]这个二元组,
形状跟论据/example单目标关系的connects([m, n])完全一样,不需要改;
只需要把expression字符串里的"的instance"后缀换成"的例子或证据"。

============================== 这是真正意义上的最后一步,之后不要再对同一篇论文跑step4b/step4c ==============================
step4c的build_instantiation_lookup是靠"expression以'的instance'结尾"这
个特征,反向识别"这条claim是不是某个原claim的实例化版本"的。这一步跑完
之后,"的instance"这个后缀就不再存在了——如果之后又对同一篇论文跑了一次
step4b(比如special_claims.json有了新内容)或者step4c,会因为找不到这个
后缀而识别不出哪些claim是实例化版本:该被step4c挑出来复核的关系找不到,
而这些已经被本步骤改过格式的instance关系,反而会因为expression也变成
"的例子或证据"结尾,被step4c误当成普通论据/example关系纳入复核候选。
所以这一步执行之后,不应该再对同一篇论文跑step4b或step4c,除非清楚这个
影响并且愿意接受。

============================== 天然幂等 ==============================
跑完一次之后,全部instance关系的expression已经不再以"的instance"结尾,
再跑一次这个脚本,一条能转换的都找不到,不会有任何变化,不需要额外加
"是否已经跑过"的保护。

============================== 输出 ==============================
原地更新 data/<paper_id>/claims_final.json 里全部符合条件的relation
条目的expression字段,其余字段(conclusion/connects)不变。

路径解析基于本文件自身位置,预期跟 step4a/step4b/step4c 同放在
step4_relation_construction/ 目录下。
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"

_INSTANCE_EXPRESSION_SUFFIX = "的instance"


def normalize_instance_relations(claims_final: dict) -> int:
    """原地修改claims_final["relation"]里全部instance型关系的expression,
    返回实际改动的条数。"""
    n_changed = 0
    for r in claims_final["relation"]:
        if r["expression"].endswith(_INSTANCE_EXPRESSION_SUFFIX):
            m, n = r["connects"]
            r["expression"] = f"[{m}] 是 [{n}] 的例子或证据"
            n_changed += 1
    return n_changed


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    claims_final_path = paper_dir / "claims_final.json"
    if not claims_final_path.exists():
        print(f"找不到文件: {claims_final_path}")
        sys.exit(1)

    claims_final = json.loads(claims_final_path.read_text(encoding="utf-8"))
    n_changed = normalize_instance_relations(claims_final)

    claims_final_path.write_text(
        json.dumps(claims_final, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"{n_changed} 条instance关系的expression已改成'...的例子或证据'格式,已写回 {claims_final_path}")


if __name__ == "__main__":
    main()
