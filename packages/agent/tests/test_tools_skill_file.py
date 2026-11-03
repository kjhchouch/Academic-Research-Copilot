"""`read_skill_file` 工具层的契约：永不 raise，错误文本是恢复路径。

D6 的教训是这条设计的存在理由：`ToolNode._default_handle_tool_errors` 只吸收特定
异常类型，其余一律重抛——一个会抛的进程内工具能掀翻整条 SSE run。所以本工具的
全部失败模式都必须返回字符串。这里的反向验证（一个都不许抛）不是形式：它是
「SkillFileError → 文本」这层转换真的存在的证明。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import BaseTool
from paper_agent.skill_loader import SkillFiles
from paper_agent.tools import make_read_skill_file_tool

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDOR_ROOT = REPO_ROOT / "skills" / "third_party"


@pytest.fixture(scope="module")
def tool() -> BaseTool:
    if not VENDOR_ROOT.is_dir():
        pytest.skip(f"vendored skills not present at {VENDOR_ROOT}")
    return make_read_skill_file_tool(SkillFiles(VENDOR_ROOT))


def _invoke(tool: BaseTool, path: str) -> str:
    """同步调用。断言返回的是 str——工具永远不抛，是这里最要紧的一行。"""
    result = tool.invoke({"path": path})
    assert isinstance(result, str), f"工具必须返回文本（错误也是文本），不是 {type(result)}"
    return result


def test_reads_a_real_file(tool: BaseTool) -> None:
    text = _invoke(tool, "nature-polishing/SKILL.md")
    assert "nature-polishing" in text


def test_description_teaches_the_path_convention(tool: BaseTool) -> None:
    assert "skills/third_party" in tool.description
    assert "read_skill_file" in tool.description or "path" in tool.description


@pytest.mark.parametrize(
    "path",
    [
        "nature-polishing/../../Windows/win.ini",
        "C:/Windows/win.ini",
        "nature-shared/scripts/check_consistency.py",
        "nature-polishing/no-such-file.md",
        "",
        "CON",
    ],
)
def test_every_failure_mode_returns_text_not_raise(tool: BaseTool, path: str) -> None:
    text = _invoke(tool, path)
    assert "可用的 skill" in text, f"错误文本必须带出路：{text}"


def test_error_text_names_the_reason(tool: BaseTool) -> None:
    assert "不读" in _invoke(tool, "nature-shared/scripts/check_consistency.py")
    # .md 后缀穿过扩展名检查，逃逸由包含性检查拦下——顺序即语义，各拦各的。
    assert "逃出" in _invoke(tool, "nature-polishing/../../../Windows/nothing.md")


def test_tool_never_raises_on_a_missing_root(tmp_path: Path) -> None:
    tool = make_read_skill_file_tool(SkillFiles(tmp_path / "nowhere"))
    text = _invoke(tool, "nature-polishing/SKILL.md")
    assert "vendor 目录缺失" in text
