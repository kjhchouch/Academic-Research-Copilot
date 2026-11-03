r"""`SkillFiles` 的沙箱契约。

真实 vendored 树上的正反用例 + tmp fixture 上的 cap/缺根用例。每一条拒绝路径都断言
错误文本里**含出路线索**——D5 的教训：错误文本是功能需求，模型靠它自我纠正；一个只说
「invalid path」的错误等于把恢复路径藏起来。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from paper_agent.skill_loader.files import SkillFileError, SkillFiles

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDOR_ROOT = REPO_ROOT / "skills" / "third_party"


@pytest.fixture(scope="module")
def files() -> SkillFiles:
    if not VENDOR_ROOT.is_dir():
        pytest.skip(f"vendored skills not present at {VENDOR_ROOT}")
    return SkillFiles(VENDOR_ROOT)


def test_reads_a_real_skill_entrypoint(files: SkillFiles) -> None:
    text = files.read("nature-polishing/SKILL.md")
    assert "nature-polishing" in text
    assert text.startswith("---")  # frontmatter 原样返回，不改写


def test_reads_a_sibling_shared_reference(files: SkillFiles) -> None:
    """`../nature-shared/...` 是 14 条真实引用的合法形状：跨 skill 读是特性不是漏洞，
    只要解析后仍在 vendor root 内。"""
    text = files.read("nature-writing/../nature-shared/core/ethics.md")
    assert text.strip()


def test_reads_manifest_yaml(files: SkillFiles) -> None:
    """manifest.yaml 是模型挑 on_demand fragment 的地图，必须可读。"""
    text = files.read("nature-writing/manifest.yaml")
    assert "always_load" in text


@pytest.mark.parametrize(
    "relpath",
    [
        "C:/Windows/system32/config",  # 绝对路径（盘符）
        "/etc/passwd",  # 绝对路径（根斜杠）
        "nature-polishing\\references\\style.md",  # 反斜杠
        "nature-polishing/../../Windows/win.ini",  # .. 穿越：resolve 后逃出 root
        "nature-polishing/..",  # 目标是目录本身
        "CON",  # Win32 保留设备名
        "nature-shared/scripts/check_consistency.py",  # .py 不在知识白名单
        "nature-response/tests/references.json",  # json 不在白名单
        "nature-polishing/no-such-file.md",  # 缺文件
        "no-such-skill/SKILL.md",  # 缺 skill
        "",  # 空
        "   ",  # 空白
    ],
)
def test_rejects_with_actionable_errors(files: SkillFiles, relpath: str) -> None:
    with pytest.raises(SkillFileError) as excinfo:
        files.read(relpath)
    message = str(excinfo.value)
    assert "nature-" in message, f"错误文本必须列出可用的 skill（出路）：{message}"


def test_error_for_missing_file_names_the_recovery_path(files: SkillFiles) -> None:
    with pytest.raises(SkillFileError) as excinfo:
        files.read("nature-polishing/no-such-file.md")
    assert "SKILL.md" in str(excinfo.value)


def test_python_rejection_states_the_reason(files: SkillFiles) -> None:
    with pytest.raises(SkillFileError) as excinfo:
        files.read("nature-shared/scripts/check_consistency.py")
    message = str(excinfo.value)
    assert ".py" in message and ("知识" in message or "不读" in message)


def test_root_absent_reports_present_false(tmp_path: Path) -> None:
    files = SkillFiles(tmp_path / "nowhere")
    assert files.present is False
    with pytest.raises(SkillFileError) as excinfo:
        files.read("nature-polishing/SKILL.md")
    assert "vendor 目录缺失" in str(excinfo.value)


def test_oversized_file_is_rejected_not_truncated(tmp_path: Path) -> None:
    """截断会静默改写知识内容——宁可拒绝。skill 目录名要形似才进 available 列表，
    这里直接在 root 下造一个带 SKILL.md 的目录。"""
    skill = tmp_path / "nature-x"
    skill.mkdir()
    (skill / "SKILL.md").write_text("x", encoding="utf-8")
    big = skill / "big.md"
    big.write_text("长" * 120_000, encoding="utf-8")
    files = SkillFiles(tmp_path, max_chars=100_000)
    with pytest.raises(SkillFileError) as excinfo:
        files.read("nature-x/big.md")
    assert "100000" in str(excinfo.value) or "100,000" in str(excinfo.value)


def test_present_is_true_on_the_real_tree(files: SkillFiles) -> None:
    assert files.present is True


def test_available_lists_the_seven_skills(files: SkillFiles) -> None:
    names = files.available()
    assert "nature-writing" in names and "nature-shared" in names
    assert len(names) == 7
