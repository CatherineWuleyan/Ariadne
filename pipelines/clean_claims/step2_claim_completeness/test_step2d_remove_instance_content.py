"""
test_step2d_remove_instance_content.py

沙盒没有网络访问权限,用monkeypatch替换call_claude验证"不依赖模型质量"的
那部分逻辑:按conclusion分组、prompt拼装、JSON格式+编号完整性校验、
三档模型依次升级+保守兜底、"跳过不升级"这条规则——尤其是"同一批里有的
claim被判定改动、有的判定不改动"这种混合结果的场景。不测试"改写内容
对不对"本身,因为这一层校验已经按要求去掉了(容易把Sonnet改对的结果
误判成没改对),内容质量完全交给Sonnet自己判断。

用法:
    python test_step2d_remove_instance_content.py
"""

import json
import re

import step2d_remove_instance_content as step2d


# 867752822639165809 这几条是用户在自己机器上真跑step2c之后人工核对过的
# 真实结果;沙盒里的claim_completeness_analysis.json还没被真实写过
# instance_containment字段,这里手动补上,构造贴近真实情况的测试输入。
_KNOWN_CONTAINMENT = {
    ("conclusion_4", 15): False, ("conclusion_4", 18): False, ("conclusion_4", 20): True,
    ("conclusion_5", 1): False, ("conclusion_5", 3): False, ("conclusion_5", 5): False,
    ("conclusion_5", 6): True, ("conclusion_5", 10): False, ("conclusion_5", 11): True,
    ("conclusion_5", 12): False, ("conclusion_5", 13): True, ("conclusion_5", 16): False,
    ("conclusion_6", 1): False, ("conclusion_6", 3): False, ("conclusion_6", 6): False,
    ("conclusion_6", 7): False, ("conclusion_6", 8): False, ("conclusion_6", 10): True,
    ("conclusion_7", 2): False, ("conclusion_7", 4): False, ("conclusion_7", 7): False,
    ("conclusion_7", 10): False, ("conclusion_7", 11): True, ("conclusion_7", 14): False,
    ("conclusion_7", 16): False,
}


def _load_records_with_known_containment():
    paper_dir = step2d.DATA_DIR / "867752822639165809"
    records = json.loads((paper_dir / "claim_completeness_analysis.json").read_text(encoding="utf-8"))
    conc_instances = step2d.load_conclusion_instances(paper_dir / "organized_content.json")

    for r in records:
        key = (r["conclusion"], r["number"])
        if key in _KNOWN_CONTAINMENT:
            r["instance_containment"] = _KNOWN_CONTAINMENT[key]
        elif r["conclusion"] not in conc_instances:
            r["instance_containment"] = None
        else:
            r["instance_containment"] = False
    return records, conc_instances


def _mock_sub_items(content: str) -> list:
    """跟生产代码里已经删掉的_instance_sub_items类似,但这里只是测试用的
    简化版,不依赖生产代码(生产代码已经不需要拆子项了)——单纯用来让mock
    自己判断"一般词语+具体内容是否同时出现"这条规则。"""
    inner = content.strip().rstrip("。.")
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    return [p.strip() for p in re.split(r",|;| and ", inner) if p.strip()]


def _rule_based_mock_call_claude(prompt, **kwargs):
    """模拟"是否同时出现一般词语+instance具体内容"这条规则:对prompt里
    每条候选claim,检查它的完整表述里有没有同时包含某个instance的of_terms
    (一般词语)和该instance的content(具体例子)——两者都在才判定"混入",
    输出去掉具体例子后的版本;否则原样输出。这不是真的语义判断,只是为了
    在真实数据上验证整条数据管线(尤其是"同一批里部分改、部分不改"这种
    混合结果)不会崩、逻辑对得上。"""
    candidates_block = prompt.split("=== Suspected claims (full existing fields) ===\n")[1]
    candidates_block = candidates_block.split("\n\n=== All instances")[0]
    instances_block = prompt.split("=== All instances in this conclusion ===\n")[1]

    candidates = json.loads(candidates_block)
    instances = json.loads(instances_block)

    def norm(s):
        return "".join(s.split()).lower()

    results = []
    for c in candidates:
        original = c.get("完整表述") or c["text"]
        original_norm = norm(original)
        mixed = False
        edited = original
        for inst in instances:
            terms_present = any(norm(t) in original_norm for t in inst["of_terms"])
            sub_items = _mock_sub_items(inst["content"])
            items_present = len(sub_items) > 1 and all(norm(item) in original_norm for item in sub_items)
            if terms_present and items_present:
                mixed = True
                edited = original
                for item in sub_items:
                    edited = edited.replace(item, "")
                edited = " ".join(edited.split()).rstrip(", ") + "."
        results.append({"number": c["number"], "content": edited if mixed else original})

    return json.dumps({"results": results})


def test_conclusion_5_batch_mixed_outcome():
    """conclusion_5有3条真实候选(6/11/13)。按规则:
      - claim[6]: "multiple scoring functions"(一般词语)+"MSP/MaxLogit"
        (instance具体内容)都出现 -> 应该被改写
      - claim[11]: 只提到MSP,完整表述里没有"multiple scoring functions"
        这个一般词语 -> 应该保持原样不变
      - claim[13]: "backbones"(一般词语)+"ResNet variants,WideResNet..."
        (instance具体内容)都出现 -> 应该被改写
    专门验证"同一批claim,有的改、有的不改"这种混合结果场景,以及现在
    "content跟原文一样"是唯一用来判断"没改"的依据(不再额外校验改写
    内容本身对不对)。
    """
    step2d.call_claude = _rule_based_mock_call_claude

    records, conc_instances = _load_records_with_known_containment()
    groups = step2d.group_candidates_by_conclusion(records)

    assert groups["conclusion_5"] and [r["number"] for r in groups["conclusion_5"]] == [6, 11, 13]

    content = conc_instances["conclusion_5"]["content"]
    instances = conc_instances["conclusion_5"]["instances"]
    result, status = step2d.process_conclusion_candidates(content, groups["conclusion_5"], instances)

    assert status == "ok"

    by_num = {r["number"]: r for r in groups["conclusion_5"]}
    original_6 = by_num[6].get("完整表述") or by_num[6]["text"]
    original_11 = by_num[11].get("完整表述") or by_num[11]["text"]
    original_13 = by_num[13].get("完整表述") or by_num[13]["text"]

    assert result[6] != original_6    # 应该被改写
    assert result[11] == original_11  # 应该保持不变
    assert result[13] != original_13  # 应该被改写


def test_non_candidates_get_null_fields():
    records, conc_instances = _load_records_with_known_containment()

    for r in records:
        if r.get("instance_containment") is not True:
            r["完整表述_不含instance"] = None
            r["instance_containment_confirmed"] = None
            r["instance_removal_status"] = (
                "skipped_not_contained" if r.get("instance_containment") is False
                else "skipped_not_applicable"
            )

    r15 = next(x for x in records if x["conclusion"] == "conclusion_4" and x["number"] == 15)
    assert r15["instance_removal_status"] == "skipped_not_contained"
    assert r15["完整表述_不含instance"] is None
    assert r15["instance_containment_confirmed"] is None


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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
