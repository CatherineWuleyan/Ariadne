"""
run_multi_paper_step1a_batch.py

用法:
    python run_multi_paper_step1a_batch.py <paper_id1> <paper_id2> ...
    python run_multi_paper_step1a_batch.py --file paper_ids.txt

批量跑多篇论文的完整流水线,但只有step1a(标签打标)这一步走Batch API,
其余13步(step1b~step4d)完全不变,还是调用现有各脚本、一次处理一篇。

============================== 整体设计 ==============================
每篇论文单独一批(不是把多篇论文的conclusion混进同一个batch)——一篇论文
里全部conclusion打包成一个batch提交。这样"一篇论文的3档重试全部走完"
这件事跟其它论文完全独立,不需要互相等待,也不需要额外的线程/队列去
实现"谁先好谁先进下游"——直接在这个单线程的主循环里,每一轮都:
  1. 挨个检查目前还在"打标签"阶段的论文,有没有batch跑完了,跑完了就
     按结果分类推进(见下面"batch结果的四种情况怎么分开处理")
  2. 如果"待下游"队列里有论文,取一篇,把它剩下的13步(step1b~step4d)
     跑完(阻塞,一步步跑,任何一步失败就把这篇论文标记失败、不再往下跑,
     但不影响其它论文)——不同论文的下游处理不会并行,只有当前这篇跑完了
     (或者失败停止了),才会去处理下一篇
  3. 如果两边都没什么可做,睡一会再回到第1步

这个循环本身是单线程的、没有真正的并发,靠"反复轮询+见缝插针处理下游"
实现"多篇论文的batch在各自独立推进、谁先好谁先下游"的效果。

============================== 三档配置,跟同步版step1a_pipeline.py完全一致 ==============================
直接复用同步版里的常量和函数(build_prompt/trivial_fallback/
RETRY_REMINDER/extract_conclusions),保证produce的conclusions_labeled
.json内容、格式、字段跟同步版产出的完全一样:
  第1档: claude-sonnet-5, thinking={"type":"adaptive","effort":"xhigh"}, max_tokens=50000, 通过时status="ok"
  第2档: claude-opus-5,   thinking={"type":"disabled"},                 max_tokens=10000, 通过时status="ok_after_retry_opus5"
  第3档: claude-sonnet-5, thinking={"type":"disabled"},                 max_tokens=50000, 通过时status="ok_after_retry_sonnet5_nothink"
三档都没过 -> trivial_fallback(),status="fallback_trivial"

============================== batch结果的四种情况怎么分开处理 ==============================
Batch API里一条request跑完不是只有"成功/失败"两种,取回结果之后按
result.type细分成四种,处理方式各不一样:

  - succeeded,但校验没过(跟同步版一样,拿validate_and_correct()校验)
    -> 排到下一档
  - errored 且 result.error.type=="invalid_request_error"(请求参数
    本身有问题,比如某档的max_tokens/thinking组合被这个模型拒绝)
    -> 打印出来报告一下,直接排到下一档(同一档换的还是同样的参数,
    重试没有意义,换一档模型/思考深度才可能绕开这个问题)
  - errored 但不是invalid_request_error(api_error/overloaded_error/
    rate_limit_error/timeout_error等,基础设施/服务端层面的问题,
    不是这次请求本身有错)-> 在同一档原样自动重试(最多
    MAX_SAME_TIER_RETRIES次,重试的时候不加RETRY_REMINDER,因为
    这条提醒是针对"格式不对"的,跟服务端故障没关系;超过重试次数
    还没成功,才并入下一档)
  - expired(24小时硬顶,这条没轮到)-> 处理方式跟"服务端错误"一样,
    同一档自动重试(最多MAX_SAME_TIER_RETRIES次)
  - canceled(有人手动调用了batches.cancel(),这个脚本自己从来不会
    主动取消batch,出现这个状态说明有外部干预)-> 这篇论文的处理
    到此为止:不再对剩下没解决的conclusion做任何重试/换档/本地兜底,
    这篇论文永远进不了下游队列,状态标记成"halted"。这一轮里已经
    真的validate通过的部分不会被丢弃,只是不会再往前推进。

MAX_SAME_TIER_RETRIES=2 是这个脚本自己定的默认值(用户只说了"服务端
错误/expired要在同一档自动重试",没说重试几次封顶——不限次数重试有
死循环的风险,所以选了一个不大的默认值,可以按需调整这个常量)。

============================== 每个batch请求怎么构造 ==============================
复用 claude_api_call._normalize_request() 把(prompt, model, max_tokens,
thinking)转换成能直接喂给SDK的kwargs,再包进
Request(custom_id=..., params=MessageCreateParamsNonStreaming(**kwargs))。
custom_id用 f"{paper_id}__{conclusion短id}" 拼接。是否在prompt末尾加
RETRY_REMINDER,由调用方显式传参决定(见build_tier_requests的
add_reminder参数),不是简单靠"是不是第一档"判断——同一档因服务端错误
重试时不加这条提醒,换到新一档时才加。

============================== 下游13步怎么跑 ==============================
直接import run_full_pipeline.py,复用它的 STEPS 列表(去掉第一项
step1a)和 run_step() 函数,不重新实现"退出码非0"/"退出码0但没生成
预期文件"这两种失败判断。

============================== 为了方便测试,批处理API的三个动作被拆成了独立函数 ==============================
_submit_batch(requests) / _poll_batch_status(batch_id) / _fetch_batch_results
(batch_id) 是这个脚本里唯一直接碰SDK batch接口的地方,其余调度逻辑都
只依赖这三个函数的返回值——测试时mock掉这三个函数,不需要真的连外网。

============================== 网络连接本身断开怎么办 ==============================
上面这三个函数内部,真正调用SDK的地方都套了一层_with_connection_retry:
专门针对"请求压根没能完整地跟服务器打一个来回"这种传输层故障(网络
抖动、SSL握手失败、DNS解析失败、电脑休眠导致断网几个小时等,表现为
anthropic.APIConnectionError)做退避重试——这跟_fetch_batch_results()
自己内部区分的invalid_request/server_error/canceled/expired是两个
完全不同的层面:那四种是"请求确实发到了服务器、服务器给了个明确的
result",这里说的是"请求压根没通,连"服务器怎么答复"这件事都没发生"。

这一层重试**不设次数上限**,只要是APIConnectionError就一直重试下去,
只有手动按Ctrl+C才会真正终止脚本——电脑休眠断网几个小时这种情况,
进程本身也会被系统一起冻结,恢复运行后重试会自然继续,不需要专门
处理"睡了多久"。重试间隔从5秒开始每次翻倍、封顶60秒,不会因为已经
失败很多次就要等越来越久才肯再试。

路径解析基于本文件自身位置,预期放在项目根目录下。
"""

import sys
import time
import json
import argparse
from pathlib import Path

import anthropic

PROJECT_ROOT = Path(__file__).resolve().parent
STEP1_DIR = PROJECT_ROOT / "step1_process_conclusions"
DATA_DIR = PROJECT_ROOT / "data"

for _p in (PROJECT_ROOT, STEP1_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from claude_api_call import _normalize_request  # noqa: E402  # 复用同一套参数归一化,不重新实现
from label_validator import validate_and_correct  # noqa: E402
from step1a_pipeline import (  # noqa: E402
    extract_conclusions, build_prompt, trivial_fallback, RETRY_REMINDER,
)
import run_full_pipeline as sync_pipeline  # noqa: E402  # 复用它的STEPS列表和run_step(),下游13步不重新写

# 三档配置:(model, thinking, max_tokens, 通过时的status标签),完全照抄
# 同步版step1a_pipeline.py里process_conclusion()的三档设定
TIERS = [
    ("claude-sonnet-5", {"type": "adaptive", "effort": "xhigh"}, 50000, "ok"),
    ("claude-opus-5", {"type": "disabled"}, 10000, "ok_after_retry_opus5"),
    ("claude-sonnet-5", {"type": "disabled"}, 50000, "ok_after_retry_sonnet5_nothink"),
]

POLL_INTERVAL_SECONDS = 60
CUSTOM_ID_SEP = "__"
MAX_SAME_TIER_RETRIES = 2  # 服务端错误/expired在同一档最多自动重试几次,见模块文档说明


# =============================================================================
# 唯一直接碰SDK batch接口的三个函数,测试的时候会被换成假的实现
# =============================================================================

def _with_connection_retry(func, *args, base_delay: float = 5.0, max_delay: float = 60.0, **kwargs):
    """对连接层面的瞬时错误(网络抖动、SSL握手失败、DNS解析失败、电脑
    休眠导致断网几个小时等)无限重试,不会自己放弃——这里只catch
    anthropic.APIConnectionError,KeyboardInterrupt(Ctrl+C)不在这个
    except范围内,会原样往外传,只有手动按Ctrl+C才能真正终止。

    重试间隔从base_delay开始,每次翻倍,封顶在max_delay(默认最长间隔
    60秒),不会随着重试次数增多而无限拉长间隔——网络一旦恢复,最多
    等一个max_delay的周期就会重新尝试,不会因为之前失败次数多就要
    等很久很久才肯再试一次。

    电脑真的进入系统休眠时,这个Python进程本身也会被操作系统一起
    冻结(time.sleep()也会跟着暂停,不会占用CPU空转计时),不需要
    专门处理"睡了几个小时"这件事——机器恢复运行的那一刻,这次
    time.sleep()自然结束,重试继续,网络一旦重新可用一般会立刻成功。"""
    attempt = 0
    delay = base_delay
    while True:
        try:
            return func(*args, **kwargs)
        except anthropic.APIConnectionError as e:
            attempt += 1
            print(f"  !! 网络连接错误({e}),{delay:.0f}秒后重试"
                  f"(已重试{attempt}次,不会自动放弃,按Ctrl+C可手动终止)")
            time.sleep(delay)
            delay = min(delay * 2, max_delay)


def _submit_batch(requests: list) -> str:
    """提交一批request,返回batch_id。"""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    import claude_api_call

    client = claude_api_call._get_client()
    sdk_requests = [
        Request(custom_id=custom_id, params=MessageCreateParamsNonStreaming(**kwargs))
        for custom_id, kwargs in requests
    ]
    batch = _with_connection_retry(client.messages.batches.create, requests=sdk_requests)
    return batch.id


def _poll_batch_status(batch_id: str) -> str:
    """返回这个batch当前的processing_status(比如"in_progress"/"ended")。"""
    import claude_api_call
    client = claude_api_call._get_client()
    batch = _with_connection_retry(client.messages.batches.retrieve, batch_id)
    return batch.processing_status


def _fetch_batch_results(batch_id: str) -> list:
    """返回这个batch的全部结果,每一项是 (custom_id, outcome)。outcome是:
      {"kind": "succeeded", "text": "..."}
      {"kind": "invalid_request", "error_message": "..."}   请求参数本身有问题
      {"kind": "server_error", "error_message": "..."}      其它errored(api_error/overloaded_error/rate_limit_error/timeout_error等)
      {"kind": "canceled"}
      {"kind": "expired"}
    """
    import claude_api_call
    client = claude_api_call._get_client()

    def _fetch_all():
        # results()返回的是一个流式迭代器,如果连接在迭代中途断开,
        # list()会在这里抛出异常,被_with_connection_retry整体捕获、
        # 整个重新拉取一遍(Anthropic那边的结果数据不会因为我们读到
        # 一半断开就丢失,重新拉取是安全的)。
        return list(client.messages.batches.results(batch_id))

    entries = _with_connection_retry(_fetch_all)

    output = []
    for entry in entries:
        result = entry.result
        if result.type == "succeeded":
            text_blocks = [b.text for b in result.message.content if b.type == "text"]
            outcome = {"kind": "succeeded", "text": "".join(text_blocks)}
        elif result.type == "errored":
            error_type = getattr(result.error, "type", None)
            if error_type == "invalid_request_error":
                outcome = {"kind": "invalid_request", "error_message": str(result.error)}
            else:
                outcome = {"kind": "server_error", "error_message": str(result.error)}
        elif result.type == "canceled":
            outcome = {"kind": "canceled"}
        elif result.type == "expired":
            outcome = {"kind": "expired"}
        else:
            # 理论上不该出现result.type未知的情况,防御性地当服务端错误处理(可重试),
            # 不静默丢弃
            outcome = {"kind": "server_error", "error_message": f"未知的result.type: {result.type}"}
        output.append((entry.custom_id, outcome))
    return output


# =============================================================================
# 调度逻辑:只依赖上面三个函数的返回值,不直接碰SDK
# =============================================================================

def make_custom_id(paper_id: str, conc_short: str) -> str:
    return f"{paper_id}{CUSTOM_ID_SEP}{conc_short}"


def parse_custom_id(custom_id: str) -> tuple:
    paper_id, conc_short = custom_id.split(CUSTOM_ID_SEP, 1)
    return paper_id, conc_short


def build_tier_requests(pending: dict, tier_index: int, add_reminder: bool) -> list:
    """pending是{conc_short: conclusion_obj},返回
    [(conc_short, kwargs), ...]。add_reminder由调用方显式决定要不要在
    prompt末尾加RETRY_REMINDER——换新一档时加(暗示上一档格式/参数有
    问题),同一档因服务端错误/expired重试时不加(跟格式无关,加了反而
    误导)。"""
    model, thinking, max_tokens, _ = TIERS[tier_index]
    extra_reminder = RETRY_REMINDER if add_reminder else ""

    requests = []
    for conc_short, conclusion_obj in pending.items():
        prompt = build_prompt(conclusion_obj["content"], extra_reminder)
        kwargs, _meta = _normalize_request(prompt, model, max_tokens, thinking, None, {})
        requests.append((conc_short, kwargs))
    return requests


def submit_tier(state: dict, add_reminder: bool) -> None:
    """给state["pending"]提交一个新batch(按state["tier_index"]的模型
    配置),更新state["batch_id"]。"""
    tier_requests = build_tier_requests(state["pending"], state["tier_index"], add_reminder)
    full_requests = [
        (make_custom_id(state["paper_id"], conc_short), kwargs)
        for conc_short, kwargs in tier_requests
    ]
    state["batch_id"] = _submit_batch(full_requests)


def init_paper_state(paper_id: str) -> dict:
    """返回这篇论文的初始状态字典,如果graph.json缺失,status直接是
    "skipped"。"""
    graph_path = DATA_DIR / paper_id / "graph.json"
    if not graph_path.exists():
        print(f"跳过 {paper_id}: 找不到 {graph_path}")
        return {"paper_id": paper_id, "status": "skipped"}

    conclusions = extract_conclusions(graph_path)
    pending = {c["id"].split("::")[-1]: c for c in conclusions}

    return {
        "paper_id": paper_id,
        "status": "labeling",
        "conclusions": conclusions,        # 保持顺序,最后写文件要按这个顺序
        "pending": pending,                # 当前这一档正在飞的(首次提交或同档重试)
        "pending_next_tier": {},           # 已经确定要换下一档、但还没提交的(等这一档同档重试收尾后再一起送)
        "same_tier_retries": 0,            # 当前这一档已经做过几次同档重试
        "resolved": {},                    # 已解决的:{conc_short: result_dict}
        "tier_index": 0,                   # 当前在飞的batch用第几档(0/1/2)
        "batch_id": None,                  # 当前在飞的batch id
    }


def categorize_batch_results(state: dict) -> dict:
    """取回并分类这一档batch的结果,不直接修改state,只返回分类结果:
    {"resolved": {conc_short: result_dict}, "next_tier": {conc_short: conclusion_obj},
     "same_tier_retry": {conc_short: conclusion_obj}, "halted": bool}"""
    raw_results = _fetch_batch_results(state["batch_id"])
    _, _, _, ok_status = TIERS[state["tier_index"]]

    resolved, next_tier, same_tier_retry = {}, {}, {}
    halted = False

    for custom_id, outcome in raw_results:
        _paper_id, conc_short = parse_custom_id(custom_id)
        conclusion_obj = state["pending"].get(conc_short)
        if conclusion_obj is None:
            continue  # 理论上不该发生,防御性跳过

        kind = outcome["kind"]

        if kind == "canceled":
            halted = True
            continue

        if kind == "succeeded":
            parsed = validate_and_correct(outcome["text"], conclusion_obj["content"])
            if parsed is not None:
                result_dict = dict(conclusion_obj)
                result_dict["labeled_segments"] = parsed
                result_dict["labeling_status"] = ok_status
                resolved[conc_short] = result_dict
            else:
                next_tier[conc_short] = conclusion_obj
            continue

        if kind == "invalid_request":
            print(f"  !! {state['paper_id']}/{conc_short}: invalid_request错误"
                  f"({outcome.get('error_message', '')}),换下一档")
            next_tier[conc_short] = conclusion_obj
            continue

        # server_error 或 expired -> 同一档重试候选
        same_tier_retry[conc_short] = conclusion_obj

    return {"resolved": resolved, "next_tier": next_tier, "same_tier_retry": same_tier_retry, "halted": halted}


def apply_final_fallback(state: dict) -> None:
    """三档都跑完,pending_next_tier里剩下的全部本地trivial_fallback,
    不再调API。"""
    for conc_short, conclusion_obj in state["pending_next_tier"].items():
        result_dict = dict(conclusion_obj)
        result_dict["labeled_segments"] = trivial_fallback(conclusion_obj["content"])
        result_dict["labeling_status"] = "fallback_trivial"
        state["resolved"][conc_short] = result_dict
    state["pending_next_tier"] = {}


def write_conclusions_labeled(state: dict) -> None:
    """按conclusions原有顺序把结果整理成列表,写成跟同步版完全一样格式
    的conclusions_labeled.json。"""
    out_path = DATA_DIR / state["paper_id"] / "conclusions_labeled.json"
    ordered_results = [
        state["resolved"][c["id"].split("::")[-1]] for c in state["conclusions"]
    ]
    out_path.write_text(
        json.dumps(ordered_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def advance_paper(state: dict) -> None:
    """这篇论文当前的batch已经ended,按四种结果分类推进它的状态。"""
    outcome = categorize_batch_results(state)
    state["batch_id"] = None

    if outcome["halted"]:
        # 有canceled结果 -> 这篇论文的处理到此为止,不再重试/换档/兜底。
        # 这一轮里已经真的validate通过的部分不丢弃,只是不再往前推进。
        state["resolved"].update(outcome["resolved"])
        state["status"] = "halted"
        return

    state["resolved"].update(outcome["resolved"])
    state["pending_next_tier"].update(outcome["next_tier"])

    same_tier_retry = outcome["same_tier_retry"]

    if same_tier_retry and state["same_tier_retries"] < MAX_SAME_TIER_RETRIES:
        state["same_tier_retries"] += 1
        state["pending"] = same_tier_retry
        submit_tier(state, add_reminder=False)
        return

    # 同档重试机会用完了(或者本来就没有同档重试候选)-> 这一档到此为止,
    # 还没解决的(含耗尽同档重试次数的)全部并入pending_next_tier
    state["pending_next_tier"].update(same_tier_retry)
    state["pending"] = {}
    state["same_tier_retries"] = 0

    if not state["pending_next_tier"]:
        write_conclusions_labeled(state)
        state["status"] = "ready_for_downstream"
        return

    if state["tier_index"] < len(TIERS) - 1:
        state["tier_index"] += 1
        state["pending"] = state["pending_next_tier"]
        state["pending_next_tier"] = {}
        submit_tier(state, add_reminder=True)
        return

    # 已经是最后一档还没解决的 -> 本地兜底
    apply_final_fallback(state)
    write_conclusions_labeled(state)
    state["status"] = "ready_for_downstream"


DOWNSTREAM_STEPS = sync_pipeline.STEPS[1:]  # 跳过第一项(step1a),后面13步


def run_downstream(paper_id: str) -> bool:
    """跑这篇论文剩下的13步(step1b~step4d),复用run_full_pipeline.py的
    run_step()逐步调用,任何一步失败就停止、返回False。"""
    paper_dir = DATA_DIR / paper_id
    for script_rel_path, expected_output, _needs_api in DOWNSTREAM_STEPS:
        script_path = PROJECT_ROOT / script_rel_path
        print(f"  [{paper_id}] {script_rel_path}")
        ok = sync_pipeline.run_step(script_path, paper_dir / expected_output, paper_id)
        if not ok:
            return False
    return True


def run_multi_paper_pipeline(paper_ids: list, existing_batch_ids: dict = None) -> None:
    """existing_batch_ids: {paper_id: batch_id},对于列在这里的论文,
    跳过"提交第1档batch"这一步,直接用给定的batch_id去查——用来恢复
    "脚本中途崩了,但对应的batch其实已经在Anthropic那边正常提交/处理"
    这种情况,避免重复提交、浪费已经花出去的钱。跳过提交之后,剩下的
    校验/升级/下游全部走跟正常流程完全一样的代码,不会因为是"接续"
    进来的就有任何不同的处理。"""
    existing_batch_ids = existing_batch_ids or {}
    papers = {pid: init_paper_state(pid) for pid in paper_ids}

    # 一开始就把全部"没被跳过"的论文各自的第1档batch都提交出去
    # (existing_batch_ids里指定的论文除外,直接复用给定的batch_id)
    for state in papers.values():
        if state["status"] != "labeling":
            continue
        if state["paper_id"] in existing_batch_ids:
            state["batch_id"] = existing_batch_ids[state["paper_id"]]
            print(f"{state['paper_id']}: 复用已有batch {state['batch_id']}(不重新提交)")
        else:
            submit_tier(state, add_reminder=False)
            print(f"{state['paper_id']}: 提交第1档batch {state['batch_id']}"
                  f"({len(state['pending'])}条conclusion)")

    downstream_queue = []

    def still_working():
        return any(s["status"] == "labeling" for s in papers.values()) or downstream_queue

    while still_working():
        made_progress = False

        # 1. 检查全部还在"打标签"阶段的论文
        for state in papers.values():
            if state["status"] != "labeling":
                continue
            batch_status = _poll_batch_status(state["batch_id"])
            if batch_status != "ended":
                continue

            made_progress = True
            print(f"{state['paper_id']}: 第{state['tier_index'] + 1}档batch跑完,校验结果...")
            advance_paper(state)

            if state["status"] == "ready_for_downstream":
                print(f"{state['paper_id']}: 全部conclusion已解决,进入下游队列")
                downstream_queue.append(state["paper_id"])
            elif state["status"] == "halted":
                print(f"{state['paper_id']}: 收到canceled结果,处理到此为止,不再重试/兜底,不会进入下游")
            else:
                n_left = len(state["pending"]) or len(state["pending_next_tier"])
                print(f"{state['paper_id']}: 还有{n_left}条没解决,继续第{state['tier_index'] + 1}档")

        # 2. 处理一篇下游(如果队列里有)
        if downstream_queue:
            made_progress = True
            paper_id = downstream_queue.pop(0)
            print(f"\n{'=' * 70}\n开始跑 {paper_id} 的下游13步\n{'=' * 70}")
            ok = run_downstream(paper_id)
            papers[paper_id]["status"] = "done" if ok else "failed"
            print(f"{paper_id}: 下游{'全部完成' if ok else '中途失败'}")

        # 3. 两边都没什么可做,睡一会
        if not made_progress:
            time.sleep(POLL_INTERVAL_SECONDS)

    n_done = sum(1 for s in papers.values() if s["status"] == "done")
    n_failed = sum(1 for s in papers.values() if s["status"] == "failed")
    n_skipped = sum(1 for s in papers.values() if s["status"] == "skipped")
    n_halted = sum(1 for s in papers.values() if s["status"] == "halted")
    print(
        f"\n全部完成: {n_done} 篇成功, {n_failed} 篇下游中途失败, "
        f"{n_skipped} 篇因缺graph.json被跳过, {n_halted} 篇因batch被取消而中止"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paper_ids", nargs="*", help="paper_id列表")
    parser.add_argument("--file", help="每行一个paper_id的文本文件")
    args = parser.parse_args()

    paper_ids = list(args.paper_ids)
    if args.file:
        paper_ids += [
            line.strip() for line in Path(args.file).read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]

    if not paper_ids:
        print("没有指定任何paper_id(可以直接传多个,或者用 --file 指定一个每行一个paper_id的文件)")
        sys.exit(1)

    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("没检测到 ANTHROPIC_API_KEY 环境变量,先设置好再跑。")
        sys.exit(1)

    run_multi_paper_pipeline(paper_ids)


if __name__ == "__main__":
    main()
