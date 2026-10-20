"""Self-contained extraction regression tests."""

import json
import re

import step2d_remove_instance_content as step2d


# 867752822639165809 这几条是用户在自己机器上真跑step2c之后人工核对过的
# 真实结果;沙盒里的claim_completeness_analysis.json还没被真实写过
# instance_containment字段,这里手动补上,构造贴近真实情况的测试输入。


def test_json_parse_rejects_missing_or_extra_numbers():
    # 少答一个
    missing = json.dumps({"results": [{"number": 1, "content": "claim one text."}]})
    assert step2d._parse_and_validate(missing, {1, 2}) is None

    # 多答一个不存在的编号
    extra = json.dumps({"results": [
        {"number": 1, "content": "claim one text."},
        {"number": 2, "content": "claim two text."},
        {"number": 3, "content": "extra."},
    ]})
    assert step2d._parse_and_validate(extra, {1, 2}) is None

    # 重复编号
    duplicate = json.dumps({"results": [
        {"number": 1, "content": "a."},
        {"number": 1, "content": "b."},
    ]})
    assert step2d._parse_and_validate(duplicate, {1}) is None


def test_content_quality_is_no_longer_checked():
    """这一步的重点:哪怕改写结果显然没有把instance内容删干净(比如只删了
    一部分),只要JSON格式对、编号对得上,就应该直接放行,不再额外校验
    "删得对不对"——这是本次改动明确要的效果。"""
    incomplete_removal = json.dumps({"results": [
        {"number": 1, "content": "uses methods such as MSP for evaluation."}
        # 原本可能有ODIN/Energy等其它instance项没删,但现在不检查这个
    ]})
    result = step2d._parse_and_validate(incomplete_removal, {1})
    assert result == {1: "uses methods such as MSP for evaluation."}

    # 改写后反而变长很多,以前会被长度检查拦下来,现在也应该直接放行
    much_longer = json.dumps({"results": [
        {"number": 1, "content": "a" * 500}
    ]})
    result = step2d._parse_and_validate(much_longer, {1})
    assert result == {1: "a" * 500}


def test_markdown_fence_is_tolerated():
    fenced = "```json\n" + json.dumps({"results": [{"number": 1, "content": "original text."}]}) + "\n```"
    result = step2d._parse_and_validate(fenced, {1})
    assert result == {1: "original text."}


def test_invalid_field_types_rejected():
    # number不是int
    bad_number = json.dumps({"results": [{"number": "1", "content": "x"}]})
    assert step2d._parse_and_validate(bad_number, {1}) is None

    # number是bool(True在Python里是int的子类,专门排除)
    bad_bool = json.dumps({"results": [{"number": True, "content": "x"}]})
    assert step2d._parse_and_validate(bad_bool, {1}) is None

    # content是空字符串
    empty_content = json.dumps({"results": [{"number": 1, "content": "   "}]})
    assert step2d._parse_and_validate(empty_content, {1}) is None


def test_three_tier_escalation_then_fallback():
    """三档模型都拿到回复但都不通过格式校验 -> 应该依次用
    Sonnet5(高思考)->Opus5(关闭思考)->Sonnet5(关闭思考)各调一次,
    最终保守兜底,全部保持原文不动。"""
    calls = []

    def always_bad(prompt, **kwargs):
        calls.append((kwargs.get("model"), kwargs.get("thinking")))
        return "not json at all"

    step2d.call_claude = always_bad

    instances = [{"number": 1, "label": "instance", "content": "(A, B)", "of_terms": ["t"]}]
    candidates = [{"number": 1, "conclusion": "c", "label": "assertion", "text": "x", "完整表述": "x"}]

    result, status = step2d.process_conclusion_candidates("content", candidates, instances)

    assert status == "fallback_unchanged"
    assert result == {1: "x"}
    assert len(calls) == 3
    assert calls[0] == (step2d.MODEL, step2d.THINKING)
    assert calls[1] == (step2d.RETRY_MODEL_1, step2d.RETRY_THINKING_1)
    assert calls[2] == (step2d.RETRY_MODEL_2, step2d.RETRY_THINKING_2)


def test_second_tier_success_stops_escalation():
    """第一档格式不对,第二档(Opus5)就成功了 -> 不应该再调用第三档。"""
    calls = []

    def flaky_mock(prompt, **kwargs):
        calls.append(kwargs.get("model"))
        if len(calls) == 1:
            return "not json"
        return json.dumps({"results": [{"number": 1, "content": "x"}]})

    step2d.call_claude = flaky_mock

    instances = [{"number": 1, "label": "instance", "content": "(A, B)", "of_terms": ["t"]}]
    candidates = [{"number": 1, "conclusion": "c", "label": "assertion", "text": "x", "完整表述": "x"}]

    result, status = step2d.process_conclusion_candidates("content", candidates, instances)

    assert status == "ok_after_opus5_nothink"
    assert result == {1: "x"}
    assert len(calls) == 2  # 只调了第一档+第二档,没到第三档
    assert calls == [step2d.MODEL, step2d.RETRY_MODEL_1]


def test_skipped_call_does_not_escalate():
    """如果调用本身被跳过(raw_answer是None,不是格式问题),不应该继续
    往下一档升级,直接保守兜底。"""
    calls = []

    def skipped_mock(prompt, **kwargs):
        calls.append(kwargs.get("model"))
        return None

    step2d.call_claude = skipped_mock

    instances = [{"number": 1, "label": "instance", "content": "(A, B)", "of_terms": ["t"]}]
    candidates = [{"number": 1, "conclusion": "c", "label": "assertion", "text": "x", "完整表述": "x"}]

    result, status = step2d.process_conclusion_candidates("content", candidates, instances)

    assert status == "fallback_unchanged"
    assert result == {1: "x"}
    assert len(calls) == 1  # 第一档被跳过就直接兜底,不升级


def test_all_skip_paper_still_writes_file():
    """回归测试:之前write_text()被continue语句挡在了外面,如果一篇论文
    里所有claim都是跳过分支(纯数据/无instance),没有任何一条会走到
    真正调API那一支,导致write_text()一次都不会被调用——文件在磁盘上
    完全没变,但main()末尾照样打印"已写入"。这里用一份"全部跳过"的
    临时数据验证main()跑完之后,文件确实被更新、带上了instance_containment
    字段(不是只改了内存里的records,却没有落盘)。"""
    import tempfile
    import shutil
    from pathlib import Path
    from unittest.mock import patch

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = tmp_dir / "fake_paper"
        paper_dir.mkdir()

        # 构造一个"全部跳过"的场景:1条纯数据claim,1条所在conclusion没有
        # instance的claim,合起来跟867760083600146646实际遇到的情况一样。
        organized = [{
            "id": "paper:fake::conclusion_1",
            "content": "some conclusion content with no instance in it.",
            "organized_parts": [
                {"number": 1, "label": "assertion", "content": "x", "source_spans": [{"start": 0, "end": 1}]},
            ],
        }]
        (paper_dir / "organized_content.json").write_text(json.dumps(organized), encoding="utf-8")

        claims = [
            {"conclusion": "conclusion_1", "number": 1, "label": "assertion", "text": "x",
             "是纯实验数据": True, "完整表述": None,
             "instance_containment": None, "instance_containment_status": "skipped_pure_data"},
        ]
        claim_path = paper_dir / "claim_completeness_analysis.json"
        claim_path.write_text(json.dumps(claims), encoding="utf-8")

        with patch.object(step2d, "DATA_DIR", tmp_dir), patch("sys.argv", ["prog", "fake_paper"]):
            step2d.main()

        result = json.loads(claim_path.read_text(encoding="utf-8"))
        assert result[0]["instance_containment_confirmed"] is None
        assert result[0]["instance_removal_status"] == "skipped_not_applicable"
        assert "完整表述_不含instance" in result[0]  # 字段确实落盘了,不是只留在内存里
    finally:
        shutil.rmtree(tmp_dir)
