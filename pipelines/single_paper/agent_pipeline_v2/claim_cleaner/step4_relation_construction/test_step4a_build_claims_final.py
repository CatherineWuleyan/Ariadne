"""
test_step4a_build_claims_final.py

用两份真实数据(867757662605934651/conclusion_2 覆盖引用非claim编号、
relation-label、note去重合并;867750889056633390/conclusion_4 覆盖论据/
example关系展开)验证 step4a_build_claims_final.py 的全部逻辑。不需要
monkeypatch任何东西,这一步不调API,纯本地计算。

用法:
    python test_step4a_build_claims_final.py
"""

import json
import shutil
import tempfile
from pathlib import Path

import step4a_build_claims_final as step4a


def _load_real_conclusion(paper_id: str, conclusion_short: str) -> dict:
    path = step4a.DATA_DIR / paper_id / "organized_content.json"
    conclusions = json.loads(path.read_text(encoding="utf-8"))
    return next(c for c in conclusions if step4a._conclusion_short_id(c) == conclusion_short)


def _build_temp_paper(organized_conclusions: list, step2_output_records: list) -> Path:
    tmp_dir = Path(tempfile.mkdtemp())
    paper_dir = tmp_dir / "fake_paper"
    paper_dir.mkdir()
    (paper_dir / "organized_content.json").write_text(
        json.dumps(organized_conclusions, ensure_ascii=False), encoding="utf-8"
    )
    (paper_dir / "step2_output_claims.json").write_text(
        json.dumps(step2_output_records, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_dir, paper_dir


# ============================================================
# 真实数据 1: 867757662605934651/conclusion_2
#   [1][2][3][4][5][7] elaboration, [6][9][11] assertion,
#   [8] 论证(不是claim), [10] relation(connects=[6,9,11])
#   claim[6]/[9]/[11]的完整表述里各自引用了几个elaboration编号,
#   有重复引用(2/4/5被三条claim共同引用),用来测试note去重+合并。
# ============================================================

_REAL_CLAIM_6_TEXT = (
    "Under the iterative magnitude pruning (IMP) regime described in 【1】, "
    "the Trojan score (as defined in 【2】, 【4】, 【5】) is approximately zero "
    "for non-Trojan (clean) models."
)
_REAL_CLAIM_9_TEXT = (
    "A significantly positive peak in the Trojan score $\\mathcal{S}_{\\mathrm{Trojan}}$ "
    "(as defined in 【2】, 【4】, 【5】) along the pruning path indicates an atypical "
    "pruning stability fingerprint associated with a backdoor."
)
_REAL_CLAIM_11_TEXT = (
    "Computing the Trojan score $\\mathcal{S}_{\\mathrm{Trojan}}$ (as defined in 【2】, 【4】, 【5】) "
    "along the IMP pruning path described in 【1】 yields a data-free detector that "
    "identifies pruning levels where Trojan information is isolated."
)


def _setup_conclusion_2_case():
    conclusion = _load_real_conclusion("867757662605934651", "conclusion_2")
    step2_output = [
        {"conclusion": "conclusion_2", "number": 6, "label": "assertion",
         "text": _REAL_CLAIM_6_TEXT, "is_pure_data": False, "instance_removed": False},
        {"conclusion": "conclusion_2", "number": 9, "label": "assertion",
         "text": _REAL_CLAIM_9_TEXT, "is_pure_data": False, "instance_removed": False},
        {"conclusion": "conclusion_2", "number": 11, "label": "assertion",
         "text": _REAL_CLAIM_11_TEXT, "is_pure_data": False, "instance_removed": False},
    ]
    return _build_temp_paper([conclusion], step2_output)


def test_multi_number_citation_in_one_bracket_gets_fully_rewritten():
    """真实数据里踩到过的bug:step2b引用多个part时,把编号写进同一对
    括号里、用逗号分隔(比如【1, 4】),不是分开写成【1】【4】——这种格式
    step2b自己的prompt规则里是明文允许的,旧版正则只认得起【单个数字】
    这一种形式,导致【1, 4】这种引用完全匹配不上,原样残留在最终输出里
    没有被替换成"claim N"/"note N"。用867760083600146646真实出现过的
    场景复现:一条claim引用【1, 4】,其中1是claim、4是note。"""
    conclusion = {
        "id": "paper:x::conclusion_5",
        "content": "irrelevant",
        "organized_parts": [
            {"number": 1, "label": "assertion", "content": "a", "source_spans": [{"start": 0, "end": 1}]},
            {"number": 4, "label": "elaboration", "content": "some elaboration content",
             "of_terms": ["something"], "source_spans": [{"start": 1, "end": 2}]},
            {"number": 9, "label": "assertion", "content": "b", "source_spans": [{"start": 2, "end": 3}]},
        ],
    }
    step2_output = [
        {"conclusion": "conclusion_5", "number": 1, "label": "assertion",
         "text": "first claim.", "is_pure_data": False, "instance_removed": False},
        {"conclusion": "conclusion_5", "number": 9, "label": "assertion",
         "text": "second claim, as described in 【1, 4】, holds.",
         "is_pure_data": False, "instance_removed": False},
    ]
    tmp_dir, paper_dir = _build_temp_paper([conclusion], step2_output)
    try:
        result = step4a.build_claims_final(paper_dir)
        claim9 = next(c for c in result["claim"] if c["text"].startswith("second claim"))
        assert "【" not in claim9["text"] and "】" not in claim9["text"]
        # 1是claim(新编号1),4是note(elaboration带of_terms,新编号1)
        assert claim9["text"] == "second claim, as described in claim 1, note 1, holds."
    finally:
        shutil.rmtree(tmp_dir)


def test_claim_numbering_in_original_order():
    tmp_dir, paper_dir = _setup_conclusion_2_case()
    try:
        result = step4a.build_claims_final(paper_dir)
        numbers_and_orig_order = [(c["number"]) for c in result["claim"]]
        assert numbers_and_orig_order == [1, 2, 3]  # 6->1, 9->2, 11->3,按原编号升序
    finally:
        shutil.rmtree(tmp_dir)


def test_notes_deduped_and_numbered_by_first_appearance():
    tmp_dir, paper_dir = _setup_conclusion_2_case()
    try:
        result = step4a.build_claims_final(paper_dir)
        # claim[6](新编号1)最先被扫描,它引用顺序是1,2,4,5(按文本里出现顺序)
        # -> note编号应该是 原1->note1, 原2->note2, 原4->note3, 原5->note4
        assert len(result["note"]) == 4
        assert result["note"][0]["number"] == 1
        assert "IMP regime" in result["note"][0]["text"]  # 原编号1的of_terms
        assert result["note"][1]["number"] == 2
        assert "Trojan score" in result["note"][1]["text"]  # 原编号2
        assert result["note"][2]["number"] == 3
        assert "LMC error barrier" in result["note"][2]["text"]  # 原编号4
        assert result["note"][3]["number"] == 4
        # 原编号5的of_terms也是'Trojan score',内容应该跟note2不一样(不同的content)
        assert result["note"][3]["text"] != result["note"][1]["text"]
        assert "Trojan score" in result["note"][3]["text"]

        # 论证(编号8)从没被任何claim的【N】引用过,不应该出现在note里
        assert not any("linear interpolation error barrier" in n["text"] for n in result["note"])
    finally:
        shutil.rmtree(tmp_dir)


def test_elaboration_note_gets_prefix_with_of_terms():
    tmp_dir, paper_dir = _setup_conclusion_2_case()
    try:
        result = step4a.build_claims_final(paper_dir)
        note1 = result["note"][0]
        assert note1["text"].startswith("elaboration of IMP regime: ")
        assert note1["text"] == (
            "elaboration of IMP regime: In the specific iterative magnitude pruning (IMP) "
            "regime adopted for Trojan analysis — where after each pruning round the surviving "
            "weights are not rewound to their initialization and all finetuning uses only the "
            "potentially poisoned dataset $\\mathcal{D}_\\mathrm{p}$ (no clean data) —"
        )
    finally:
        shutil.rmtree(tmp_dir)


def test_claim_text_citations_rewritten_to_note_numbers():
    tmp_dir, paper_dir = _setup_conclusion_2_case()
    try:
        result = step4a.build_claims_final(paper_dir)
        claim1 = result["claim"][0]  # 原claim[6]
        assert "【" not in claim1["text"]  # 全角引用记号应该被完全替换掉
        assert claim1["text"] == (
            "Under the iterative magnitude pruning (IMP) regime described in note 1, "
            "the Trojan score (as defined in note 2, note 3, note 4) is approximately zero "
            "for non-Trojan (clean) models."
        )
    finally:
        shutil.rmtree(tmp_dir)


def test_relation_label_connects_and_expression_remapped():
    tmp_dir, paper_dir = _setup_conclusion_2_case()
    try:
        result = step4a.build_claims_final(paper_dir)
        # 原relation[10]: connects=[6,9,11], expression='([6] 且 [9]) 推出 [11]'
        # 6/9/11 分别是新claim 1/2/3
        rel = next(r for r in result["relation"] if r["connects"] == [1, 2, 3])
        assert rel["expression"] == "([1] 且 [2]) 推出 [3]"
    finally:
        shutil.rmtree(tmp_dir)


def test_note_entry_has_conclusion_field():
    tmp_dir, paper_dir = _setup_conclusion_2_case()
    try:
        result = step4a.build_claims_final(paper_dir)
        for n in result["note"]:
            assert set(n.keys()) == {"conclusion", "text", "number"}
            assert n["conclusion"] == "conclusion_2"
    finally:
        shutil.rmtree(tmp_dir)


def test_relation_entry_has_conclusion_field():
    tmp_dir, paper_dir = _setup_conclusion_2_case()
    try:
        result = step4a.build_claims_final(paper_dir)
        for r in result["relation"]:
            assert set(r.keys()) == {"conclusion", "connects", "expression"}
            assert r["conclusion"] == "conclusion_2"
    finally:
        shutil.rmtree(tmp_dir)


def test_needs_more_context_flows_through_to_claim_entry():
    tmp_dir, paper_dir = _setup_conclusion_2_case()
    try:
        # 手动往这个临时论文的step2_output_claims.json里塞一条带
        # needs_more_context的记录,验证它真的原样传到了claim条目里
        step2_output_path = paper_dir / "step2_output_claims.json"
        records = json.loads(step2_output_path.read_text(encoding="utf-8"))
        records[0]["needs_more_context"] = ["some undefined term"]
        step2_output_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

        result = step4a.build_claims_final(paper_dir)
        claim1 = result["claim"][0]
        assert claim1["needs_more_context"] == ["some undefined term"]
        # 没手动设置过的那条,应该默认是空列表,不是缺失这个键
        assert result["claim"][1]["needs_more_context"] == []
    finally:
        shutil.rmtree(tmp_dir)


def test_claim_entry_has_conclusion_and_four_other_fields():
    tmp_dir, paper_dir = _setup_conclusion_2_case()
    try:
        result = step4a.build_claims_final(paper_dir)
        for c in result["claim"]:
            assert set(c.keys()) == {"conclusion", "text", "is_pure_data", "needs_more_context", "number"}
            assert c["conclusion"] == "conclusion_2"
    finally:
        shutil.rmtree(tmp_dir)


# ============================================================
# 真实数据 2: 867750889056633390/conclusion_4
#   [1] assertion, [6] 论据(supports=[1]), [7] example(illustrates=[1])
#   用来测试单目标的论据/example关系。
# ============================================================

def test_single_target_relation_has_no_parens():
    conclusion = _load_real_conclusion("867750889056633390", "conclusion_4")
    step2_output = [
        {"conclusion": "conclusion_4", "number": 1, "label": "assertion",
         "text": "some assertion text.", "is_pure_data": False, "instance_removed": False},
        {"conclusion": "conclusion_4", "number": 6, "label": "论据",
         "text": "some evidence text.", "is_pure_data": False, "instance_removed": False},
        {"conclusion": "conclusion_4", "number": 7, "label": "example",
         "text": "some example text.", "is_pure_data": False, "instance_removed": False},
    ]
    tmp_dir, paper_dir = _build_temp_paper([conclusion], step2_output)
    try:
        result = step4a.build_claims_final(paper_dir)

        new_numbers = [c["number"] for c in result["claim"]]
        assert new_numbers == [1, 2, 3]  # 原1,6,7 -> 新1,2,3

        # 论据(原6,新2)只support原1(新1) -> 单目标,不带括号
        # example(原7,新3)只illustrate原1(新1) -> 同样单目标,不带括号
        expressions = {r["expression"] for r in result["relation"]}
        assert "[2] 是 [1] 的例子或证据" in expressions
        assert "[3] 是 [1] 的例子或证据" in expressions

        lunju_rel = next(r for r in result["relation"] if r["expression"] == "[2] 是 [1] 的例子或证据")
        assert lunju_rel["connects"] == [2, 1]
    finally:
        shutil.rmtree(tmp_dir)


# ============================================================
# 真实数据 3: 867752822639165809/conclusion_7
#   claim编号(原始) 2,4,7,10,11,14,16 -> 新编号1,2,3,4,5,6,7
#   原14(论据)supports=[2,4,7,11] -> 4个目标,用来测试合并成1条、多目标
#   带括号"和"连接的场景(不再拆成多条1对1)。
# ============================================================

def test_multi_target_relation_merged_with_parens_and_and():
    conclusion = _load_real_conclusion("867752822639165809", "conclusion_7")
    labels = {2: "assertion", 4: "论据", 7: "论据", 10: "example", 11: "assertion", 14: "论据", 16: "论据"}
    step2_output = [
        {"conclusion": "conclusion_7", "number": n, "label": lbl,
         "text": f"claim text {n}.", "is_pure_data": False, "instance_removed": False}
        for n, lbl in labels.items()
    ]
    tmp_dir, paper_dir = _build_temp_paper([conclusion], step2_output)
    try:
        result = step4a.build_claims_final(paper_dir)

        # 原编号升序2,4,7,10,11,14,16 -> 新编号1,2,3,4,5,6,7
        new_numbers = [c["number"] for c in result["claim"]]
        assert new_numbers == [1, 2, 3, 4, 5, 6, 7]

        # 原14(论据,新编号6)supports原[2,4,7,11](新编号[1,2,3,5])
        # -> 应该合并成1条,不是4条,expression带括号+"和"连接
        rel14 = next(r for r in result["relation"] if r["connects"][0] == 6)
        assert rel14["connects"] == [6, 1, 2, 3, 5]
        assert rel14["expression"] == "[6] 是 ([1] 和 [2] 和 [3] 和 [5]) 的例子或证据"

        # 原10(example,新编号4)illustrates原[2,4,7](新编号[1,2,3])
        rel10 = next(r for r in result["relation"] if r["connects"][0] == 4)
        assert rel10["connects"] == [4, 1, 2, 3]
        assert rel10["expression"] == "[4] 是 ([1] 和 [2] 和 [3]) 的例子或证据"

        # relation总数应该等于claim里是论据/example的条数(4,7,10,14,16共5个),
        # 不应该因为多目标被拆成更多条
        assert len(result["relation"]) == 5
    finally:
        shutil.rmtree(tmp_dir)


# ============================================================
# 合成边界情况:elaboration没有of_terms(有数字形式的of,或者完全没有)
# ============================================================

def test_elaboration_without_of_terms_gets_no_prefix():
    conclusion = {
        "id": "paper:x::conclusion_1",
        "content": "irrelevant",
        "organized_parts": [
            {"number": 1, "label": "elaboration", "content": "plain continuation text",
             "of": [3], "source_spans": [{"start": 0, "end": 1}]},
            {"number": 2, "label": "elaboration", "content": "another plain continuation",
             "source_spans": [{"start": 1, "end": 2}]},  # 完全没有of/of_terms
            {"number": 3, "label": "assertion", "content": "x", "source_spans": [{"start": 2, "end": 3}]},
        ],
    }
    step2_output = [
        {"conclusion": "conclusion_1", "number": 3, "label": "assertion",
         "text": "cites 【1】 and 【2】 here.", "is_pure_data": False, "instance_removed": False},
    ]
    tmp_dir, paper_dir = _build_temp_paper([conclusion], step2_output)
    try:
        result = step4a.build_claims_final(paper_dir)
        assert len(result["note"]) == 2
        # 两条note都不该有"elaboration of"前缀——[1]虽然带数字形式的of,
        # 但它是被claim[3]的【1】直接引用才收进来的,按更早确认过的规则
        # 不加前缀;[2]本来就没有of/of_terms
        texts = [n["text"] for n in result["note"]]
        assert texts == ["plain continuation text", "another plain continuation"]
        assert not any(t.startswith("elaboration of") for t in texts)
    finally:
        shutil.rmtree(tmp_dir)


def test_unreferenced_of_claim_elaboration_gets_added_with_prefix():
    """真实数据:867752822639165809/conclusion_1只有1条claim(编号1),
    elaboration#4有of=[1],但没有被这条claim的【N】引用过(实测确认过)。
    按新规则,这条elaboration应该被补收进note,前缀写"elaboration of
    claim 1:"(claim编号1在这个只有1条claim的conclusion里,新编号也是1)。
    """
    conclusion = _load_real_conclusion("867752822639165809", "conclusion_1")
    claim1_part = next(p for p in conclusion["organized_parts"] if p["number"] == 1)
    step2_output = [{
        "conclusion": "conclusion_1", "number": 1, "label": claim1_part["label"],
        "text": "some claim text with no citations at all.",
        "is_pure_data": False, "instance_removed": False,
    }]
    tmp_dir, paper_dir = _build_temp_paper([conclusion], step2_output)
    try:
        result = step4a.build_claims_final(paper_dir)

        assert len(result["claim"]) == 1
        assert result["claim"][0]["number"] == 1

        # elaboration#4(of=[1])没被任何claim引用,应该被新规则补收进note
        note4 = next(
            (n for n in result["note"] if "OOD performance degrades" in n["text"]), None
        )
        assert note4 is not None
        assert note4["text"] == (
            "elaboration of claim 1: (OOD performance degrades as the model "
            "memorizes and learns redundant features)"
        )
    finally:
        shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
