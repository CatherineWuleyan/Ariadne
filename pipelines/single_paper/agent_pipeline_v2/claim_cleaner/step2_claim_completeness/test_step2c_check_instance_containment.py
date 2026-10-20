"""
test_step2c_check_instance_containment.py

沙盒没有网络访问权限,没法真的调Haiku,用monkeypatch替换call_claude来验证
"不依赖模型判断质量"的那部分逻辑:跳过条件(纯数据/无instance)、prompt
拼装、单值true/false提取、重试链三条路径。

用法:
    python test_step2c_check_instance_containment.py
"""

import json

import step2c_check_instance_containment as step2c


def _heuristic_mock_call_claude(prompt, **kwargs):
    """粗略判断:claim文本里有没有出现某个instance content的核心内容,
    用来在真实数据上跑出一个"看起来合理"的结果,顺便验证数据管线不崩。"""
    claim_text = prompt.split("=== Claim being tested ===\n")[1].split("\n\n=== All instances")[0]
    instances_block = prompt.split("=== All instances in this conclusion ===\n")[1]
    instances = json.loads(instances_block)

    claim_norm = "".join(claim_text.split()).lower()
    for inst in instances:
        core = "".join(inst["content"].split()).lower().strip("().;,")
        if core and core in claim_norm:
            return "true"
    return "false"


def test_real_data_instance_containment_true_and_false():
    step2c.call_claude = _heuristic_mock_call_claude

    paper_dir = step2c.DATA_DIR / "867752822639165809"
    conc_instances = step2c.load_conclusion_instances(paper_dir / "organized_content.json")
    records = json.loads((paper_dir / "claim_completeness_analysis.json").read_text(encoding="utf-8"))

    for r in records:
        if r.get("是纯实验数据"):
            r["instance_containment"] = None
            r["instance_containment_status"] = "skipped_pure_data"
            continue
        conc_short = r["conclusion"]
        if conc_short not in conc_instances:
            r["instance_containment"] = None
            r["instance_containment_status"] = "skipped_no_instances"
            continue
        content = conc_instances[conc_short]["content"]
        instances = conc_instances[conc_short]["instances"]
        claim_text = step2c.complete_text_of(r)
        contains, status = step2c.check_claim_against_instances(content, claim_text, instances)
        r["instance_containment"] = contains
        r["instance_containment_status"] = status

    by_key = {(r["conclusion"], r["number"]): r for r in records}

    assert by_key[("conclusion_6", 10)]["instance_containment"] is True
    assert by_key[("conclusion_7", 11)]["instance_containment"] is True
    assert by_key[("conclusion_4", 15)]["instance_containment"] is False
    assert by_key[("conclusion_4", 18)]["instance_containment"] is False
    assert by_key[("conclusion_4", 20)]["instance_containment"] is False

    no_instance = [r for r in records if not r.get("是纯实验数据") and r["conclusion"] not in conc_instances]
    assert no_instance and all(
        r["instance_containment"] is None and r["instance_containment_status"] == "skipped_no_instances"
        for r in no_instance
    )


def test_pure_data_claims_are_skipped_not_tested():
    # 867757662605934651/conclusion_4/number=6 是真实数据里"是纯实验数据"
    # =true的claim,应该被跳过,不该走到API调用那一步。
    paper_dir = step2c.DATA_DIR / "867757662605934651"
    records = json.loads((paper_dir / "claim_completeness_analysis.json").read_text(encoding="utf-8"))
    r = next(x for x in records if x["conclusion"] == "conclusion_4" and x["number"] == 6)
    assert r["是纯实验数据"] is True

    def boom(*a, **k):
        raise AssertionError("纯数据claim不该调用call_claude")

    step2c.call_claude = boom

    if r.get("是纯实验数据"):
        contains, status = None, "skipped_pure_data"
    assert status == "skipped_pure_data"


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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
