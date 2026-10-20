"""
step2e_finalize_claims.py

用法:
    python step2e_finalize_claims.py <paper_id>
或不带参数运行,会提示你输入 paper_id。

step2(claim 语义完整性分析)的最后一步:综合 step2b(完整表述)和
step2d(完整表述_不含instance)的产出,给每一条claim算出唯一一个
"step2处理完毕后的文本"——已经完成语义补全、且已经剥离了instance重复
内容的claim,写进一份新文件,下游(包括之后的step3)不用再回头去
claim_completeness_analysis.json里判断该用哪个字段。这是step2这一段
流程自己的产出,不是整个项目最终会用到的那份数据——所以文件没有叫
"final",避免跟后面step3还会继续加工这件事产生混淆。

============================== 每条claim最终文本怎么选 ==============================
按优先级取第一个非null的:
  1. "完整表述_不含instance"(step2d的产出)——只要这个字段不是null,不管
     它当初是"确认混入,已剥离"还是"复核后判定不是混入,原样保留",都直接
     采用:前者是剥离后的新文本,后者本身就跟"完整表述"完全一样,两种
     情况这个字段都已经是正确的最终版本。
  2. "完整表述"(step2b的产出)——覆盖 instance_containment 不是 True 的
     claim(step2c判定它没有混入任何instance内容,或者所在conclusion压根
     没有instance;两种情况step2d都不会处理它,"完整表述_不含instance"
     是null)。
  3. "text"(最原始的字段)——覆盖"是纯实验数据"的claim,这类claim
     step2b本来就不需要给它写"完整表述"(该字段是null),它自己的原始text
     已经被判定为自足、不需要改写。

这个优先级链条跟 step2c/step2d 里 complete_text_of() 的"完整表述 or text"
是同一个思路,只是这里多了一层"完整表述_不含instance"排在最前面。

============================== 处理逻辑 ==============================
只需要读 claim_completeness_analysis.json 这一份文件——它已经带着
step2a/b/c/d全部字段,不需要再读 organized_content.json。文件里的记录顺序
本来就是按conclusion在原文中出现的顺序、以及claim在各conclusion内的原有
顺序排好的(step2a最初写入时定下的顺序,后续step2b/c/d都只在原有记录上
追加字段、不重新排序或增删条目),这里直接保留这个顺序,不重新计算。

对每条claim记录:
  1. 按上面的优先级算出最终文本 final_text。
  2. instance_removed = (instance_containment_confirmed is True),记录
     这条claim的最终文本是不是真的经过了instance剥离,方便下游不用回头
     翻claim_completeness_analysis.json全部字段就知道这一条有没有被
     动过、动过没有。
  3. is_pure_data:原样保留这个信息(step2a的判断结果,claim_completeness
     _analysis.json里叫"是纯实验数据"),输出键名用英文——虽然这一步已经
     把"该用完整表述还是text"这个选择做完了,但"这条claim本来是不是纯
     数据"这件事本身是下游可能还需要用到的信息(比如做统计、或者对
     纯数据类claim采用不同的下游处理方式),不应该在综合的时候被悄悄
     丢掉。
  4. supports / illustrates:论据(label=="论据")要带上它support哪些claim
     编号,example(label=="example")要带上它illustrate哪些claim编号——
     这两个字段claim_completeness_analysis.json自己不带(step2a当初只
     把number/label/text/是纯实验数据这些字段搬了过去,没有把这两个跨
     引用字段一起搬过来),这一步额外读一次organized_content.json把它们
     找回来,按(conclusion, number)对应补上。assertion没有这两个字段
     (它是独立成立的claim,不指向谁),输出里就不带这个键,不补null占位。
  5. needs_more_context:原样保留step2b的"需要更多上下文"字段(list[str],
     step2b没能从上下文/引用解决的术语列表),键名转成英文——是纯数据的
     claim没跑过step2b、这个字段本来是None,这里跟"跑过了但没有问题"的
     []统一处理,都当成[],不特意区分"没检查过"和"检查了但没发现问题"。
  6. 输出精简后的记录:{"conclusion", "number", "label",
     "supports"或"illustrates"(仅论据/example有), "text", "is_pure_data",
     "needs_more_context", "instance_removed"},不带其余中间过程字段
     (pure_data_status/是完整断言/completeness_status/completion_status/
     instance_containment/instance_containment_status/完整表述/完整表述_
     不含instance/instance_containment_confirmed/instance_removal_status)
     ——这些都是step2a-d各自处理过程中的中间字段,这一步产出的是给下游
     直接消费的结果,不需要带着整条处理链的全部痕迹。

============================== 输出格式 ==============================
需要同时读 organized_content.json(取supports/illustrates指向信息)和
claim_completeness_analysis.json(取其余全部字段)。输出写到
data/<paper_id>/step2_output_claims.json,一个列表,每条claim一条,比如:
    {
      "conclusion": "conclusion_4",
      "number": 7,
      "label": "example",
      "illustrates": [1],
      "text": "Compared to SNIP (62.23%, 67.51%, 69.35%) and GraSP (62.85%, 67.24%, 69.23%), the Elastic Ticket Transformation (ETT)-transferred tickets for ResNet architectures described in \u30101\u3011 are much closer to Iterative Magnitude-based Pruning (IMP)-found tickets.",
      "is_pure_data": false,
      "needs_more_context": [],
      "instance_removed": false
    }
或者assertion(没有supports/illustrates键):
    {
      "conclusion": "conclusion_4",
      "number": 20,
      "label": "assertion",
      "text": "The combined module of Average of Pruning (AoP) is applied without modifying existing post-hoc OOD scoring methods.",
      "is_pure_data": false,
      "instance_removed": true
    }

路径解析基于本文件自身位置,预期跟 step2a/step2b/step2c/step2d 同放在
step2_claim_completeness/ 目录下。
"""

import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"


def load_pointer_fields(organized_content_path: Path) -> dict:
    """{(conclusion短id, number): {"supports": [...]}} 或
    {(conclusion短id, number): {"illustrates": [...]}} 或
    {(conclusion短id, number): {}}(assertion,没有指向信息)。
    claim_completeness_analysis.json自己不带supports/illustrates这两个
    跨引用字段(step2a当初只把number/label/content/是纯实验数据这些搬了
    过去),这里额外读一次organized_content.json把它们找回来。"""
    conclusions = json.loads(organized_content_path.read_text(encoding="utf-8"))
    result = {}
    for c in conclusions:
        conc_short = c["id"].split("::")[-1]
        for p in c["organized_parts"]:
            key = (conc_short, p["number"])
            if p["label"] == "论据" and "supports" in p:
                result[key] = {"supports": p["supports"]}
            elif p["label"] == "example" and "illustrates" in p:
                result[key] = {"illustrates": p["illustrates"]}
            else:
                result[key] = {}
    return result


def finalize_claim(r: dict, pointer_fields: dict) -> dict:
    """算出这条claim在step2处理完毕后的文本+是否经过了instance剥离,
    返回精简后的输出记录。文本优先级见模块文档"每条claim最终文本怎么选"
    一节。pointer_fields是load_pointer_fields()的返回值,用来给论据/
    example补上supports/illustrates。输出字段名统一用英文
    (is_pure_data/needs_more_context/instance_removed),不混用中文——
    读取上游数据时用的还是claim_completeness_analysis.json里实际的
    字段名"是纯实验数据"/"需要更多上下文"(这是step2a/2b定的schema,不是
    这一步能改的),只是写进这一步自己的输出时统一转成英文键名。
    needs_more_context取自step2b的"需要更多上下文"字段(list[str],列出
    这条claim里step2b没能从上下文解决的术语/引用);is_pure_data的claim
    没跑过step2b、这个字段本来就是None,跟"跑过了但没有问题"的[]统一
    当成[]处理,不特意区分"没检查过"和"检查了但没问题"这两种情况。"""
    final_text = r.get("完整表述_不含instance") or r.get("完整表述") or r["text"]

    out = {
        "conclusion": r["conclusion"],
        "number": r["number"],
        "label": r["label"],
    }
    out.update(pointer_fields.get((r["conclusion"], r["number"]), {}))
    out["text"] = final_text
    out["is_pure_data"] = r.get("是纯实验数据", False)
    out["needs_more_context"] = r.get("需要更多上下文") or []
    out["instance_removed"] = r.get("instance_containment_confirmed") is True
    return out


def main():
    if len(sys.argv) > 1:
        paper_id = sys.argv[1]
    else:
        paper_id = input("请输入 paper_id: ").strip()

    paper_dir = DATA_DIR / paper_id
    organized_path = paper_dir / "organized_content.json"
    claim_analysis_path = paper_dir / "claim_completeness_analysis.json"

    if not organized_path.exists():
        print(f"找不到文件: {organized_path}(需要先跑完 step1b_organize_labeled_content.py)")
        sys.exit(1)
    if not claim_analysis_path.exists():
        print(f"找不到文件: {claim_analysis_path}(需要先跑完 step2a/step2b/step2c/step2d)")
        sys.exit(1)

    records = json.loads(claim_analysis_path.read_text(encoding="utf-8"))

    missing_fields = [
        f for f in ("完整表述_不含instance", "instance_containment_confirmed")
        if not any(f in r for r in records)
    ]
    if missing_fields:
        print(f"claim_completeness_analysis.json 里还没有 {missing_fields} 字段"
              "(需要先跑完 step2d_remove_instance_content.py)")
        sys.exit(1)

    pointer_fields = load_pointer_fields(organized_path)
    output_claims = [finalize_claim(r, pointer_fields) for r in records]

    out_path = paper_dir / "step2_output_claims.json"
    out_path.write_text(json.dumps(output_claims, ensure_ascii=False, indent=2), encoding="utf-8")

    n_removed = sum(1 for c in output_claims if c["instance_removed"])
    n_pure = sum(1 for c in output_claims if c["is_pure_data"])
    print(
        f"共 {len(output_claims)} 条claim,其中 {n_removed} 条剥离了instance内容、"
        f"{n_pure} 条是纯实验数据,已写入 {out_path}"
    )


if __name__ == "__main__":
    main()
