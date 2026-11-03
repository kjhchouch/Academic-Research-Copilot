# Skill Loader（D10）：让 7 个 vendored skill 可达 · 设计

- 日期：2026-11-02（D10）
- 状态：已实现（自治模式按本文件落地；关键决策附被否掉的备选）
- 上游承诺：D8 devlog「skill loader（step ②/③）：intent → skill 映射表 + 把 nature-writing
  的 always_load 注入到 section 级边界条件之后 + 沙箱化的 read_skill_file 工具。设计已交付，
  等放行」——那份「设计」只存在于对话里，本文件是它第一次落盘，并补齐了实现期的修正。
- 非目标：不改任何 vendored 文件（NOTICE 的 verbatim 声明 + Apache-2.0 §4(b) 约束）；
  不扩意图词汇表（D4 明确「先只 6 个不要贪多」，`data_analysis` 等留 Week 3）；
  不做 skill 沙箱执行（vendored .py 本来就不进运行时，白名单门禁继续守着）；
  不做用户自装 skill（README 后续规划已明确需要先有沙箱与审计）。

## 1. 问题

7 个 vendored skill（996 KB→875 KB / 123+77 文件）处于 **vendored-but-unreachable** 状态：
仓库里存在、门禁看着、运行时碰不到。skill 是知识不是能力（README 的原话），但知识也得
有通道进到模型面前。D8 已把「让它安全的机制」建好（L0 guard 拒整篇/降重），本设计把
「让它可达的机制」补上——两件事合起来才是 D8 说的「上游（skill 内容）和下游（路由门）」。

## 2. 可达的三件事

1. **intent → skill 映射**（`route` 节点写 `state["skill"]`）：静态表，我们自己的代码，
   不是模型的选择。路由照旧只产 6 意图；映射把意图接到 skill。
2. **skill pack 注入**（`agent` 节点）：意图命中映射时，把 `SKILL.md` 全文 + 该 skill
   `manifest.yaml` 的 `always_load` 文件列表，包上我们的范围说明，作为一条
   `SystemMessage` 注入在主 system prompt 之后、历史之前。
3. **`read_skill_file` 工具**：on_demand 的渐进披露——SKILL.md/manifest 里的
   references/axes 指到几百个按需文件，模型用这个工具按需取。

## 3. 关键决策

### 3.1 always_load 注入，不靠模型自觉去读

**被否的备选**：只注入一段 brief（~300 字），让模型用 `read_skill_file` 自己读 SKILL.md
和 always_load。

理由：D8 明确警告过「目录一变深，失败发生在 **prompt 组装期**——不报错，只掉质量」。
「模型忘了调工具」是同一类静默失效：不崩、不报错、输出质量下降，等审稿人才发现。
skill 的价值恰恰在于 always_load 里那些「对每个 job 都适用」的 stance/workflow/ethics
（SKILL.md 自己说 *These hold the default stance, writing workflow, and output format
that apply to every drafting job*）——把它们交给模型的自觉，等于没有加载。

**成本账**（实测体积）：writing pack = 8.2K(SKILL.md) + 18.9K(7 个 always_load) ≈ 27.6K
chars；polishing ≈ 19.4K；ref-verifier ≈ 11.4K。首个映射轮多 1-2s prefill；**此后缓存
兜底**——pack 只随 intent 变化，固定插在 system prompt 之后，同一意图的多轮对话里
pack 字节不变、位置不变，prompt cache 从第二轮起命中。映射的意图（润色/起草/大纲/
引用）本就不是延迟敏感的窄任务。

**防膨胀门禁**：每张映射表内 skill 的 pack ≤ 40,000 chars，测试钉死——将来 re-vendor
把 always_load 撑大，latency/cost 回归会在 CI 上红，而不是在用户侧悄悄变慢。

### 3.2 nature-writing 的收窄写在我们的包装里，并声明优先级

skill 内容是上游的，范围边界是我们的。注入的 pack 头部带一段**我们的文本**：

> 【任务范围（本项目限定，优先于下文 skill 内容）】本次是章节级起草辅助：只在用户已经
> 提供论点、结果或数据的前提下，协助起草或重构指定章节；不生成整篇论文或无依据的整章
> 正文。缺论点/数据时先向用户要，不要编造。

D8 的三件事对齐：**L0 拒整篇/降重**（已存在，guard 是图边）；**路由收窄**（intent 是
`draft_section` 不是整篇）；**本包装**（把「辅助不代写」翻译成 skill 语境里的正向契约）。
声明「优先于下文」是因为 pack 里是外语境的指令，必须显式排序，不能指望模型自己仲裁。

### 3.3 映射表 v1：四个意图可达，三个 skill 刻意不可达

| intent | skill | 备注 |
|---|---|---|
| `polish` | nature-polishing | 上游 Stable，零可执行代码 |
| `draft_section` | nature-writing | 带收窄包装（§3.2） |
| `outline` | nature-writing | article-architecture 在它的 on_demand 里 |
| `citation_format` | nature-ref-verifier | 格式化仍走 MCP 的确定性工具；verifier 补「对不对」的启发式 |
| `literature_search` | — | 检索走 MCP 工具；`nature-academic-search` 被刻意排除（PROVENANCE） |
| `unclear` / `knowledge_qa` | — | guard 出口 / 资料问答有自己的二阶段审核 |

**刻意不可达**：`nature-response`（审稿回复——词汇表没有对应意图）、`nature-statistics`
（D4 已记：等 `data_analysis` 进词汇表，Week 3）、`nature-social-science`（选题构思——
同前）、`nature-shared`（其自述「Do not invoke it as a standalone user workflow」）。
不可达不是浪费：它们的内容仍受门禁看守，词汇表一扩，映射表加一行就可达——**增长路径
是表里加一行，不是新机制**。

### 3.4 read_skill_file：永不 raise，错误文本是恢复路径

两个硬约束，各有一段已付过的学费：

- **永不 raise**。D6 的教训：`ToolNode._default_handle_tool_errors` 只吸收特定异常类型，
  其余一律重抛——一个会抛的工具能掀翻整条 SSE run。本工具所有失败模式都返回**错误文本
  字符串**，文本里给出路（D5 的教训：「错误文本是功能需求而不是润色」）。
- **沙箱解析**：路径必须相对 `skills/third_party`（如 `nature-writing/SKILL.md`、
  `nature-writing/../nature-shared/core/ethics.md`）；拒绝绝对路径、反斜杠、含 `..` 的
  段落、`CON`/`NUL` 等 Win32 保留名（D1 的 `_safe_name` 同款）；解析后必须仍在
  vendor root 内；扩展名白名单 `{.md, .yaml, .tex}`——`.py` 明确拒绝（知识工具不读代码，
  白名单门禁守的是「不被执行」，这里守的是「不被读取」）；单文件 cap 100K chars。

### 3.5 缓存与位置的账

pack 作为第 2 条 SystemMessage 插在主 system prompt 之后：同一意图的多轮对话，pack
字节与位置都不变，缓存从历史部分继续命中；意图在对话中切换时，前缀从 pack 处分叉——
这是正确行为（skill 确实变了），代价是那一次 miss。**不**并进 `FAST_PATH_SYSTEM_PROMPT`
：那会让无 skill 轮次也背上变动前缀，且违反该文件的「不变的才进来」纪律。

## 4. 结构与数据流

```
paper_agent/skill_loader/
  __init__.py     导出 + 「可达三件事」论证
  catalog.py      INTENT_SKILLS 映射表 + SkillSpec + pack 组装（含收窄包装）+ 体积门禁常量
  files.py        沙箱磁盘访问：resolve/validate/read，SkillFileError（带出路的错误文本）
paper_agent/tools/skill_files.py   make_read_skill_file_tool(catalog) —— 工厂，永不 raise
```

- **main.py**：`_build_skill_catalog(settings)`（disabled 或 root 缺失 → None，日志 +
  `/health` 可见）；catalog 非 None 时把 `read_skill_file` 工具加进 tools（在
  `assert_unique_tool_names` 之前）；`build_graph(..., skill_catalog=catalog)`。
- **builder.py**：`skill_catalog` 参数穿过到 `make_route_node` / `make_agent_node`。
- **route 节点**：写 `state["skill"]`（skill 名或 None）。知识问答路径恒 None。
- **agent 节点**：非知识路径读 `state.get("intent")` → `catalog.pack_for_intent` →
  消息列表 = [system] + [skill pack]? + history。
- **state**：`skill: NotRequired[str | None]`（跟随 D8/D9 的七字段先例）。
- **SSE**：`route` 事件增 `skill` 字段（additive，transport 忽略未知字段；D8 同款「后端
  先行」）。`/health` 增 `skills: {enabled, reachable}`。
- **配置**：`skills_enabled: bool = True`、`skills_dir: str = "skills/third_party"`。

**裸环（无 router）无 skill**：可达性以路由意图为前提——零配置裸启动没有意图，也就没有
映射。这是特性：skill 注入是路由决策的下游，不是独立的第二条路由。

## 5. 测试策略（全部离线）

- `test_skill_loader.py`：映射键 ⊆ INTENTS 交叉测试（INTENTS/eval 双清单同款纪律）；
  pack 确定性（两次构建逐字节相等）；writing pack 含收窄文本与 SKILL.md 正文；
  **真实 vendored 树上的体积门禁**（每个映射内 skill ≤ 40K chars）；disabled/缺 root →
  全部 None。
- `test_skill_files.py`（files.py）：真实树上读 `nature-polishing/SKILL.md` 成功；
  拒绝——绝对路径、反斜杠、`..`、保留设备名、`.py`、缺文件；每个错误文本都含出路。
  tmp fixture 上测 size cap。
- `test_tools_skill_file.py`：工具层——合法路径返回正文；所有失败模式返回**字符串**
  （`pytest.raises` 反向验证：一个都不许抛）。
- `test_graph_skills.py`：真编译图——ScriptedRouter(polish) → `state["skill"]` 已写、
  捕获型假模型断言 pack 出现在消息列表第 2 条、无 catalog 时无 pack、知识路径无 pack、
  draft_section 的 pack 含收窄文本、裸环无 skill。
- API：route 事件的 `skill` 字段、`/health` 的 skills 块。
- 既有 `test_vendored_skills.py` 17 条门禁必须原样全绿（vendored 树零改动）。

## 6. 被否掉的备选

- **只注入 brief，正文靠模型自己读**（§3.1）：静默失效类风险，否。
- **把 pack 并进 system prompt**：破坏「不变的才进 system prompt」纪律，无 skill 轮次
  背锅，否。
- **现在就扩 `data_analysis` / `review_response` 意图让 statistics/response 可达**：
  D4 已有明确决策（词汇表扩张要连 eval 语料一起做），否；路径是映射表加一行。
- **解析 manifest 的 axes/on_demand 自动选 fragment**：那是把上游的 router 搬进我们运行时
  ——README 说「永不 vendor 第二个 router」，模型的 on_demand 选择 + read_skill_file 才是
  与 skill 设计一致的方式，否。
- **给 skill 内容跑注入检测**：vendored 内容经逐行审查、进版本库、由 PR 门禁——它不是
  「检索回来的不可信输入」，套同一套管线是错分类。边界写在 files.py docstring。

## 7. 上线边界（如实声明）

- 映射表是我们对「哪个意图需要哪类知识」的判断，不是上游背书的路由；调整 = 改一行 +
  devlog 记原因。
- pack 注入改变映射意图轮次的成本模型（首轮 prefill +1-2s）；`skills_enabled=false`
  是整体退路。
- 上游 skill 是 Draft 级的两家（statistics / social-science）本来就不可达，本设计不改变
  它们的成熟度。
