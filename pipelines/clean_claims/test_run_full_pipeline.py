"""
test_run_full_pipeline.py

不调用真实的14个步骤脚本(那需要真实API访问,而且沙盒没有网络),只测试
run_step()这个编排逻辑本身对不对——用真实写到磁盘上的、极简的"假步骤
脚本"(不是mock掉subprocess,是真的用subprocess跑一个真实存在的.py文件)
模拟三种关键场景:
  1. 正常成功(退出码0,且生成了预期文件)
  2. 退出码非0(模拟真实步骤跑失败的情况)
  3. 退出码是0,但没有生成预期文件(专门模拟step1b_organize_labeled_
     content.py在缺输入文件时sys.exit(0)的真实行为——这是run_step()
     存在的核心原因,必须单独测这一条)

用法:
    python test_run_full_pipeline.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

import run_full_pipeline as pipeline


def _write_fake_step(dir_path: Path, filename: str, body: str) -> Path:
    path = dir_path / filename
    path.write_text(body, encoding="utf-8")
    return path


def test_success_case_returns_true():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        script = _write_fake_step(tmp_dir, "fake_ok.py", (
            "import sys\n"
            "paper_id = sys.argv[1]\n"
            "print(f'processed {paper_id}')\n"
        ))
        output_path = tmp_dir / "some_output.json"
        output_path.write_text("[]", encoding="utf-8")  # 模拟脚本自己产出了这个文件

        ok = pipeline.run_step(script, output_path, "fake_paper")
        assert ok is True
    finally:
        shutil.rmtree(tmp_dir)


def test_nonzero_exit_code_returns_false():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        script = _write_fake_step(tmp_dir, "fake_fail.py", (
            "import sys\n"
            "print('something went wrong')\n"
            "sys.exit(1)\n"
        ))
        output_path = tmp_dir / "never_created.json"

        ok = pipeline.run_step(script, output_path, "fake_paper")
        assert ok is False
        assert not output_path.exists()
    finally:
        shutil.rmtree(tmp_dir)


def test_exit_zero_but_no_output_returns_false():
    """专门复现step1b_organize_labeled_content.py的真实行为:退出码是0
    (表示"正常跳过",不是出错),但因为上游输入缺失,压根没写出预期的
    输出文件——这种情况run_step()必须判定为失败,不能被"退出码是0"
    骗过去。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        script = _write_fake_step(tmp_dir, "fake_silent_skip.py", (
            "import sys\n"
            "print('跳过:找不到输入文件')\n"
            "sys.exit(0)\n"  # 关键:退出码是0,不是1
        ))
        output_path = tmp_dir / "should_have_been_created.json"  # 这个脚本不会创建它

        ok = pipeline.run_step(script, output_path, "fake_paper")
        assert ok is False  # 尽管退出码是0,也必须判失败
        assert not output_path.exists()
    finally:
        shutil.rmtree(tmp_dir)


def test_paper_id_is_passed_as_argv():
    """确认paper_id确实是通过命令行参数传给子进程的(跟全部14个真实
    步骤脚本"sys.argv[1]或input()"这个既定读取方式对应)。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        script = _write_fake_step(tmp_dir, "fake_echo_argv.py", (
            "import sys\n"
            "from pathlib import Path\n"
            "paper_id = sys.argv[1]\n"
            "Path('" + str(tmp_dir).replace('\\', '\\\\') + "/echoed.txt').write_text(paper_id)\n"
        ))
        output_path = tmp_dir / "echoed.txt"

        ok = pipeline.run_step(script, output_path, "my-specific-paper-id-123")
        assert ok is True
        assert output_path.read_text() == "my-specific-paper-id-123"
    finally:
        shutil.rmtree(tmp_dir)


def test_main_stops_at_first_failing_step_via_temp_pipeline():
    """构造一条3步的迷你流水线(第1步成功,第2步"退出码0但不产出文件",
    第3步理论上不该被跑到),验证main()式的"失败即停"逻辑——直接复用
    run_step()逐步调用,不重新实现一遍main()的参数解析部分。"""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        step1 = _write_fake_step(tmp_dir, "step1.py", (
            "from pathlib import Path\n"
            "Path(__file__).parent.joinpath('out1.json').write_text('[]')\n"
        ))
        step2 = _write_fake_step(tmp_dir, "step2.py", (
            "print('装作正常跳过')\n"
        ))  # 退出码默认是0,但不产出out2.json
        step3_called = tmp_dir / "step3_was_called.txt"
        step3 = _write_fake_step(tmp_dir, "step3.py", (
            f"from pathlib import Path\n"
            f"Path(r'{step3_called}').write_text('yes')\n"
        ))

        results = []
        for script, out_name in [(step1, "out1.json"), (step2, "out2.json"), (step3, "out3.json")]:
            ok = pipeline.run_step(script, tmp_dir / out_name, "fake_paper")
            results.append(ok)
            if not ok:
                break

        assert results == [True, False]  # 第3步不该被跑到
        assert not step3_called.exists()
    finally:
        shutil.rmtree(tmp_dir)


def test_noninteractive_step_receives_eof_instead_of_waiting_for_user():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        script = _write_fake_step(root, "needs_input.py", "input('retry?')\n")
        output = root / "partial.json"
        output.write_text("{}", encoding="utf-8")
        assert pipeline.run_step(script, output, "test", non_interactive=True) is False


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  OK: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")
