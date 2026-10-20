"""
test_run_multi_paper_step1a_batch.py

不连真实网络,mock掉 _submit_batch / _poll_batch_status /
_fetch_batch_results 这三个唯一碰SDK的函数,验证调度逻辑本身对不对,
尤其是这次新加的四种batch结果(succeeded校验不过/invalid_request/
server_error/expired/canceled)分别应该怎么处理。用真实的
validate_and_correct()校验(不mock这一层)。

用法:
    python test_run_multi_paper_step1a_batch.py
"""

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import run_multi_paper_step1a_batch as batch_pipeline


def _write_fake_graph(paper_dir: Path, conclusions: list) -> None:
    nodes = [
        {
            "id": f"paper:x::{c['id']}", "global_id": f"g_{c['id']}",
            "kind": "conclusion", "order": i, "title": c["id"], "content": c["content"],
        }
        for i, c in enumerate(conclusions)
    ]
    graph = {"data": {"papers": [{"graph": {"nodes": nodes}}]}}
    (paper_dir / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


def _succeeded(content: str) -> dict:
    """构造一个能通过validate_and_correct()校验的succeeded结果。"""
    text = json.dumps({"segments": [{"text": content, "label": "assertion", "number": 1}]})
    return {"kind": "succeeded", "text": text}


def _bad_format() -> dict:
    return {"kind": "succeeded", "text": "not valid json at all"}


def _invalid_request() -> dict:
    return {"kind": "invalid_request", "error_message": "max_tokens must be greater than thinking.budget_tokens"}


def _server_error() -> dict:
    return {"kind": "server_error", "error_message": "Overloaded"}


def _expired() -> dict:
    return {"kind": "expired"}


def _canceled() -> dict:
    return {"kind": "canceled"}


def _setup_single_conclusion_paper(tmp_dir: Path, paper_name: str, content: str) -> Path:
    paper_dir = tmp_dir / paper_name
    paper_dir.mkdir()
    _write_fake_graph(paper_dir, [{"id": "conclusion_1", "content": content}])
    return paper_dir


def test_connection_retry_succeeds_after_transient_failures():
    """模拟前2次都是网络连接错误、第3次成功,验证_with_connection_retry
    会重试、不会立刻把异常抛出去,而且真的拿到了最终的返回值。"""
    import anthropic

    call_count = [0]

    def flaky():
        call_count[0] += 1
        if call_count[0] < 3:
            raise anthropic.APIConnectionError("connection failed")
        return "finally ok"

    with patch("time.sleep"):  # 不真的等
        result = batch_pipeline._with_connection_retry(flaky, base_delay=1, max_delay=2)

    assert result == "finally ok"
    assert call_count[0] == 3


def test_connection_retry_never_gives_up_even_after_many_failures():
    """失败很多次(远超过之前旧版本设过的上限)之后终于成功,验证现在
    的版本不会有任何"重试次数用完就放弃"的上限——只要不是
    KeyboardInterrupt,就会一直重试下去。"""
    import anthropic

    call_count = [0]
    N_FAILURES = 50  # 远超旧版本的max_retries=5

    def flaky():
        call_count[0] += 1
        if call_count[0] <= N_FAILURES:
            raise anthropic.APIConnectionError("connection failed")
        return "eventually ok"

    with patch("time.sleep"):
        result = batch_pipeline._with_connection_retry(flaky, base_delay=1, max_delay=2)

    assert result == "eventually ok"
    assert call_count[0] == N_FAILURES + 1


def test_connection_retry_delay_doubles_then_caps():
    """验证重试间隔是从base_delay开始每次翻倍、封顶在max_delay,不会
    无限增长。"""
    import anthropic

    call_count = [0]
    sleep_calls = []

    def flaky():
        call_count[0] += 1
        if call_count[0] <= 5:
            raise anthropic.APIConnectionError("connection failed")
        return "ok"

    with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        batch_pipeline._with_connection_retry(flaky, base_delay=1, max_delay=8)

    # 1,2,4,8,8 —— 前几次翻倍,到了max_delay=8之后就不再增长
    assert sleep_calls == [1, 2, 4, 8, 8]


def test_connection_retry_keyboard_interrupt_propagates_immediately():
    """Ctrl+C(KeyboardInterrupt)不应该被这层重试吞掉,应该立刻往外传,
    不会被当成"网络错误"重试。"""
    def raises_keyboard_interrupt():
        raise KeyboardInterrupt()

    try:
        batch_pipeline._with_connection_retry(raises_keyboard_interrupt, base_delay=1)
        raised = False
    except KeyboardInterrupt:
        raised = True

    assert raised


def test_existing_batch_id_skips_resubmission():
    """给定existing_batch_ids时,不该重新提交batch,应该直接用给定的
    batch_id去查,后面校验/进下游全部照常走。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = _setup_single_conclusion_paper(tmp_dir, "paperResume", "Resumed text.")
        cid = batch_pipeline.make_custom_id("paperResume", "conclusion_1")

        submit_called = []

        def fake_submit(requests):
            submit_called.append(requests)
            return "should_not_be_used"

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "_submit_batch", fake_submit), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results",
                           lambda bid: [(cid, _succeeded("Resumed text."))] if bid == "existing_batch_123" else []), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", lambda *a, **k: True):
            batch_pipeline.run_multi_paper_pipeline(
                ["paperResume"], existing_batch_ids={"paperResume": "existing_batch_123"}
            )

        assert submit_called == []  # 一次都没重新提交
        out = json.loads((paper_dir / "conclusions_labeled.json").read_text(encoding="utf-8"))
        assert out[0]["labeling_status"] == "ok"
    finally:
        shutil.rmtree(tmp_dir)


def test_custom_id_roundtrip():
    cid = batch_pipeline.make_custom_id("867752822639165809", "conclusion_5")
    assert cid == "867752822639165809__conclusion_5"
    assert batch_pipeline.parse_custom_id(cid) == ("867752822639165809", "conclusion_5")


def test_succeeded_first_round_no_reminder_needed():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = _setup_single_conclusion_paper(tmp_dir, "paperA", "Some text.")
        cid = batch_pipeline.make_custom_id("paperA", "conclusion_1")

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "_submit_batch", lambda reqs: "b1"), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results", lambda bid: [(cid, _succeeded("Some text."))]), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", lambda *a, **k: True):
            batch_pipeline.run_multi_paper_pipeline(["paperA"])

        out = json.loads((paper_dir / "conclusions_labeled.json").read_text(encoding="utf-8"))
        assert out[0]["labeling_status"] == "ok"
    finally:
        shutil.rmtree(tmp_dir)


def test_invalid_request_reported_and_moves_to_next_tier_immediately():
    """invalid_request不应该在同一档重试(重试也是同样的参数,没意义),
    应该直接换下一档,而且换档时prompt要带RETRY_REMINDER。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = _setup_single_conclusion_paper(tmp_dir, "paperB", "Text.")
        cid = batch_pipeline.make_custom_id("paperB", "conclusion_1")

        submissions = []  # 记录每次submit_tier时的(tier_index, add_reminder)

        real_build = batch_pipeline.build_tier_requests

        def spy_build(pending, tier_index, add_reminder):
            submissions.append((tier_index, add_reminder))
            return real_build(pending, tier_index, add_reminder)

        call_count = [0]

        def fake_results(batch_id):
            call_count[0] += 1
            if call_count[0] == 1:
                return [(cid, _invalid_request())]
            return [(cid, _succeeded("Text."))]

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "build_tier_requests", spy_build), \
             patch.object(batch_pipeline, "_submit_batch", lambda reqs: f"b{call_count[0]}"), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results", fake_results), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", lambda *a, **k: True):
            batch_pipeline.run_multi_paper_pipeline(["paperB"])

        # 第1次是tier0/不带提醒(初次提交);第2次应该是tier1(换档了)/带提醒
        assert submissions == [(0, False), (1, True)]

        out = json.loads((paper_dir / "conclusions_labeled.json").read_text(encoding="utf-8"))
        assert out[0]["labeling_status"] == "ok_after_retry_opus5"
    finally:
        shutil.rmtree(tmp_dir)


def test_server_error_retried_at_same_tier_without_reminder():
    """服务端错误应该在同一档重试,不换档,重试时不带RETRY_REMINDER。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = _setup_single_conclusion_paper(tmp_dir, "paperC", "Text.")
        cid = batch_pipeline.make_custom_id("paperC", "conclusion_1")

        submissions = []
        real_build = batch_pipeline.build_tier_requests

        def spy_build(pending, tier_index, add_reminder):
            submissions.append((tier_index, add_reminder))
            return real_build(pending, tier_index, add_reminder)

        call_count = [0]

        def fake_results(batch_id):
            call_count[0] += 1
            if call_count[0] == 1:
                return [(cid, _server_error())]
            return [(cid, _succeeded("Text."))]

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "build_tier_requests", spy_build), \
             patch.object(batch_pipeline, "_submit_batch", lambda reqs: f"b{call_count[0]}"), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results", fake_results), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", lambda *a, **k: True):
            batch_pipeline.run_multi_paper_pipeline(["paperC"])

        # 两次提交都还是tier0(没换档),而且都不带提醒
        assert submissions == [(0, False), (0, False)]

        out = json.loads((paper_dir / "conclusions_labeled.json").read_text(encoding="utf-8"))
        assert out[0]["labeling_status"] == "ok"  # 因为最终是在tier0成功的,status是tier0对应的"ok"
    finally:
        shutil.rmtree(tmp_dir)


def test_expired_treated_same_as_server_error():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = _setup_single_conclusion_paper(tmp_dir, "paperD", "Text.")
        cid = batch_pipeline.make_custom_id("paperD", "conclusion_1")

        call_count = [0]

        def fake_results(batch_id):
            call_count[0] += 1
            if call_count[0] == 1:
                return [(cid, _expired())]
            return [(cid, _succeeded("Text."))]

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "_submit_batch", lambda reqs: f"b{call_count[0]}"), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results", fake_results), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", lambda *a, **k: True):
            batch_pipeline.run_multi_paper_pipeline(["paperD"])

        assert call_count[0] == 2  # 同档重试了一次就成功,不是直接换档
        out = json.loads((paper_dir / "conclusions_labeled.json").read_text(encoding="utf-8"))
        assert out[0]["labeling_status"] == "ok"
    finally:
        shutil.rmtree(tmp_dir)


def test_server_error_exhausts_same_tier_retries_then_moves_to_next_tier():
    """服务端错误持续发生、超过MAX_SAME_TIER_RETRIES次,应该并入下一档,
    不会在同一档无限重试下去。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = _setup_single_conclusion_paper(tmp_dir, "paperE", "Text.")
        cid = batch_pipeline.make_custom_id("paperE", "conclusion_1")

        submissions = []
        real_build = batch_pipeline.build_tier_requests

        def spy_build(pending, tier_index, add_reminder):
            submissions.append((tier_index, add_reminder))
            return real_build(pending, tier_index, add_reminder)

        call_count = [0]

        def fake_results(batch_id):
            call_count[0] += 1
            if call_count[0] <= 1 + batch_pipeline.MAX_SAME_TIER_RETRIES:
                return [(cid, _server_error())]  # 一直服务端错误
            return [(cid, _succeeded("Text."))]  # 换档之后终于成功

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "build_tier_requests", spy_build), \
             patch.object(batch_pipeline, "_submit_batch", lambda reqs: f"b{call_count[0]}"), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results", fake_results), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", lambda *a, **k: True):
            batch_pipeline.run_multi_paper_pipeline(["paperE"])

        # 前 1+MAX_SAME_TIER_RETRIES 次都停在tier0,重试次数耗尽后第
        # 1+MAX_SAME_TIER_RETRIES+1 次应该已经换到tier1
        tier0_count = sum(1 for t, _ in submissions if t == 0)
        assert tier0_count == 1 + batch_pipeline.MAX_SAME_TIER_RETRIES
        assert submissions[-1][0] == 1  # 最后一次提交是tier1

        out = json.loads((paper_dir / "conclusions_labeled.json").read_text(encoding="utf-8"))
        assert out[0]["labeling_status"] == "ok_after_retry_opus5"
    finally:
        shutil.rmtree(tmp_dir)


def test_canceled_halts_paper_without_fallback_or_downstream():
    """canceled不应该重试、不应该换档、不应该本地兜底,这篇论文也不该
    进下游队列。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = _setup_single_conclusion_paper(tmp_dir, "paperF", "Text.")
        cid = batch_pipeline.make_custom_id("paperF", "conclusion_1")

        downstream_called = []

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "_submit_batch", lambda reqs: "b1"), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results", lambda bid: [(cid, _canceled())]), \
             patch.object(batch_pipeline.sync_pipeline, "run_step",
                           lambda *a, **k: downstream_called.append(1) or True):
            batch_pipeline.run_multi_paper_pipeline(["paperF"])

        assert not downstream_called  # 下游13步一次都不该被调用
        assert not (paper_dir / "conclusions_labeled.json").exists()  # 不该写出文件
    finally:
        shutil.rmtree(tmp_dir)


def test_canceled_keeps_already_resolved_items_in_same_round():
    """同一批里,一条succeeded通过、另一条canceled——已经解决的那条结果
    应该被保留在state里(即使这篇论文最终halted、不会写文件/不会进
    下游),不会因为另一条canceled就被丢弃。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = tmp_dir / "paperG"
        paper_dir.mkdir()
        _write_fake_graph(paper_dir, [
            {"id": "conclusion_1", "content": "First."},
            {"id": "conclusion_2", "content": "Second."},
        ])
        cid1 = batch_pipeline.make_custom_id("paperG", "conclusion_1")
        cid2 = batch_pipeline.make_custom_id("paperG", "conclusion_2")

        def fake_results(batch_id):
            return [(cid1, _succeeded("First.")), (cid2, _canceled())]

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "_submit_batch", lambda reqs: "b1"), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results", fake_results), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", lambda *a, **k: True):

            state = batch_pipeline.init_paper_state("paperG")
            batch_pipeline.submit_tier(state, add_reminder=False)
            batch_pipeline.advance_paper(state)

            assert state["status"] == "halted"
            assert "conclusion_1" in state["resolved"]  # 已解决的保留了
            assert "conclusion_2" not in state["resolved"]
    finally:
        shutil.rmtree(tmp_dir)


def test_all_three_tiers_fail_falls_back_locally():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = _setup_single_conclusion_paper(tmp_dir, "paperH", "Never passes.")
        cid = batch_pipeline.make_custom_id("paperH", "conclusion_1")

        n_submits = [0]

        def fake_submit(reqs):
            n_submits[0] += 1
            return f"batch_{n_submits[0]}"

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "_submit_batch", fake_submit), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results", lambda bid: [(cid, _bad_format())]), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", lambda *a, **k: True):
            batch_pipeline.run_multi_paper_pipeline(["paperH"])

        assert n_submits[0] == 3  # 三档都提交过,没有第4次(兜底不调API)
        out = json.loads((paper_dir / "conclusions_labeled.json").read_text(encoding="utf-8"))
        assert out[0]["labeling_status"] == "fallback_trivial"
    finally:
        shutil.rmtree(tmp_dir)


def test_papers_advance_independently_faster_one_goes_downstream_first():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        for name in ("paperFast", "paperSlow"):
            _setup_single_conclusion_paper(tmp_dir, name, f"{name} content.")

        status_calls = {"paperFast": 0, "paperSlow": 0}
        downstream_order = []

        def fake_submit(requests):
            (custom_id, _kwargs), = requests
            paper_id, _ = batch_pipeline.parse_custom_id(custom_id)
            return f"batch_{paper_id}_{status_calls[paper_id]}"

        def fake_status(batch_id):
            paper_id = batch_id.split("_")[1]
            status_calls[paper_id] += 1
            if paper_id == "paperSlow" and status_calls[paper_id] == 1:
                return "in_progress"
            return "ended"

        def fake_results(batch_id):
            paper_id = batch_id.split("_")[1]
            cid = batch_pipeline.make_custom_id(paper_id, "conclusion_1")
            return [(cid, _succeeded(f"{paper_id} content."))]

        def fake_run_step(script_path, output_path, paper_id):
            downstream_order.append(paper_id)
            output_path.write_text("{}")
            return True

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "_submit_batch", fake_submit), \
             patch.object(batch_pipeline, "_poll_batch_status", fake_status), \
             patch.object(batch_pipeline, "_fetch_batch_results", fake_results), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", fake_run_step):
            batch_pipeline.run_multi_paper_pipeline(["paperFast", "paperSlow"])

        assert downstream_order[0] == "paperFast"
        assert "paperSlow" in downstream_order
    finally:
        shutil.rmtree(tmp_dir)


def test_missing_graph_json_is_skipped_without_blocking_others():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        ok_dir = _setup_single_conclusion_paper(tmp_dir, "paperOK", "fine.")
        (tmp_dir / "paperMissing").mkdir()

        cid = batch_pipeline.make_custom_id("paperOK", "conclusion_1")

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "_submit_batch", lambda reqs: "b1"), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results", lambda bid: [(cid, _succeeded("fine."))]), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", lambda *a, **k: True):
            batch_pipeline.run_multi_paper_pipeline(["paperOK", "paperMissing"])

        assert (ok_dir / "conclusions_labeled.json").exists()
        assert not (tmp_dir / "paperMissing" / "conclusions_labeled.json").exists()
    finally:
        shutil.rmtree(tmp_dir)


def test_downstream_failure_marked_failed_not_crash():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        _setup_single_conclusion_paper(tmp_dir, "paperFail", "x.")
        cid = batch_pipeline.make_custom_id("paperFail", "conclusion_1")

        with patch.object(batch_pipeline, "DATA_DIR", tmp_dir), \
             patch.object(batch_pipeline, "_submit_batch", lambda reqs: "b1"), \
             patch.object(batch_pipeline, "_poll_batch_status", lambda bid: "ended"), \
             patch.object(batch_pipeline, "_fetch_batch_results", lambda bid: [(cid, _succeeded("x."))]), \
             patch.object(batch_pipeline.sync_pipeline, "run_step", lambda *a, **k: False):
            batch_pipeline.run_multi_paper_pipeline(["paperFail"])  # 不应该抛异常
    finally:
        shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
