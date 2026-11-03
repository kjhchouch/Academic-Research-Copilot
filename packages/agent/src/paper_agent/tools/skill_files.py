"""`read_skill_file`：模型按需读取 vendored skill 文件的唯一通道。

同步进程内工具，与 `count_words` 同一档：确定性、离线、无副作用（读而已）。

**永不 raise 是本工具的硬契约。** D6 的教训：`ToolNode._default_handle_tool_errors`
只吸收特定异常类型，其余一律重抛——一个会抛的进程内工具能掀翻整条 SSE run。MCP
bridge 为此「刻意不做自愈、错误文本二分」；本工具更简单：`SkillFileError` 在工具
内部转成错误文本返回，文本里带出路线索（可用的 skill 列表、正确示例）。模型读到
错误文本后可以自己纠正路径再试——错误文本就是恢复路径（D5 的教训）。

渐进披露的另一半：SKILL.md/manifest 的 references 和 axes 指到几百个按需文件，
skill pack 只注入 SKILL.md + always_load；剩下的由模型按 manifest 的条件用本工具
取——这是 skill 自己设计的加载方式，不是我们发明的第二套路由。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool

from paper_agent.skill_loader.files import SkillFileError, SkillFiles

_TOOL_DESCRIPTION = (
    "读取一个 skill 知识文件的正文。path 是相对 skills/third_party 的相对路径，"
    "例如 nature-polishing/SKILL.md、nature-writing/references/introduction.md、"
    "nature-writing/../nature-shared/core/ethics.md（跨 skill 读共享材料是合法的）。"
    "只接受 .md / .yaml / .tex；先读对应 skill 的 SKILL.md 或 manifest.yaml 找到"
    "需要的文件路径再调用。"
)


def make_read_skill_file_tool(files: SkillFiles) -> BaseTool:
    """围绕一个 `SkillFiles` 门构建工具。vendor 缺失时工具照常注册——错误文本会
    告诉模型 vendor 目录缺失，比「工具凭空消失让模型猜」诚实。"""

    def read_skill_file(path: str) -> str:
        try:
            return files.read(path)
        except SkillFileError as exc:
            return str(exc)

    return StructuredTool(
        name="read_skill_file",
        description=_TOOL_DESCRIPTION,
        func=read_skill_file,
        args_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "相对 skills/third_party 的文件路径，如 nature-polishing/SKILL.md"
                    ),
                }
            },
            "required": ["path"],
        },
    )
