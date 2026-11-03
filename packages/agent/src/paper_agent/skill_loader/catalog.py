"""intent → skill 映射表，以及 skill pack 的确定性组装。

**映射是配置，不是模型的选择。** 路由照旧只产六个意图；这张静态表决定哪些意图背上
pack 的成本、获得哪类知识。表里加一行就是一个 skill 可达——增长路径是数据不是机制。
v1 刻意不可达的 skill 与理由见 specs/2026-11-02-skill-loader-design.md §3.3。

**pack 组装是确定性的。** 同一意图两次构建逐字节相等——这是 pack 能插在 system
prompt 之后还命中 prompt cache 的前提（同一意图的多轮对话里，pack 的字节与位置都不
变；意图切换导致的 cache miss 是正确行为：skill 确实变了）。

**组装永不 raise。** 磁盘完整性由 `test_vendored_skills.py` 的门禁看守（兄弟引用、
白名单、verbatim）；运行时遇到缺失文件只求可见——pack 里留一行占位警告（带路径），
日志记 warning，不炸当轮回答。这是与「静默失效」的界限：缺失可辨，不是假装没事。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from paper_agent.skill_loader.files import SkillFileError, SkillFiles

logger = logging.getLogger(__name__)

# v1 映射。target 必须是 vendor root 下真实存在的 skill 名
# （test_mapping_targets_exist_on_disk 钉住）。
INTENT_SKILLS: dict[str, str] = {
    "polish": "nature-polishing",
    "draft_section": "nature-writing",
    "outline": "nature-writing",
    "citation_format": "nature-ref-verifier",
}

# 防膨胀门禁：映射内每个 skill 的 pack 上限。实测 v1 量级：writing ≈ 27.6K，
# polishing ≈ 19.4K，ref-verifier ≈ 11.4K——40K 留了余量但不纵容。
MAX_PACK_CHARS = 40_000

# nature-writing 的范围收窄（D8 的三件事之一；L0 guard 拒整篇/降重是另一件，在图边）。
# 这是我们的文本，不是上游内容；声明对 pack 内其余材料有优先级。
_WRITING_NARROWING = (
    "【任务范围（本项目限定，优先于下文 skill 内容）】本次是章节级的规划或起草辅助："
    "只在用户已经提供论点、结果或数据的前提下，协助规划结构、起草或重构指定章节；"
    "不生成整篇论文，也不生成没有用户材料支撑的整章正文。缺论点或数据时先向用户要，"
    "不要编造。整篇代写与降重类请求会被系统直接拒绝。"
)


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """一个可达的 skill：名字与随包注入的范围说明（没有就 None）。"""

    name: str
    narrowing: str | None


def _parse_always_load(manifest_text: str) -> list[str]:
    """manifest.yaml 的 always_load 文件列表（含 `../nature-shared/...` 兄弟引用）。

    解析规则与上游格式对齐：`always_load:` 开块，块内收 `  - path` 项，`#` 注释行
    跳过，第一个非缩进非注释行终止。manifest 是上游数据不是我们的代码——这里只读
    结构，不改写、不校验语义（文件存在性由 vendored 门禁测试看守）。
    """
    entries: list[str] = []
    in_block = False
    for line in manifest_text.splitlines():
        if line.strip() == "always_load:":
            in_block = True
            continue
        if in_block:
            if line.strip().startswith("#"):
                continue
            if line.startswith("  ") and line.strip().startswith("- "):
                entries.append(line.strip()[2:].strip())
            elif line.strip():
                in_block = False
    return entries


class SkillCatalog:
    """vendor root 之上的可达性层：意图 → SkillSpec → pack 文本。"""

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self._files = SkillFiles(root)
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def files(self) -> SkillFiles:
        """底层读取门：`read_skill_file` 工具与 pack 组装共用同一套沙箱与错误文本。"""
        return self._files

    @property
    def reachable(self) -> int:
        """当前可达的 skill 数（映射表里磁盘上真实存在的）。/health 的口径。"""
        if not self._enabled:
            return 0
        present = set(self._files.available())
        return sum(1 for name in set(INTENT_SKILLS.values()) if name in present)

    def skill_for_intent(self, intent: str | None) -> SkillSpec | None:
        """意图命中的 skill，或 None（无映射 / disabled / vendor 缺失 / intent 为空）。

        None 是「这一轮没有 skill」，不是错误：literature_search 有 MCP 工具、
        unclear 是 guard 出口、知识问答有自己的审核层。
        """
        if not self._enabled or intent is None:
            return None
        name = INTENT_SKILLS.get(intent)
        if name is None or name not in self._files.available():
            return None
        return SkillSpec(
            name=name, narrowing=_WRITING_NARROWING if name == "nature-writing" else None
        )

    def pack_for_intent(self, intent: str | None) -> str | None:
        """意图对应的注入文本，或 None。永不 raise；构建失败按 None 处理并记日志。"""
        if not self._enabled or intent is None:
            return None
        spec = self.skill_for_intent(intent)
        if spec is None:
            return None
        try:
            return self.pack_for_skill_name(spec.name, narrowing=spec.narrowing)
        except Exception:  # noqa: BLE001 - pack 失败不能变成回答失败
            logger.exception("skill pack build failed for %s; injecting nothing", spec.name)
            return None

    def pack_for_skill_name(self, name: str, *, narrowing: str | None = None) -> str | None:
        """SKILL.md + always_load 的完整注入文本；不校验映射表，供门禁测试逐 skill 调用。"""
        if name not in self._files.available():
            return None
        sections: list[str] = [self._header(name, narrowing)]
        try:
            skill_md = self._files.read(f"{name}/SKILL.md")
        except SkillFileError as exc:
            logger.warning("skill %s has no readable SKILL.md: %s", name, exc)
            return None
        sections.append(f"=== {name}/SKILL.md ===\n{skill_md}")

        manifest_path = Path(name) / "manifest.yaml"
        try:
            manifest = self._files.read(manifest_path.as_posix())
        except SkillFileError:
            manifest = ""
        for entry in _parse_always_load(manifest):
            relpath = f"{name}/{entry}"
            try:
                body = self._files.read(relpath)
            except SkillFileError as exc:
                # 缺失可辨：占位行保留路径，日志留痕。不假装加载成功。
                logger.warning("always_load entry missing for %s: %s", name, exc)
                sections.append(f"=== {relpath}（缺失，未加载）===")
                continue
            sections.append(f"=== {relpath} ===\n{body}")
        return "\n\n".join(sections)

    def _header(self, name: str, narrowing: str | None) -> str:
        lines = [
            f"【skill 加载：{name}】",
            (
                "下文是该 skill 的入口文件与 always_load 材料（vendored 知识，"
                "Apache-2.0，经审查）。本任务按上述 skill 的指引工作；若 SKILL.md "
                "的 manifest 指到更多按需文件，用 read_skill_file 工具读取，路径"
                "相对 skills/third_party，例如 nature-writing/references/introduction.md。"
            ),
        ]
        if narrowing:
            lines.append(narrowing)
        return "\n".join(lines)
