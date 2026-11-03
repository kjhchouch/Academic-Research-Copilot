"""Skill 文件的沙箱磁盘访问。

vendored skill 内容与「检索回来的文本」是两种东西，边界必须分清：检索文本是不可信
输入（过注入检测、进不了指令通道）；vendored 树是**逐行审查过、进版本库、由 PR 门禁
看守的知识资产**（见 `packages/agent/tests/test_vendored_skills.py` 的白名单门禁）。
所以这里不做内容级注入扫描——那是对已审查资产的错分类——只做**路径级**沙箱：
模型能读到的，只能是 vendor root 之内、白名单扩展名、非保留名的文件。

失败语义：一切拒绝都抛 `SkillFileError`，`str(exc)` 就是给模型的错误文本。两条既有
纪律在此汇合——错误文本必须**给出路**（D5：「错误文本是功能需求而不是润色」），而
**把错误变成文本这件事发生在工具层**（D6：`ToolNode` 会重抛未吸收的异常，一个会抛的
工具能掀翻整条 SSE run；`tools/skill_files.py` 负责 try/except）。
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

# 知识白名单：markdown 是正文，yaml 是 manifest（模型挑 fragment 的地图），tex 是
# 模板知识。.py 明确在白名单外——vendored 脚本经审查后「不被执行」，这里再加一层
# 「不被读取」：模型没有任何理由需要它们。
ALLOWED_SUFFIXES = frozenset({".md", ".yaml", ".tex"})

# Win32 保留设备名（D1 在 LocalBlobStore 里踩过：Path("..").name 与 CON 都能穿过
# 天真的校验，open("CON") 会挂到控制台而不是报错）。小写比较。
_WIN32_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

MAX_FILE_CHARS = 100_000


class SkillFileError(Exception):
    """路径被沙箱拒绝或读取失败。`str(exc)` 即给模型的错误文本，必须含出路线索。"""


class SkillFiles:
    """一个 vendor root 的读取门。纯同步、无网络、无副作用。"""

    def __init__(self, root: Path, *, max_chars: int = MAX_FILE_CHARS) -> None:
        self._root = root
        self._max_chars = max_chars

    @property
    def root(self) -> Path:
        return self._root

    def available(self) -> list[str]:
        """root 下有 SKILL.md 的 skill 名，排序。错误文本的出路来源。"""
        if not self._root.is_dir():
            return []
        return sorted(
            p.name for p in self._root.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()
        )

    @property
    def present(self) -> bool:
        """vendor root 是否真的在那。缺 root 是可降级的配置事实，不是异常。"""
        return bool(self.available())

    def read(self, relpath: str) -> str:
        """读一个相对 vendor root 的文件；一切失败 raise `SkillFileError`。

        校验顺序即错误文本的顺序：先拒形状（空/反斜杠/绝对路径），再拒类型（扩展名/
        保留名），最后才碰磁盘。**`..` 段不在此列**：manifest 的 always_load 里有 14 条
        `../nature-shared/...` 兄弟引用，是合法形状；真正的边界是下面的 resolve 后
        包含性检查——`nature-polishing/../../Windows/win.ini` 解析后不在 root 内，照样
        被拒。每条错误的文本都带同一句出路——可用的 skill 列表加一个正确示例。
        """
        hint = (
            f"可用的 skill：{', '.join(self.available()) or '（vendor 目录缺失）'}；"
            "示例：nature-polishing/SKILL.md"
        )

        if not relpath or not relpath.strip():
            raise SkillFileError(f"路径为空。{hint}")
        if "\\" in relpath:
            raise SkillFileError(f"路径里不能有反斜杠，用正斜杠。{hint}")
        if relpath.startswith("/") or PureWindowsPath(relpath).is_absolute():
            raise SkillFileError(f"路径必须是相对 skills/third_party 的相对路径。{hint}")
        suffix = PureWindowsPath(relpath).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise SkillFileError(
                f"只允许读取知识文件（{'、'.join(sorted(ALLOWED_SUFFIXES))}）；"
                f"{suffix or '（无扩展名）'} 不在其中——脚本文件不是知识，本工具不读。{hint}"
            )
        final = PureWindowsPath(relpath).name
        if final.upper() in _WIN32_RESERVED:
            raise SkillFileError(f"文件名 {final!r} 是 Windows 保留设备名。{hint}")

        root = self._root.resolve()
        target = (root / relpath).resolve()
        if not target.is_relative_to(root):
            raise SkillFileError(f"解析后的路径逃出了 vendor 目录。{hint}")
        if not target.is_file():
            raise SkillFileError(
                f"文件不存在：{relpath}。先读对应 skill 的 SKILL.md 找到正确路径；{hint}"
            )
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > self._max_chars:
            raise SkillFileError(
                f"文件超过 {self._max_chars} 字符（实际 {len(text)}），拒绝读取——"
                "截断会静默改写知识内容；如果是正常文件，说明 vendor 树异常。"
            )
        return text
