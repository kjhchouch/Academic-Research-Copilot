# Skill Loader 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 7 个 vendored skill 里与现有意图匹配的 3 个真正可达：intent→skill 映射、
SKILL.md+always_load 的确定性注入（含 nature-writing 收窄包装）、沙箱化的
`read_skill_file` 工具。

**Architecture:** 映射在 `route` 节点（写 `state["skill"]`），注入在 `agent` 节点
（system prompt 之后插一条 SystemMessage pack），渐进披露靠永不 raise 的
`read_skill_file` 工具。vendored 树零改动（NOTICE verbatim 约束）。

**Tech Stack:** LangGraph/LangChain（StructuredTool）、pydantic-free 纯函数、pytest 离线。

**命令约定：** `uv run --no-sync pytest <path> -q`；`uv run --no-sync ruff check . && uv run --no-sync ruff format --check .`。执行方式 inline（同会话），提交留给用户。

---

### Task 1: `paper_agent.skill_loader.files`——沙箱磁盘访问

**Files:**
- Create: `packages/agent/src/paper_agent/skill_loader/__init__.py`（最小导出）
- Create: `packages/agent/src/paper_agent/skill_loader/files.py`
- Test: `packages/agent/tests/test_skill_files.py`

- [ ] **Step 1: 写失败测试**——真实 vendored 树上：读 `nature-polishing/SKILL.md` 成功；
  读 `nature-writing/../nature-shared/core/ethics.md` 成功（兄弟引用是合法用例）；拒绝——
  绝对路径（`C:/x`、`/x`）、反斜杠、`..` 段、Win32 保留名（`CON`）、非白名单扩展名
  （`.py`：`nature-shared/scripts/check_consistency.py`）、缺文件；每个错误文本都含
  「可用」出路信息（列出根目录下的 skill 名）；tmp fixture 上测 size cap（>100K → 错误）。

```python
class SkillFileError(Exception):
    """沙箱拒绝或读取失败；str(exc) 即给模型的错误文本（含出路）。"""

class SkillFiles:
    def __init__(self, root: Path, *, max_chars: int = 100_000): ...
    @property
    def present(self) -> bool: ...          # root 存在且含 SKILL.md 的目录 ≥1
    def read(self, relpath: str) -> str:    # 全部失败 raise SkillFileError
```

- [ ] **Step 2: 失败确认。**
- [ ] **Step 3: 实现**。校验顺序（每条失败都 raise，文本不同）：非空 → 无反斜杠 →
  非绝对路径（`PureWindowsPath(relpath).is_absolute()` 与 `startswith(("/","\\"))`）→
  无 `..` 段 → 扩展名 ∈ {`.md`,`.yaml`,`.tex`} → 文件名非 Win32 保留名（复用 D1
  `storage.py` 的保留名清单思路，本地重建常量）→ `resolve()` 后
  `is_relative_to(root.resolve())` → 存在且是文件 → 读入 ≤ max_chars。docstring 写明
  「vendored 内容是经审查的版本内知识，不是检索来的不可信输入，不做注入扫描」。
- [ ] **Step 4: 测试通过。**

### Task 2: `catalog.py`——映射表 + pack 组装

**Files:**
- Create: `packages/agent/src/paper_agent/skill_loader/catalog.py`
- Modify: `packages/agent/src/paper_agent/skill_loader/__init__.py`（导出）
- Test: `packages/agent/tests/test_skill_loader.py`

- [ ] **Step 1: 写失败测试**——映射键 ⊆ `decision_model.INTENTS` 交叉测试；`unclear`/
  `literature_search`/`knowledge_qa` 无映射；`SkillCatalog(root, enabled=True).skill_for_intent`
  返回 `SkillSpec`（polish→nature-polishing；draft_section/outline→nature-writing；
  citation_format→nature-ref-verifier）；disabled / root 缺失 → 恒 None；
  `pack_for_intent("draft_section")` 非空、两次调用逐字节相等、含收窄文本
  （「优先于下文 skill 内容」/「章节级」/「先向用户要」）、含 SKILL.md 正文首行、
  含 always_load 文件路径头；每个映射内 skill 的 pack ≤ 40_000 chars（真实树）；
  root 缺失 → pack None。

```python
INTENT_SKILLS: dict[str, str] = {
    "polish": "nature-polishing",
    "draft_section": "nature-writing",
    "outline": "nature-writing",
    "citation_format": "nature-ref-verifier",
}

@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    narrowing: str | None   # nature-writing 携带收窄文本，其余 None

class SkillCatalog:
    def __init__(self, root: Path, *, enabled: bool = True): ...
    def skill_for_intent(self, intent: str | None) -> SkillSpec | None: ...
    def pack_for_intent(self, intent: str | None) -> str | None:  # 永不 raise
```

- [ ] **Step 2: 失败确认。**
- [ ] **Step 3: 实现**。manifest 解析沿用侦察脚本验证过的规则（`always_load:` 块内，
  跳过 `#` 注释行，收 `  - path` 项，非缩进行终止）；pack 组装 =
  包装头（含 narrowing 段）+ `=== SKILL.md ===` 正文 + 每个 always_load 文件
  `=== <path> ===` 正文；任何文件读取失败 → 该文件记一行占位警告、**不炸 pack**
  （pack 构建永不 raise，部分缺失用日志暴露——与「静默失效」的界限：缺文件是
  兄弟门禁测试管的事，运行时只求可见）。
- [ ] **Step 4: 测试通过。**

### Task 3: `read_skill_file` 工具

**Files:**
- Create: `packages/agent/src/paper_agent/tools/skill_files.py`
- Modify: `packages/agent/src/paper_agent/tools/__init__.py`（导出工厂）
- Test: `packages/agent/tests/test_tools_skill_file.py`

- [ ] **Step 1: 写失败测试**——合法路径返回正文（真实树）；所有失败模式返回**错误字符串
  而非 raise**（对每个 SkillFileError 用例断言 `isinstance(result, str)` 且含出路线索；
  反向验证：没有一次抛出）；description 提到「路径相对 skills/third_party」。

```python
def make_read_skill_file_tool(files: SkillFiles) -> BaseTool:
    """同步工具；SkillFileError 在工具内部转成错误文本（ToolNode 会重抛其他异常）。"""
```

- [ ] **Step 2: 失败确认。**
- [ ] **Step 3: 实现**（`StructuredTool`，同步函数，`try/except SkillFileError` 包住
  `files.read`）。
- [ ] **Step 4: 测试通过。**

### Task 4: 图接入——state + route/agent/builder

**Files:**
- Modify: `packages/agent/src/paper_agent/graph/state.py`（`skill: NotRequired[str | None]`，
  docstring 增 D10 行）
- Modify: `packages/agent/src/paper_agent/graph/nodes.py`（`make_route_node(router,
  skill_catalog=None)` 写 `skill`；`make_agent_node(..., skill_catalog=None)` 注入 pack）
- Modify: `packages/agent/src/paper_agent/graph/builder.py`（`build_graph(...,
  skill_catalog=None)` 穿过两处）
- Test: `packages/agent/tests/test_graph_skills.py`

- [ ] **Step 1: 写失败测试**——捕获型假模型（记录 `ainvoke` 的 messages，返回固定
  AIMessage）+ ScriptedRouter：intent=polish → `state["skill"]=="nature-polishing"` 且
  model 收到的 messages[1] 是 pack（messages[0] 是 system prompt）；intent 无映射 →
  messages 长度 = prefix+history（无 pack）、`state["skill"] is None`；catalog=None →
  同上；draft_section → pack 含收窄文本；知识路径（bind_knowledge + knowledge_model）→
  无 pack；裸环（router=None）→ 不写 skill、无 pack。脚本化路由的 `Decisions.intent`
  用真实词表值。

- [ ] **Step 2: 失败确认。**
- [ ] **Step 3: 实现**。route 节点：`skill = skill_catalog.skill_for_intent(decisions.intent)
  if skill_catalog else None`，知识分支写 `"skill": None`；agent 节点非知识分支：
  `pack = skill_catalog.pack_for_intent(state.get("intent")) if skill_catalog else None`，
  `messages = prefix + ([SystemMessage(pack)] if pack else []) + _complete_tool_history(...)`。
- [ ] **Step 4: 测试通过 + packages/agent 全量无回归。**

### Task 5: 网关——config / main / SSE / health

**Files:**
- Modify: `apps/api/src/api/config.py`（`skills_enabled: bool = True`、
  `skills_dir: str = "skills/third_party"`）
- Modify: `apps/api/src/api/main.py`（`_build_skill_catalog`；工具注册在
  `assert_unique_tool_names` 之前；`build_graph(skill_catalog=…)`；`/health` 增
  `skills: {enabled, reachable}`）
- Modify: `apps/api/src/api/routes/chat.py`（`_route_event` 增 `"skill"` 字段）
- Modify: `.env.example`（两个新变量 + 注释）
- Test: `apps/api/tests/test_api_skills.py`

- [ ] **Step 1: 写失败测试**——route 事件含 `skill`（ScriptedRouter(polish) + 真编译图
  带 catalog）且原七字段仍在（形状 additive）；/health 的 skills 块键集合。
- [ ] **Step 2: 失败确认。**
- [ ] **Step 3: 实现。**
- [ ] **Step 4: apps/api 全量无回归。**

### Task 6: 门禁 + 文档

- [ ] 全套 `uv run --no-sync pytest -q` 全绿；ruff check + format 双绿（vendored 树
  保持零改动——`git status` 里不得出现 `skills/` 下的修改）。
- [ ] README：架构图 SK 节点注一行「D10 已接入（3 skill 可达）」；第三方 skill 小节
  增可达性状态表。
- [ ] devlog 增 D10（做了什么 / 坑·判断 / 当前门禁 / 仍未做）。

## Self-Review

- **Spec 覆盖**：§2 三件事→Task 2/3/4；§3.1 注入+门禁→Task 2；§3.2 收窄→Task 2/4；
  §3.3 映射表→Task 2；§3.4 工具沙箱→Task 1/3；§3.5 位置→Task 4；§4 网关→Task 5；
  §5 测试→各任务。无缺口。
- **占位符**：无。
- **类型一致**：`SkillFiles.read` / `SkillCatalog.pack_for_intent` / `make_read_skill_file_tool(files)`
  贯穿 Task 3/4/5；state 键 `skill` 与 `_route_event` 读取一致。
