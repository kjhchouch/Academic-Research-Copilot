"""Skill loader：让 vendored skill 从「仓库里存在」变成「运行时可达」。

可达的三件事（论证见 specs/2026-11-02-skill-loader-design.md）：

1. **intent → skill 映射**（catalog.INTENT_SKILLS，静态表，`route` 节点写 state）；
2. **skill pack 注入**（`agent` 节点：SKILL.md + manifest 的 always_load，包上我们的
   范围说明——确定性注入，不靠模型自觉去读；D8 警告的「prompt 组装期静默失效」类
   失败不允许在这里发生）；
3. **read_skill_file 工具**（on_demand 渐进披露；永不 raise，错误文本是恢复路径）。

本包只加载、不修改：vendored 树是 verbatim 副本（NOTICE 约束），一字不改。
"""

from paper_agent.skill_loader.catalog import (
    INTENT_SKILLS,
    MAX_PACK_CHARS,
    SkillCatalog,
    SkillSpec,
)
from paper_agent.skill_loader.files import MAX_FILE_CHARS, SkillFileError, SkillFiles

__all__ = [
    "INTENT_SKILLS",
    "MAX_FILE_CHARS",
    "MAX_PACK_CHARS",
    "SkillCatalog",
    "SkillFileError",
    "SkillFiles",
    "SkillSpec",
]
