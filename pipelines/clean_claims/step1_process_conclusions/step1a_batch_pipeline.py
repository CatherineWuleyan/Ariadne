"""
step1a_batch_pipeline.py

用 Anthropic Batch API(而不是 step1a_pipeline.py 里逐条同步调用)批量跑
多篇论文的 step1a 打标签流程。三级重试的判断逻辑、每一级用的
model/thinking/max_tokens、什么时候放弃退到trivial兜底,都跟
step1a_pipeline.py 完全一样(直接复用它的 RETRY_REMINDER/extract_conclusions/
build_prompt/trivial_fallback),只是把"一条一条发请求、等一条一条的结果"
换成了"一批一批发请求、等一批一批的结果"——Batch API 所有用量打五折,但
不支持流式,处理是异步的(官方文档:大多数一小时内完成,最多24小时)。

具体做法是把三级重试拆成三"轮":第1轮把所有 conclusion 一起提交一个batch
(用第一档参数);等这一轮结果都回来后,校验通过的直接定稿,没通过校验的
(且不是被跳过/报错、且还有下一档可试)进入第2轮,提交一个只包含这些失败项
的新batch(用第二档参数);第2轮同理产出第3轮;第3轮结束后还没通过的一律
trivial兜底。

支持中断后恢复:每提交一批就把 batch_id 存进本地状态文件(STATE_PATH),
重新运行这个脚本会先看状态文件里有没有正在进行中的batch,有的话直接接着
轮询这一批,不会重新提交、不会重复扣钱。全部处理完成后状态文件会被删掉。

用法:
    python step1a_batch_pipeline.py <paper_id1> <paper_id2> ...
或不带参数运行,会用 DEFAULT_PAPER_IDS 这个默认列表。

跑完之后,每篇论文各自写一份 conclusions_labeled.json,格式跟
step1a_pipeline.py 单篇同步处理写出来的完全一样,后续 step1b 不用做任何
区分、可以直接接着跑。

路径解析基于本脚本自身所在位置,paper_graph2ir 整个文件夹挪到哪里都能跑。
"""

import sys
import json
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

for _p in (PROJECT_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from anthropic.types.message_create_params import MessageCreateParamsNonStreaming  # noqa: E402
from anthropic.types.messages.batch_create_params import Request  # noqa: E402

from claude_api_call import _normalize_request, _get_client  # noqa: E402
from label_validator import validate_and_correct  # noqa: E402
from step1a_pipeline import (  # noqa: E402
    extract_conclusions,
    build_prompt,
    trivial_fallback,
    RETRY_REMINDER,
    DATA_DIR,
)

DEFAULT_PAPER_IDS = [
    "1032903864883347458",
    "867750889056633390",
    "867752822639165809",
    "867760083600146646",
]

STATE_PATH = SCRIPT_DIR / "batch_state.json"
POLL_INTERVAL_SECONDS = 60

# 跟 step1a_pipeline.py 的 process_conclusion() 里完全一样的三级参数,
# 外加对应成功时该写的 labeling_status 名字、以及这一档要不要在prompt末尾
# 加格式提醒(第一档不加,第二、三档加,对应 step1a_pipeline.py 里的用法)。
RETRY_TIERS = [
    {
        "model": "claude-sonnet-5",
        "thinking": {"type": "adaptive", "effort": "xhigh"},
        "max_tokens": 50000,
        "status_name": "ok",
        "use_reminder": False,
    },
    {
        "model": "claude-opus-5",
        "thinking": {"type": "disabled"},
        "max_tokens": 10000,
        "status_name": "ok_after_retry_opus5",
        "use_reminder": True,
    },
    {
        "model": "claude-sonnet-5",
        "thinking": {"type": "disabled"},
        "max_tokens": 50000,
        "status_name": "ok_after_retry_sonnet5_nothink",
        "use_reminder": True,
    },
]


def _make_key(paper_id: str, idx: int) -> str:
    return f"{paper_id}_{idx}"


def _parse_key(key: str):
    paper_id, idx = key.rsplit("_", 1)
    return paper_id, int(idx)


def _load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_conclusions_index(paper_ids: list) -> dict:
    """返回 {key: conclusion_dict}——每次运行都从 graph.json 现算,不
    持久化(这一步很便宜、而且是确定性的,没必要存进状态文件)。"""
    index = {}
    for paper_id in paper_ids:
        graph_path = DATA_DIR / paper_id / "graph.json"
        if not graph_path.exists():
            print(f"找不到文件: {graph_path}")
            sys.exit(1)
        conclusions = extract_conclusions(graph_path)
        for idx, c in enumerate(conclusions):
            index[_make_key(paper_id, idx)] = c
    return index


def _extract_text_from_message(message) -> str:
    text_blocks = [block.text for block in message.content if block.type == "text"]
    return "".join(text_blocks)


def _submit_batch(keys: list, conclusions_index: dict, tier: dict) -> str:
    """给keys这些条目按tier的参数提交一个新batch,返回batch_id。"""
    requests = []
    for key in keys:
        content = conclusions_index[key]["content"]
        prompt = build_prompt(content, RETRY_REMINDER if tier["use_reminder"] else "")
        kwargs, _meta = _normalize_request(
            prompt, tier["model"], tier["max_tokens"], tier["thinking"], None, {}
        )
        requests.append(Request(custom_id=key, params=MessageCreateParamsNonStreaming(**kwargs)))
    batch = _get_client().messages.batches.create(requests=requests)
    return batch.id


def _wait_for_batch(batch_id: str) -> None:
    """轮询直到这一批处理结束。期间可以随时Ctrl+C中断——状态文件里已经
    存了batch_id,重新运行脚本会直接从这里继续轮询,不会重新提交。"""
    while True:
        batch = _get_client().messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"  batch {batch_id} 状态: {batch.processing_status}  "
            f"(处理中{counts.processing} 成功{counts.succeeded} 出错{counts.errored} "
            f"取消{counts.canceled} 过期{counts.expired})"
        )
        if batch.processing_status == "ended":
            return
        time.sleep(POLL_INTERVAL_SECONDS)


def _collect_batch_results(batch_id: str) -> dict:
    """返回 {custom_id: raw_answer_or_None}。errored/canceled/expired 统一
    当成"这次被跳过了"处理,跟同步调用里 call_claude() 返回 None 的语义
    一致——不会在同一档重试,直接看要不要进入下一档或者兜底。"""
    results = {}
    for item in _get_client().messages.batches.results(batch_id):
        if item.result.type == "succeeded":
            results[item.custom_id] = _extract_text_from_message(item.result.message)
        else:
            results[item.custom_id] = None
    return results


def run(paper_ids: list) -> None:
    conclusions_index = _build_conclusions_index(paper_ids)
    print(f"共 {len(conclusions_index)} 条 conclusion,来自 {len(paper_ids)} 篇论文")

    state = _load_state()
    if state is None:
        state = {
            "round": 0,
            "batch_id": None,
            "pending_keys": sorted(conclusions_index.keys()),
            "finished": {},
        }
        _save_state(state)
    else:
        print(
            f"发现已有进度: 第{state['round'] + 1}轮, batch_id={state['batch_id']}, "
            f"待处理{len(state['pending_keys'])}条, 已完成{len(state['finished'])}条"
        )

    while state["pending_keys"] and state["round"] < len(RETRY_TIERS):
        tier = RETRY_TIERS[state["round"]]

        if state["batch_id"] is None:
            print(
                f"\n=== 第{state['round'] + 1}轮: 提交 {len(state['pending_keys'])} 条 "
                f"(model={tier['model']}, max_tokens={tier['max_tokens']}) ==="
            )
            batch_id = _submit_batch(state["pending_keys"], conclusions_index, tier)
            state["batch_id"] = batch_id
            _save_state(state)
            print(f"  已提交, batch_id={batch_id}")
        else:
            print(f"\n=== 第{state['round'] + 1}轮: 已有进行中的batch {state['batch_id']}, 继续等待 ===")

        _wait_for_batch(state["batch_id"])
        raw_answers = _collect_batch_results(state["batch_id"])

        next_pending = []
        has_next_tier = state["round"] + 1 < len(RETRY_TIERS)
        for key in state["pending_keys"]:
            raw_answer = raw_answers.get(key)
            content = conclusions_index[key]["content"]
            parsed = validate_and_correct(raw_answer, content)

            if parsed is not None:
                state["finished"][key] = {"status": tier["status_name"], "labeled_segments": parsed}
            elif raw_answer is not None and has_next_tier:
                next_pending.append(key)
            else:
                state["finished"][key] = {
                    "status": "fallback_trivial",
                    "labeled_segments": trivial_fallback(content),
                }

        print(
            f"  第{state['round'] + 1}轮结束: 本轮{len(state['pending_keys'])}条中,"
            f"{len(state['pending_keys']) - len(next_pending)}条已定稿,"
            f"{len(next_pending)}条进入下一档"
        )

        state["round"] += 1
        state["batch_id"] = None
        state["pending_keys"] = next_pending
        _save_state(state)

    if state["pending_keys"]:
        # 理论上不会走到这里(has_next_tier=False时上面已经兜底了),留着
        # 只是为了防御性兜底,避免任何遗漏的条目永远卡在pending里
        print(f"\n还剩 {len(state['pending_keys'])} 条走兜底")
        for key in state["pending_keys"]:
            content = conclusions_index[key]["content"]
            state["finished"][key] = {
                "status": "fallback_trivial",
                "labeled_segments": trivial_fallback(content),
            }
        state["pending_keys"] = []
        _save_state(state)

    print("\n全部完成,正在写出每篇论文的 conclusions_labeled.json ...")
    for paper_id in paper_ids:
        conclusions = extract_conclusions(DATA_DIR / paper_id / "graph.json")
        results = []
        for idx, c in enumerate(conclusions):
            finished = state["finished"][_make_key(paper_id, idx)]
            result = dict(c)
            result["labeled_segments"] = finished["labeled_segments"]
            result["labeling_status"] = finished["status"]
            results.append(result)
        out_path = DATA_DIR / paper_id / "conclusions_labeled.json"
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        n_ok = sum(1 for r in results if r["labeling_status"] != "fallback_trivial")
        print(f"  {paper_id}: {len(results)}条,其中{n_ok}条正常通过,写入 {out_path}")

    if STATE_PATH.exists():
        STATE_PATH.unlink()
    print("\n全部处理完成。")


def main():
    paper_ids = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_PAPER_IDS
    run(paper_ids)


if __name__ == "__main__":
    main()
