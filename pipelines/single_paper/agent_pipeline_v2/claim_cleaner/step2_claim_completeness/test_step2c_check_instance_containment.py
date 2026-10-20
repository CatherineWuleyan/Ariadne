"""Self-contained extraction regression tests."""

import json

import step2c_check_instance_containment as step2c






def test_retry_chain_and_fallback_default_is_true():
    calls = []

    def flaky_mock(prompt, **kwargs):
        calls.append(kwargs.get("model"))
        if len(calls) == 1:
            return "hmm no idea"  # 既没有t也没有f,判不出来
        return "false"

    step2c.call_claude = flaky_mock
    instances = [{"number": 1, "label": "instance", "content": "(X, Y)", "of_terms": ["methods"]}]

    contains, status = step2c.check_claim_against_instances(
        "conclusion content mentioning methods (X, Y).", "a claim without those items.", instances,
    )
    assert status == "ok_after_retry_sonnet5_nothink"
    assert contains is False
    assert calls == [step2c.MODEL, step2c.RETRY_MODEL]

    def always_bad(prompt, **kwargs):
        return "unparseable garbage"

    step2c.call_claude = always_bad
    contains, status = step2c.check_claim_against_instances(
        "content", "claim text", instances,
    )
    assert status == "fallback_trivial"
    assert contains is True  # 兜底方向:选True,不是False


def test_all_skip_paper_still_writes_file():
    """回归测试:之前write_text()被两个continue语句挡在了外面,如果一篇
    论文里所有claim都走跳过分支(纯数据/无instance),没有一条会真正调
    API,导致write_text()一次都不会被调用——文件在磁盘上完全没变,但
    main()末尾照样打印"已写入"(867760083600146646实际踩到的就是这个
    坑)。这里用一份"全部跳过"的临时数据验证main()跑完之后,文件确实被
    更新、带上了instance_containment字段。"""
    import tempfile
    import shutil
    from pathlib import Path
    from unittest.mock import patch

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = tmp_dir / "fake_paper"
        paper_dir.mkdir()

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
             "是纯实验数据": True, "完整表述": None},
        ]
        claim_path = paper_dir / "claim_completeness_analysis.json"
        claim_path.write_text(json.dumps(claims), encoding="utf-8")

        def boom(*a, **k):
            raise AssertionError("这条claim是纯数据,不该调用call_claude")

        step2c.call_claude = boom

        with patch.object(step2c, "DATA_DIR", tmp_dir), patch("sys.argv", ["prog", "fake_paper"]):
            step2c.main()

        result = json.loads(claim_path.read_text(encoding="utf-8"))
        assert result[0]["instance_containment"] is None
        assert result[0]["instance_containment_status"] == "skipped_pure_data"
    finally:
        shutil.rmtree(tmp_dir)
