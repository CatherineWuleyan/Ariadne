"""
test_step4b_add_instantiated_claims.py

用真实的867757662605934651整篇论文数据(claims_final.json已经由step4a
真实跑出,13条claim/4条note/6条relation)+真实的实例化claim文本(就是
用户在对话里贴过的那条conclusion_3 claim[1]的实例化结果)测试主流程,
再用合成场景补测引用改写、重复运行保护、排序、空文件这几个边界情况。

用法:
    python test_step4b_add_instantiated_claims.py
"""

import json
import shutil
import tempfile
from pathlib import Path

import step4a_build_claims_final as step4a
import step4b_add_instantiated_claims as step4b


_REAL_PAPER_DIR = step4b.DATA_DIR / "867757662605934651"

_REAL_INSTANTIATED_TEXT = (
    "Empirically, across the four architectures ResNet-20s, ResNet-18, VGG-16, and "
    "DenseNet-100 and the three datasets CIFAR-10, CIFAR-100, and Restricted ImageNet "
    "and for the four diverse Trojan types gray-scale patch, RGB patch, clean-label, "
    "and stealthier WaNet, there exists an extreme-sparsity subnetwork identified by a "
    "peak in the Trojan score (the LMC-based metric $\\mathcal{S}_{\\mathrm{Trojan}}$ "
    "that quantifies pruning-induced stability via the error barrier between "
    "un-finetuned and finetuned Trojan tickets), which simultaneously (i) preserves "
    "the Trojan attack effectiveness in terms of attack success rate (ASR) nearly as "
    "well as the original dense Trojan model and (ii) has near-random standard "
    "accuracy (SA) on clean inputs due to the aggressive pruning."
)


def _copy_real_paper_with_special_claims(special_claims: list) -> tuple:
    """复制真实的867757662605934651整篇论文所需的源文件(organized_content
    .json / step2_output_claims.json)到临时目录,并在临时目录里用step4a
    现场重新生成一份干净的claims_final.json(不直接拷贝磁盘上现有的
    claims_final.json——那份文件可能已经被之前别的手动/CLI测试追加过,
    不是保证干净的13条初始状态,测试不应该依赖这种容易被弄脏的外部
    可变状态)。"""
    tmp_dir = Path(tempfile.mkdtemp())
    paper_dir = tmp_dir / "fake_paper"
    paper_dir.mkdir()
    for fname in ("organized_content.json", "step2_output_claims.json"):
        shutil.copy(_REAL_PAPER_DIR / fname, paper_dir / fname)

    fresh_claims_final = step4a.build_claims_final(paper_dir)
    (paper_dir / "claims_final.json").write_text(
        json.dumps(fresh_claims_final, ensure_ascii=False), encoding="utf-8"
    )

    (paper_dir / "special_claims.json").write_text(
        json.dumps(special_claims, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_dir, paper_dir


def test_needs_more_context_is_inherited_from_original_claim():
    """实例化claim的needs_more_context应该原样继承自它对应的原始claim,
    不是重新算、也不是丢弃。"""
    special_claims = [{
        "conclusion": "conclusion_3", "claim_number": 1,
        "text": "instantiated text, no citations.",
        "certain_instances_given": [2], "uncertain_instances_given": [],
    }]
    tmp_dir, paper_dir = _copy_real_paper_with_special_claims(special_claims)
    try:
        # 手动往对应的原始claim(conclusion_3, number=1)的
        # step2_output_claims.json记录里塞一个needs_more_context
        step2_output_path = paper_dir / "step2_output_claims.json"
        records = json.loads(step2_output_path.read_text(encoding="utf-8"))
        for r in records:
            if r["conclusion"] == "conclusion_3" and r["number"] == 1:
                r["needs_more_context"] = ["some undefined term"]
        step2_output_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

        result = step4b.add_instantiated_claims(paper_dir)
        new_claim = result["claim"][-1]
        assert new_claim["needs_more_context"] == ["some undefined term"]
    finally:
        shutil.rmtree(tmp_dir)


def test_real_data_instantiated_claim_appended_correctly():
    special_claims = [{
        "conclusion": "conclusion_3", "claim_number": 1,
        "text": _REAL_INSTANTIATED_TEXT,
        "certain_instances_given": [2, 3, 4], "uncertain_instances_given": [],
    }]
    tmp_dir, paper_dir = _copy_real_paper_with_special_claims(special_claims)
    try:
        result = step4b.add_instantiated_claims(paper_dir)

        # 原来13条claim,追加1条应该变成14条,新的这条编号是14
        assert len(result["claim"]) == 14
        new_claim = result["claim"][-1]
        assert new_claim["number"] == 14
        assert new_claim["conclusion"] == "conclusion_3"
        assert new_claim["text"] == _REAL_INSTANTIATED_TEXT  # 这条本身没有【N】引用,原样进来
        assert new_claim["is_pure_data"] is False

        # note列表不应该变(5条,不变)
        assert len(result["note"]) == 5

        # 原来6条relation,追加1条变成7条;conclusion_3 claim[1]是新claim 4,
        # 所以新关系应该是"[14] 是 [4] 的instance"
        assert len(result["relation"]) == 7
        new_relation = result["relation"][-1]
        assert new_relation == {
            "conclusion": "conclusion_3", "connects": [14, 4], "expression": "[14] 是 [4] 的instance",
        }
    finally:
        shutil.rmtree(tmp_dir)


def test_citations_inside_instantiated_text_are_rewritten():
    """构造一条"实例化文本里也带【N】引用"的情况,验证复用的
    rewrite_claim_text_citations在这里同样生效——用conclusion_2的
    claim[6](新编号1,原文引用了【1】【2】【4】【5】,对应note1-4)来测,
    假装它也被实例化了一次(内容不用完全真实,只是要触发引用改写这条
    路径)。"""
    special_claims = [{
        "conclusion": "conclusion_2", "claim_number": 6,
        "text": "Instantiated version still cites 【1】 and 【2】 here.",
        "certain_instances_given": [99], "uncertain_instances_given": [],
    }]
    tmp_dir, paper_dir = _copy_real_paper_with_special_claims(special_claims)
    try:
        result = step4b.add_instantiated_claims(paper_dir)
        new_claim = result["claim"][-1]
        assert "【" not in new_claim["text"]
        assert new_claim["text"] == "Instantiated version still cites note 1 and note 2 here."
    finally:
        shutil.rmtree(tmp_dir)


def test_multiple_special_claims_sorted_by_conclusion_and_number():
    """special_claims.json里故意乱序放,验证追加时会按conclusion在
    organized_content.json里的出现顺序+conclusion内claim_number升序
    重新排,不依赖文件本身的顺序。"""
    special_claims = [
        {"conclusion": "conclusion_3", "claim_number": 1, "text": "c3-claim1 instantiated.",
         "certain_instances_given": [2], "uncertain_instances_given": []},
        {"conclusion": "conclusion_2", "claim_number": 6, "text": "c2-claim6 instantiated.",
         "certain_instances_given": [1], "uncertain_instances_given": []},
    ]  # 故意先放conclusion_3再放conclusion_2,顺序反的
    tmp_dir, paper_dir = _copy_real_paper_with_special_claims(special_claims)
    try:
        result = step4b.add_instantiated_claims(paper_dir)
        new_entries = result["claim"][-2:]
        # conclusion_2在organized_content.json里排在conclusion_3前面,
        # 追加顺序应该是conclusion_2的先、conclusion_3的后(重新排过了)
        assert new_entries[0]["text"].startswith("c2-claim6")
        assert new_entries[0]["number"] == 14
        assert new_entries[1]["text"].startswith("c3-claim1")
        assert new_entries[1]["number"] == 15
    finally:
        shutil.rmtree(tmp_dir)


def test_empty_special_claims_is_noop():
    tmp_dir, paper_dir = _copy_real_paper_with_special_claims([])
    try:
        before = json.loads((paper_dir / "claims_final.json").read_text(encoding="utf-8"))
        result = step4b.add_instantiated_claims(paper_dir)
        assert result == before
    finally:
        shutil.rmtree(tmp_dir)


def test_running_twice_raises_instead_of_duplicating():
    special_claims = [{
        "conclusion": "conclusion_3", "claim_number": 1, "text": "instantiated once.",
        "certain_instances_given": [2], "uncertain_instances_given": [],
    }]
    tmp_dir, paper_dir = _copy_real_paper_with_special_claims(special_claims)
    try:
        result = step4b.add_instantiated_claims(paper_dir)
        # 模拟"已经写回过一次"的状态
        (paper_dir / "claims_final.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        try:
            step4b.add_instantiated_claims(paper_dir)
            raised = False
        except RuntimeError:
            raised = True
        assert raised
    finally:
        shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
