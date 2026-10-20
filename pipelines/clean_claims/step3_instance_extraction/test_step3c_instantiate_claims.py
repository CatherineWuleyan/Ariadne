"""
test_step3c_instantiate_claims.py

沙盒没有网络访问权限,用monkeypatch替换call_claude验证"不依赖模型质量"的
那部分逻辑:模板分节解析、prompt固定/可变内容交替结构、UNCERTAIN_CAVEAT
按batch条件性出现、三档模型依次升级+保守兜底、忽略空格的内容变化检测、
空结果时仍写同名文件。

用法:
    python test_step3c_instantiate_claims.py
"""

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import step3c_instantiate_claims as step3c


def test_content_changed_ignores_whitespace_only_diffs():
    assert step3c.content_changed("a  b   c", "a b c") is False
    assert step3c.content_changed(" a b c ", "a b c") is False
    assert step3c.content_changed("a b c", "a b d") is True
    assert step3c.content_changed("a b c", "a b c") is False


def test_instance_target_texts_dedupes_and_skips_not_found():
    inst = {
        "targets": [
            {"certainty": "certain", "content": "widget", "candidates": []},
            {"certainty": "uncertain", "content": None,
             "candidates": [{"content": "widget"}, {"content": "gadget"}]},
            {"certainty": "not_found", "content": None, "candidates": []},
        ]
    }
    result = step3c.instance_target_texts(inst)
    assert result == ["widget", "gadget"]  # 去重(certain的widget和uncertain候选里的widget只留一个),not_found跳过


def test_prompt_alternates_fixed_and_variable_no_uncertain():
    """没有uncertain_instances时,UNCERTAIN_CAVEAT不应该出现在prompt里,
    且固定/可变内容应该是交替的(不是全部固定文字堆在最前面)。"""
    claims_info = [{
        "number": 6, "text": "AoP also improves multiple scoring functions",
        "certain": [{"instance_number": 8, "targets": ["multiple scoring functions"],
                     "instance_content": "(MSP, MaxLogit)"}],
        "uncertain": [],
    }]
    prompt = step3c.build_prompt("some conclusion content here.", claims_info)

    assert "possibly exemplifying" not in prompt  # UNCERTAIN_CAVEAT没出现
    assert "Claim number 6:" in prompt
    assert "AoP also improves multiple scoring functions" in prompt

    # 交替结构:conclusion内容应该出现在"Here is the conclusion..."之后、
    # "Claim number 6:"之前
    conclusion_leadin_pos = prompt.index("Here is the conclusion")
    conclusion_content_pos = prompt.index("some conclusion content here.")
    claim_leadin_pos = prompt.index("Claim number 6:")
    claim_text_pos = prompt.index("AoP also improves multiple scoring functions")
    certain_leadin_pos = prompt.index("definitely exemplify")

    assert conclusion_leadin_pos < conclusion_content_pos < claim_leadin_pos < claim_text_pos < certain_leadin_pos


def test_prompt_includes_uncertain_caveat_when_any_claim_has_uncertain():
    claims_info = [
        {"number": 1, "text": "claim one.", "certain": [], "uncertain": [
            {"instance_number": 5, "targets": ["term"], "instance_content": "(A, B)"}
        ]},
        {"number": 2, "text": "claim two.", "certain": [
            {"instance_number": 6, "targets": ["term2"], "instance_content": "(C)"}
        ], "uncertain": []},
    ]
    prompt = step3c.build_prompt("content.", claims_info)
    assert "possibly exemplifying" in prompt
    # claim 1没有certain段,不应该出现它的CERTAIN_LEADIN;claim 2没有uncertain段
    assert prompt.count("definitely exemplify") == 1
    assert prompt.count("may exemplify") == 1


def test_three_tier_escalation_then_fallback():
    calls = []

    def always_bad(prompt, **kwargs):
        calls.append((kwargs.get("model"), kwargs.get("thinking")))
        return "not json"

    step3c.call_claude = always_bad

    claims_info = [{"number": 1, "text": "x.", "certain": [], "uncertain": []}]
    result, status = step3c.process_conclusion("content", claims_info)

    assert status == "fallback_unchanged"
    assert result == {1: "x."}
    assert len(calls) == 3
    assert calls[0] == (step3c.MODEL, step3c.THINKING)
    assert calls[1] == (step3c.RETRY_MODEL_1, step3c.RETRY_THINKING_1)
    assert calls[2] == (step3c.RETRY_MODEL_2, step3c.RETRY_THINKING_2)


def test_skipped_call_does_not_escalate():
    calls = []

    def skipped(prompt, **kwargs):
        calls.append(1)
        return None

    step3c.call_claude = skipped

    claims_info = [{"number": 1, "text": "x.", "certain": [], "uncertain": []}]
    result, status = step3c.process_conclusion("content", claims_info)

    assert status == "fallback_unchanged"
    assert result == {1: "x."}
    assert len(calls) == 1


def test_main_end_to_end_only_changed_claims_are_output():
    """跑一遍main(),用真实的867752822639165809/conclusion_5数据结构
    (organized_content.json/instance_target_positions.json都是真实拷贝,
    claim_instance_associations.json和step2_output_claims.json用贴合
    真实情况的数据构造),mock掉call_claude模拟"claim[6]真的被实例化改写,
    claim[13]被模型判定其实不用改"这种混合结果,验证只有claim[6]出现在
    special_claims.json里。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = tmp_dir / "fake_paper"
        paper_dir.mkdir()

        organized = [{
            "id": "paper:fake::conclusion_5",
            "content": "irrelevant for this test",
            "organized_parts": [
                {"number": 6, "label": "assertion", "content": "AoP improves multiple scoring functions",
                 "source_spans": [{"start": 0, "end": 10}]},
                {"number": 13, "label": "assertion", "content": "The improvements hold across backbones",
                 "source_spans": [{"start": 20, "end": 30}]},
            ],
        }]
        (paper_dir / "organized_content.json").write_text(json.dumps(organized), encoding="utf-8")

        instance_targets = [
            {
                "conclusion": "conclusion_5", "instance_number": 8,
                "instance_content": "(MSP, MaxLogit)", "instance_span": {"start": 0, "end": 1},
                "targets": [{"term": "multiple scoring functions", "certainty": "certain",
                             "content": "multiple scoring functions", "start": 5, "end": 6, "candidates": []}],
            },
            {
                "conclusion": "conclusion_5", "instance_number": 14,
                "instance_content": "(ResNet variants, WideResNet, VGG, MobileNet)",
                "instance_span": {"start": 0, "end": 1},
                "targets": [{"term": "backbones", "certainty": "certain",
                             "content": "backbones", "start": 25, "end": 26, "candidates": []}],
            },
        ]
        (paper_dir / "instance_target_positions.json").write_text(
            json.dumps(instance_targets), encoding="utf-8"
        )

        associations = [
            {"conclusion": "conclusion_5", "claim_number": 6, "claim_label": "assertion",
             "certain_instances": [8], "uncertain_instances": []},
            {"conclusion": "conclusion_5", "claim_number": 13, "claim_label": "assertion",
             "certain_instances": [14], "uncertain_instances": []},
        ]
        (paper_dir / "claim_instance_associations.json").write_text(
            json.dumps(associations), encoding="utf-8"
        )

        step2_output = [
            {"conclusion": "conclusion_5", "number": 6, "label": "assertion",
             "text": "AoP improves multiple scoring functions", "is_pure_data": False, "instance_removed": False},
            {"conclusion": "conclusion_5", "number": 13, "label": "assertion",
             "text": "The improvements hold across backbones", "is_pure_data": False, "instance_removed": False},
        ]
        (paper_dir / "step2_output_claims.json").write_text(
            json.dumps(step2_output), encoding="utf-8"
        )

        def mock_call_claude(prompt, **kwargs):
            # claim 6真的被实例化改写,claim 13模型判定不用动(原样返回)
            return json.dumps({"results": [
                {"number": 6, "content": "AoP improves Maximum Softmax Probability (MSP) and MaxLogit"},
                {"number": 13, "content": "The improvements hold across backbones"},
            ]})

        step3c.call_claude = mock_call_claude

        with patch.object(step3c, "DATA_DIR", tmp_dir), patch("sys.argv", ["prog", "fake_paper"]):
            step3c.main()

        out_path = paper_dir / "special_claims.json"
        assert out_path.exists()
        result = json.loads(out_path.read_text(encoding="utf-8"))

        assert len(result) == 1  # 只有claim 6真的变了
        assert result[0]["conclusion"] == "conclusion_5"
        assert result[0]["claim_number"] == 6
        assert result[0]["text"] == "AoP improves Maximum Softmax Probability (MSP) and MaxLogit"
        assert result[0]["certain_instances_given"] == [8]
        assert result[0]["uncertain_instances_given"] == []
    finally:
        shutil.rmtree(tmp_dir)


def test_main_writes_empty_list_when_nothing_changed():
    """全部claim都没有真的发生实例化改写(或者压根没有待处理claim)时,
    仍然要写special_claims.json,内容是空列表,不是干脆不产生这个文件。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        paper_dir = tmp_dir / "fake_paper"
        paper_dir.mkdir()

        organized = [{
            "id": "paper:fake::conclusion_1", "content": "irrelevant",
            "organized_parts": [
                {"number": 1, "label": "assertion", "content": "x", "source_spans": [{"start": 0, "end": 1}]},
            ],
        }]
        (paper_dir / "organized_content.json").write_text(json.dumps(organized), encoding="utf-8")
        (paper_dir / "instance_target_positions.json").write_text("[]", encoding="utf-8")
        (paper_dir / "claim_instance_associations.json").write_text(json.dumps([
            {"conclusion": "conclusion_1", "claim_number": 1, "claim_label": "assertion",
             "certain_instances": [], "uncertain_instances": []},
        ]), encoding="utf-8")
        (paper_dir / "step2_output_claims.json").write_text(json.dumps([
            {"conclusion": "conclusion_1", "number": 1, "label": "assertion",
             "text": "x", "is_pure_data": False, "instance_removed": False},
        ]), encoding="utf-8")

        def boom(*a, **k):
            raise AssertionError("没有待处理claim,不该调用call_claude")

        step3c.call_claude = boom

        with patch.object(step3c, "DATA_DIR", tmp_dir), patch("sys.argv", ["prog", "fake_paper"]):
            step3c.main()

        out_path = paper_dir / "special_claims.json"
        assert out_path.exists()
        assert json.loads(out_path.read_text(encoding="utf-8")) == []
    finally:
        shutil.rmtree(tmp_dir)


def test_real_data_prompt_structure_conclusion_5():
    """用真实的867752822639165809/conclusion_5数据(organized_content.json/
    instance_target_positions.json/claim_instance_associations.json都是
    真实拷贝)验证build_claims_info()+build_prompt()整条链路能正确处理
    真实数据结构,不报错、字段对得上。"""
    paper_dir = step3c.DATA_DIR / "867752822639165809"
    conclusion_contents = step3c.load_conclusion_contents(paper_dir)
    instances_by_key = step3c.load_instances_by_key(paper_dir)
    candidate_groups = step3c.load_candidate_associations(paper_dir)

    assert "conclusion_5" in candidate_groups
    assert {a["claim_number"] for a in candidate_groups["conclusion_5"]} == {6, 13}

    # 用organized_content.json自己的content模拟claim_texts(沙盒里没有真实
    # 跑过的step2_output_claims.json)
    org = json.loads((paper_dir / "organized_content.json").read_text(encoding="utf-8"))
    claim_texts = {}
    for c in org:
        conc_short = c["id"].split("::")[-1]
        for p in c["organized_parts"]:
            if p["label"] in ("assertion", "论据", "example"):
                claim_texts[(conc_short, p["number"])] = p["content"]

    claims_info = step3c.build_claims_info(
        "conclusion_5", candidate_groups["conclusion_5"], claim_texts, instances_by_key
    )
    prompt = step3c.build_prompt(conclusion_contents["conclusion_5"], claims_info)

    assert "multiple scoring functions" in prompt
    assert "MSP" in prompt
    assert "backbones" in prompt
    assert "ResNet variants" in prompt
    assert "possibly exemplifying" not in prompt  # 这两条claim都只有certain,没有uncertain


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
