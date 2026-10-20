"""
build_single_text_graph.py

把一段文本包装成只有1个conclusion节点的graph.json,写到
data/<paper_id>/graph.json——paper_id用当前时间戳生成。这样就能直接
喂给现成的 run_full_pipeline.py / run_multi_paper_step1a_batch.py,
不需要对这14步流水线本身做任何修改:这两个脚本从头到尾都只认
"paper_id目录下有没有graph.json",不关心这背后是不是一篇真实论文、
有几个conclusion。

这个文件只提供 build_graph_for_text() 这一个函数,被
run_single_text_pipeline_sync.py 和 run_single_text_pipeline_batch.py
两边共用,避免同样的"怎么拼graph.json"这件事写两遍。

============================== paper_id怎么生成 ==============================
格式: text_YYYYMMDD_HHMMSS_ffffff(ffffff是微秒,6位)——时间统一用北京
时间(UTC+8),不管这个脚本运行在哪个时区的机器上,paper_id里看到的
时间都是一致的、不受运行机器本地时区设置影响(具体做法是先取UTC
绝对时刻,再转换成北京时间显示,全程本地计算,不需要联网)。精确到
微秒,连续两次调用基本不会撞车,前缀"text_"方便你在data/目录里跟
别的真实论文文件夹区分开。

============================== graph.json的字段怎么填 ==============================
只有1个节点,字段对齐step1a_pipeline.py.extract_conclusions()期望的
形状:
  - kind: 必须是"conclusion"这个字符串,extract_conclusions靠这个字段
    筛出conclusion节点
  - id: "paper:{paper_id}::conclusion_1"——沿用整个项目里
    "xxx::conclusion_N"这个既有约定(下游全部代码都是用
    c["id"].split("::")[-1]取"conclusion_N"这个短id的),不这样写的话,
    后面14步里任何一步都会因为解析不出短id而出错
  - global_id: 跟id用同一个值——这个字段在下游代码里只是被原样带着走、
    没有任何计算逻辑依赖它的具体内容,不需要额外设计
  - order: 0(只有1个节点,顺序无所谓,但extract_conclusions会拿这个
    字段排序,写清楚比依赖.get()的默认值更明确)
  - title: 留空——同样只是被原样带着走的元数据,不影响任何处理逻辑
  - content: 就是传进来的这段文本本身
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 北京时间是固定的UTC+8,不实行夏令时,所以直接用固定偏移量表示就行,
# 不需要依赖系统自带的时区数据库(zoneinfo)。


def build_graph_for_text(text: str, data_dir: Path) -> str:
    """把text包装成只有1个conclusion的graph.json,写到
    data_dir/<paper_id>/graph.json。返回生成的paper_id。

    paper_id里的时间戳:先取当前的UTC绝对时刻(不受运行这台机器自己
    系统时区设置的影响),再转换成北京时间(UTC+8)显示——这样不管这个
    脚本部署在哪个时区的服务器上,paper_id里看到的时间都统一是北京
    时间,不会因为机器所在时区不同而不一致。整个过程都是本地计算,
    不需要联网:UTC和本地时间是同一个系统时钟的两种读法,不是分开
    获取的两份数据。

    如果同名目录已经存在(理论上只有同一微秒内被调用两次才会发生,
    正常使用基本不可能撞上),会抛FileExistsError,不会静默覆盖已有
    数据。"""
    now_utc = datetime.now(timezone.utc)
    now_beijing = now_utc.astimezone(timezone(timedelta(hours=8)))
    paper_id = "text_" + now_beijing.strftime("%Y%m%d_%H%M%S_%f")
    paper_dir = data_dir / paper_id
    paper_dir.mkdir(parents=True, exist_ok=False)

    node = {
        "id": f"paper:{paper_id}::conclusion_1",
        "global_id": f"paper:{paper_id}::conclusion_1",
        "kind": "conclusion",
        "order": 0,
        "title": "",
        "content": text,
    }
    graph = {"data": {"papers": [{"graph": {"nodes": [node]}}]}}

    (paper_dir / "graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return paper_id
