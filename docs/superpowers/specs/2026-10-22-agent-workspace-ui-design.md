# 主流 Agent 工作区 UI 改造 · 设计文档

日期：2026-10-22
状态：已确认，待实现
范围：**A — 纯前端，零后端改动**

## 1. 背景与目标

当前前端只有一个 `apps/web/src/components/ChatPanel.tsx`（459 行），把 header、假的
面板切换条、消息列表、输入框、右侧参考文献面板全塞在一个文件里。它能跑通「流式回复 +
引用 chip + 参考文献」，但外观和交互不像主流 Agent 产品（ChatGPT / Claude / Cursor 那
种「左侧导航 + 中间对话 + 右侧工作区」的分栏结构），也没有把已经产生但从未被渲染的
数据暴露出来。

目标：把界面改造成**图标导航栏 + 中间对话列 + 右侧可切换工作区面板**的三栏布局，让
它看起来、用起来像主流 Agent 应用；同时把后端**已经在发**但前端**还没渲染**的两类
SSE 数据接上——`route`（D8 刚加的路由/guard 决策事件）和 `tool`（活动日志），做到
「有多少真数据，就展示多少真面板」。

非目标（见 §9）：不做编辑器、不做多会话历史、不做拖拽调整分栏宽度。

## 2. 范围界定：为什么是「纯前端」

一个面板要么有真实的 SSE 数据源，要么就不该假装存在。按这个标准盘点：

| 面板 | 数据源 | 结论 |
| --- | --- | --- |
| 对话 Chat | `token` 事件 → 文本 part | ✅ 已有 |
| 文献 References | `citation` 事件 → `data-citation` part | ✅ 已有（D6） |
| 路由 Route | `route` 事件（D8，前端从未渲染） | ✅ 本次接上 |
| 活动 Activity | `tool` 事件 → `dynamic-tool` part | ✅ 已有，本次汇总成日志 |
| 大纲 Outline | 无文档模型；只有 `ui_context.paper_stage/cursor_section` | ⚠️ 明确标注的 Week-2 占位 |
| 编辑器 Editor | 无文档模型（后端不存正文） | ❌ 跳过 |
| 对话历史 Threads | 无 list-threads API | ❌ 跳过 |

所以本次只动 `apps/web/`，后端一行不改。`route` 事件在 D8 已经由
`apps/api/src/api/routes/chat.py:_route_event` 发出，wire shape 稳定（每个 key 都在、
未知时为 null），前端只是第一次消费它。

## 3. 布局 B：三栏结构

```
┌──┬───────────────────────────────┬──────────────┐
│  │  header（标题 · thread · 状态 · API badge）    │
│图│───────────────────────────────│  工作区面板   │
│标│                               │  （按导航栏   │
│导│        中间：对话列            │   选中项切换） │
│航│   消息列表 + 输入框            │              │
│栏│                               │  文献/路由/   │
│  │                               │  活动/大纲    │
└──┴───────────────────────────────┴──────────────┘
w-14            flex-1 min-w-0          w-96 shrink-0
```

- **图标导航栏（IconRail）**：最左，固定宽 `w-14`（56px）。竖排图标按钮，选中项高亮。
  顶部一项是「对话」——选中它时**右侧工作区收起**，对话列占满（对应主流 App 的
  「关掉侧栏」）。其余项切换右侧面板。
- **对话列（ChatColumn）**：中间，`flex-1 min-w-0`。保留现有 header、消息列表、输入框、
  错误条。这是当前 `ChatPanel` 的主体。
- **工作区面板（WorkspacePanel）**：右侧，`w-96`（384px）`shrink-0`。按导航栏选中项
  渲染 文献/路由/活动/大纲 之一。选「对话」时整块不渲染。

导航栏项（`PanelId`）：`chat | references | route | activity | outline`。

## 4. 组件分解

把 459 行的单体拆成职责单一、可独立理解的文件。沿用现有代码风格（暗色 zinc 主题、
中文注释解释「为什么」）：

```
apps/web/src/
  app/
    page.tsx              # 改：保留 <Suspense>，渲染 <Workspace/>（骨架同步改）
    layout.tsx            # 不动
    globals.css           # 不动（暗色 zinc 主题保留）
  lib/
    api.ts                # 改：加 RouteDecision 类型 + intent/guard/source 标签表
    sseTransport.ts       # 改：加 case "route": → data-route part
  components/
    Workspace.tsx         # 新：三栏外壳，持有 useChat / uiContext / activePanel
    IconRail.tsx          # 新：左侧图标导航栏
    ChatColumn.tsx        # 新：中间对话列（原 ChatPanel 主体）
    MessageBubble.tsx     # 新：单条消息，渲染 text/tool/citation/route part
    ChatChips.tsx         # 新：ToolChip / CitationChip / RouteChip / textWithChips
    chatParts.ts          # 新：纯函数 citationsOf/byId/citedIds/citationMeta/summarize/routeOf
    BackendBadge.tsx      # 新：从 ChatPanel 抽出
    panels/
      PanelHost.tsx       # 新：按 activePanel 切换右侧面板
      ReferencesPanel.tsx # 抽出（现有）
      RoutePanel.tsx      # 新
      ActivityPanel.tsx   # 新
      OutlinePanel.tsx    # 新（Week-2 占位）
```

`ChatPanel.tsx` 删除，其内容分散到上述文件。

**状态归属**：`useChat`、`uiContext`、`activePanel` 三份状态都放在 `Workspace.tsx`
（唯一持有者），向下传递：

- `messages`（来自 useChat）→ `ChatColumn` 和 `PanelHost`（各面板都从 messages 派生数据）。
- `uiContext` → `ChatColumn.onSubmit`（sendMessage body）和 `OutlinePanel`（只读预览）。
- `activePanel` → `IconRail`（高亮）和 `PanelHost`（切换）。

所有面板数据都是 `messages` 的**派生值**，不新增独立状态，避免同步问题。

## 5. 数据流：新增 `data-route` part

镜像现有 `citation` → `data-citation` 的做法。

**① 传输层**（`sseTransport.ts`）：现在 `route` 落在被忽略的 `default` 分支。新增：

```ts
case "route": {
  const route = JSON.parse(parsed.data) as RouteDecision;
  if (!route || typeof route !== "object") break;
  // 每轮对话恰好一个 route 决策（在首个 token 之前），所以用固定 id "route"：
  // 重发时替换而非重复。与 citation 的 per-id key 不同——citation 一轮多个，route 一轮一个。
  controller.enqueue({ type: "data-route", id: "route", data: route });
  break;
}
```

`data-route` 与 `data-citation` 一样是 AI SDK v7 的自定义 data part（`data-${string}`），
机制已被现有 citation 验证可用。

**② 类型**（`api.ts`）：wire mirror，每个字段可空，与后端 `_route_event` 逐字段对齐：

```ts
export type RouteDecision = {
  intent: string | null;                 // literature_search | outline | draft_section | polish | citation_format | unclear
  guard: "proceed" | "clarify" | "refuse" | null;
  needs_clarify: boolean | null;
  path_deep: boolean | null;
  source: string | null;                 // jev | small_llm | static
  confidence: number | null;
  from_cache: boolean | null;
};
```

`guard` 用联合类型（后端 `Guard = Literal["proceed","clarify","refuse"]` 钉死了这三个值，
测试也逐字断言），但 UI 的颜色映射带**中性兜底分支**，这样后端将来若加第四个值也不会崩
——与 `TOOL_LABELS` 的「已知映射 + 原样回退」同一套防御姿态。

**③ 读取**（`chatParts.ts`）：

```ts
export function routeOf(message: UIMessage): RouteDecision | null {
  const part = message.parts.find((p) => p.type === "data-route");
  return part ? (part as unknown as { data: RouteDecision }).data : null;
}
```

（沿用现有 `data-citation` 的 cast 读法，不引入 useChat 泛型改造。）

**④ 渲染**：
- `MessageBubble` 用 `routeOf(message)` 在气泡顶部渲染 `RouteChip`（见 §7）。
- `RoutePanel` 遍历所有 messages 收集 route 决策，按轮次列出。

## 6. `ui_context.active_panel` 的处理（已标记的数据流风险）

现状：旧代码有个**假**面板条，点按钮只改 `uiContext.active_panel`（发给后端做路由提示），
并不切换任何视图。词汇表是 `["chat","editor","outline","references"]`，后端
`chat.py:92 active_panel: str | None = None` 原样接收、经 `RouteInput.as_state()` 透传给
路由后端。

新设计里导航栏**真的**切换视图，同时保留它作为路由提示的价值。但导航栏有
`route`/`activity` 两个**诊断面板**，它们不是「编辑器面板」，发给后端语义不通。

**决策**：导航栏选中项 → 显示哪个面板（纯前端）；`active_panel` 单独用一张映射表收敛到
后端已知的编辑器词汇，诊断面板映射到中性的 `"chat"`：

```ts
const PANEL_TO_UI_CONTEXT: Record<PanelId, string> = {
  chat: "chat",
  references: "references",
  outline: "outline",
  route: "chat",      // 诊断面板，无编辑器语义
  activity: "chat",   // 诊断面板，无编辑器语义
};
```

这样：后端永远只收到它认识的 4 个值之一（契约不破，**零后端改动**，范围 A 成立）；
导航栏对 chat/references/outline 仍是**有路由意义**的信号（「用户注意力在哪」）；不会
把 `route`/`activity` 这种后端没见过的值发出去。切换 activePanel 时同步更新
`uiContext.active_panel`。

## 7. 各面板内容

**对话 Chat（activePanel="chat"）**：右侧面板收起，对话列占满。这就是主流 App 的
「专注对话」态。

**文献 References**：抽出现有 `ReferencesPanel`，行为不变（cited-only、DOI 链接、
「本次回答 N 条」）。

**路由 Route（新）**：遍历 messages 收集每轮的 `RouteDecision`，**最新在上**，每条一张
紧凑卡片：
- `intent` → 中文标签（`INTENT_LABELS`：文献检索/提纲/起草小节/润色/引用格式/意图不明）。
  `intent` 类型是 `string | null`（比联合宽，防后端增值），故标签表带兜底：未知值原样显示、
  null 显示「—」。与 guard 同一套「已知映射 + 中性回退」姿态。
- `guard` → 彩色徽章（proceed=翠绿「放行」/ clarify=琥珀「需澄清」/ refuse=红「已拒绝」）
- `source` → 哪个后端决定的（jev=类型化决策模型 / small_llm=小模型 / static=静态兜底）
- `confidence` → 百分比 + 细进度条
- `from_cache` → true 时加「缓存」标签
- `needs_clarify` / `path_deep` → 小旗标
空态：解释「路由决策会在你提问后出现在这里」。**这是 D8 route 事件第一次被可视化**，
面试时能直接指着它讲「路由即节点 + L0 guard 覆盖模型」的故事。

**活动 Activity（新）**：遍历 messages 收集所有 `dynamic-tool` part，**最新在上**，每行：
工具中文名（`TOOL_LABELS`）、状态图标（●运行中 / ✓完成 / ✕出错）、args/output 摘要
（复用 `summarize`）。相当于一个工具调用控制台。空态：「还没有工具调用」。

**大纲 Outline（新，Week-2 占位）**：顶部一条醒目横幅「大纲功能将在 Week 2 接入文档模型」。
下面把 Workspace 持有的 `uiContext.paper_stage / cursor_section` 作为**只读预览**展示
（这俩是将来驱动大纲的真实信号），再配一棵 ghosted/禁用的静态大纲树（摘要/引言/方法/
结果/讨论）示意形态。诚实标注占位，不假装有文档数据。

**RouteChip（气泡内联，新）**：`MessageBubble` 顶部按 `routeOf(message)` 渲染：
- `guard="refuse"` → 红 chip「已拒绝 · 超出边界」
- `guard="clarify"` → 琥珀 chip「需要澄清」
- `guard="proceed"` → 低调 chip 显示 intent 中文标签（可点，跳到路由面板）
让 guard 的结果出现在拒绝/澄清文案**旁边**，用户一眼看到「为什么是这个回答」。

## 8. 响应式与错误处理

**响应式**：
- `lg` 及以上：导航栏 + 对话列 + 工作区面板（三栏）。
- `lg` 以下：导航栏和工作区面板都 `hidden`，只剩对话列（≈今天的单栏）。`[n]` chip 的
  悬浮标题/摘要仍在，信息不丢，只是少了常驻面板。这与现有 `ReferencesPanel` 的
  `hidden lg:flex` 一致。（移动端抽屉式导航列为 Week-2 增强，不在本次范围。）

**错误处理**：
- 保留 useChat 的 `error` 条和 `stop`/`clearError`。
- `data-route`/`data-citation` 解析失败时 `break`（不 enqueue），与现有 citation 的
  `typeof citation?.citation_id !== "number"` 防御一致——脏事件不能让流崩。
- 后端未配路由（`router=None`）时**不发** route 事件 → RoutePanel 空态，RouteChip 不渲染。
  这是正常降级，不是 bug。
- `page.tsx` 的 `<Suspense>` 边界**必须保留**：`cacheComponents: true` 下 Next 16 会在
  构建期预渲染 Client Component，而 `useChat` 用 `Math.random()` 铸 id 会让预渲染硬失败。
  骨架屏同步改成三栏轮廓。

## 9. 明确不做（YAGNI）

- **编辑器 Editor**：后端无文档模型，做了就是空壳。
- **对话历史 Threads**：无 list-threads API，无法列真实会话。
- **拖拽调整分栏宽度**：「分工作区」= 可切换的多面板（导航栏选），不含 drag-resize
  splitter（额外 pointer 事件 + 宽度状态，无数据价值）。若确认要，另开。
- **移动端抽屉导航**：`lg` 以下先退化成单栏对话。
- **useChat 泛型化**：沿用现有 cast 读 data part，不做大改造。

## 10. 测试与验收

`apps/web` 是**独立 pnpm 项目**（无根 workspace、无 `--filter`；脚本只有 `dev/build/lint`，
**没有 typecheck 脚本**），且**没有单测运行器**（无 vitest/playwright/jest）。所有命令在
`apps/web` 目录下跑，门禁 = 类型检查 + lint + 构建 + 手动浏览器实测：

1. `pnpm exec tsc --noEmit` 干净（`RouteDecision` 类型对齐、data-route cast 通过）。
2. `pnpm exec eslint . --max-warnings=0` 干净（等价于 `pnpm lint` 加严格零告警；注意 React 19
   的 `react-hooks/refs`、`react-hooks/set-state-in-effect` 规则——per-request 数据走
   sendMessage body，thread id 走 useSyncExternalStore，都已是现有写法）。
3. `pnpm build` 成功（= `next build`；`cacheComponents` 下 `<Suspense>` 保住预渲染）。
4. **浏览器实测**（后端起来后）：
   - 三栏布局渲染正确，导航栏切换面板。
   - 发「找 5 篇 contrastive learning 论文」→ 活动面板出现 search_papers 行、文献面板出现
     cited 条目、路由面板出现一条 `intent=literature_search / guard=proceed` 决策。
   - 发「帮我把这段降重」→ 路由面板 `guard=refuse`、气泡内联红 RouteChip、回复是拒绝文案。
   - 发「在吗」→ `guard=clarify`、琥珀 chip。
   - `lg` 以下缩成单栏；`[n]` chip 悬浮仍出标题。
   - 大纲面板显示 Week-2 占位横幅。

（无单测运行器，故不写组件测试；正确性靠类型 + 构建 + 上述手动清单。若后续要加
playwright，另开任务。）

## 11. 面试叙事价值

- **路由面板**：D8 的 route/guard 决策第一次可视化，能直接演示「路由即图节点」「L0 guard
  覆盖模型泄漏（降重被判 polish 仍被 refuse）」「熔断降级时 source=static」——把后端抽象
  变成看得见的产品面。
- **活动面板**：工具调用透明化，坐实「辅助不代写、数字来自可核实的工具」。
- **文献面板 + chip**：引用可核实（D6 已有），三栏让它更像真产品。
- **架构**：单体拆成职责单一的组件、data part 派生状态、ui_context 驱动路由——都是
  Agent 应用开发岗想听的工程判断。
