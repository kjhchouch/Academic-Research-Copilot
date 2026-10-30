# 主流 Agent 工作区 UI 改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把单文件前端改造成「图标导航栏 + 中间对话列 + 右侧可切换工作区面板」的三栏主流 Agent 布局，并首次渲染后端已在发的 `route` 决策与 `tool` 活动日志。

**Architecture:** 纯前端改造（范围 A，后端零改动）。把 459 行的 `ChatPanel.tsx` 拆成职责单一的组件；`Workspace.tsx` 独占 `useChat`/`uiContext`/`activePanel` 三份状态，所有面板数据都是 `messages` 的派生值。新增 `route` SSE 事件 → `data-route` part 的传输映射（镜像现有 `citation`→`data-citation`）。

**Tech Stack:** Next.js 16.4（`cacheComponents: true`）、React 19.3、Vercel AI SDK v7（`ai@7` + `@ai-sdk/react@4`）、Tailwind v4、TypeScript 5 strict、pnpm 10（`apps/web` 为独立项目，非 workspace）。

> **提交授权门（覆盖本计划的 commit 步骤）：** 用户的长期规则是「未经明文授权绝不 git 提交；选择题答复不算授权」。本计划按惯例写出 commit 检查点，但**执行时每到 commit 检查点必须暂停并请求明文授权**，不得自行提交。当前工作区已有未提交改动（`docs/devlog.md` D8 条目、`.gitignore` 的 `.superpowers/`、本 spec 与本 plan），一并在授权后处理。

> **验证方式说明：** `apps/web` **无单测运行器**（无 vitest/playwright/jest）。因此本计划不写 test-first 步骤；每个任务的验证 = `pnpm exec tsc --noEmit` + `pnpm exec eslint .`，集成里程碑加 `pnpm build`，最后用 §Task 12 的浏览器清单实测。所有命令在 `E:/agent/apps/web` 下运行。

> **不使用 emoji：** 导航栏图标用内联 SVG（无图标库依赖），不用 emoji 字符。

---

## File Structure

锁定分解决策（在 spec §4 基础上补两个 `lib/` 常量模块，把魔法字符串集中）：

```
apps/web/src/
  app/
    page.tsx                 # 改：保留 <Suspense>，渲染 <Workspace/>；骨架改三栏轮廓
    layout.tsx               # 不动
    globals.css              # 不动
  lib/
    api.ts                   # 改：+ RouteDecision 类型（wire mirror）
    sseTransport.ts          # 改：+ case "route": → data-route part
    labels.ts                # 新：TOOL_LABELS / INTENT_LABELS / GUARD_META / SOURCE_LABELS
    panels.ts                # 新：PanelId / PANELS / PANEL_META / PANEL_TO_UI_CONTEXT
  components/
    Workspace.tsx            # 新：三栏外壳，持有 useChat / uiContext / activePanel
    IconRail.tsx             # 新：左侧图标导航栏（内联 SVG 图标）
    ChatColumn.tsx           # 新：中间对话列（header + 消息 + 输入 + 错误条）
    MessageBubble.tsx        # 新：单条消息，渲染 text/tool/citation/route part
    ChatChips.tsx            # 新：ToolChip / CitationChip / RouteChip / textWithChips
    chatParts.ts             # 新：纯函数（从 message 派生数据）
    BackendBadge.tsx         # 新：从 ChatPanel 抽出
    panels/
      PanelHost.tsx          # 新：按 activePanel 切换右侧面板
      ReferencesPanel.tsx    # 抽出（现有 ChatPanel:393-446）
      RoutePanel.tsx         # 新
      ActivityPanel.tsx      # 新
      OutlinePanel.tsx       # 新（Week-2 占位）
    ChatPanel.tsx            # 删除（Task 11）
```

**依赖顺序**：Task 1→11 严格自底向上，每个文件只依赖更早创建的文件。`ChatPanel.tsx` 与 `page.tsx` 对它的 import **保持不动直到 Task 11**，所以 Task 1–10 期间应用始终能编译（新文件是纯增量）。Task 11 一次性切换 `page.tsx` 到 `Workspace` 并删除 `ChatPanel.tsx`。

**verbatim-move 约定**：标注「move verbatim from `ChatPanel.tsx:A-B`」的步骤＝把那段已存在且正确的代码原样搬到新文件（改 import 路径即可），不重新发明。这不是占位符——源码已在仓库中。

---

### Task 1: `lib/api.ts` 加 RouteDecision 类型

**Files:**
- Modify: `apps/web/src/lib/api.ts`（在 `Citation` 类型之后、`doiUrl` 之前插入）

- [ ] **Step 1: 加 RouteDecision 类型**

在 `api.ts` 的 `Citation` 类型定义之后插入：

```ts
/**
 * One turn's routing decision, and the wire mirror of the gateway's `route`
 * event (apps/api/src/api/routes/chat.py:_route_event). Every key is present and
 * null when the router did not supply it — the same rule as `Citation`.
 *
 * `guard` is the load-bearing field: "refuse"/"clarify" mean the agent never ran
 * and the only text is the fixed boundary/clarification message; "proceed" means
 * the ReAct loop ran. Typed as a union because the backend pins it to
 * Literal["proceed","clarify","refuse"], but every consumer maps it with a
 * neutral fallback so a future 4th value degrades instead of crashing.
 *
 * `intent`/`source` stay `string` (wider than the backend Literal) on purpose:
 * they are display-only, and a narrowed type here would be a lie the moment the
 * backend adds a value. `intent` ∈ literature_search | outline | draft_section |
 * polish | citation_format | unclear; `source` ∈ jev | small_llm | static.
 */
export type RouteDecision = {
  intent: string | null;
  guard: "proceed" | "clarify" | "refuse" | null;
  needs_clarify: boolean | null;
  path_deep: boolean | null;
  source: string | null;
  confidence: number | null;
  from_cache: boolean | null;
};
```

- [ ] **Step 2: 类型检查**

Run: `cd E:/agent/apps/web && pnpm exec tsc --noEmit`
Expected: 无输出（PASS）。纯类型新增，不影响任何现有代码。

- [ ] **Step 3: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/lib/api.ts
git commit -m "feat(web): RouteDecision 类型（route SSE 事件的 wire mirror）"
```

---

### Task 2: `lib/sseTransport.ts` 接 route 事件

**Files:**
- Modify: `apps/web/src/lib/sseTransport.ts`（`case "citation"` 之后、`case "done"` 之前新增 `case "route"`；并在顶部 import 加 `RouteDecision`）

- [ ] **Step 1: import RouteDecision**

把现有 import 行
```ts
import type { Citation } from "@/lib/api";
```
改成
```ts
import type { Citation, RouteDecision } from "@/lib/api";
```

- [ ] **Step 2: 新增 case "route"**

在 `switch (parsed.event)` 里，`case "citation": { ... }` 块之后、`case "done":` 之前插入：

```ts
                case "route": {
                  // The gateway sends one `route` decision per turn, before any
                  // token. Carried as a `data-route` part (mirroring citation) so
                  // the RoutePanel and the inline RouteChip read it as data, not
                  // prose. Fixed id "route": one decision per turn means a re-send
                  // replaces rather than duplicates — unlike citation, which keys
                  // per citation_id because a turn carries many.
                  const route = JSON.parse(parsed.data) as RouteDecision;
                  if (!route || typeof route !== "object") break;
                  controller.enqueue({
                    type: "data-route",
                    id: "route",
                    data: route,
                  });
                  break;
                }
```

- [ ] **Step 3: 更新 default 分支注释**

现有 default 分支注释是 `// D4 adds `route`. Ignoring unknown names ...`。`route` 现已处理，改成：

```ts
                default:
                  // Unknown event names are ignored so an older frontend survives
                  // a newer gateway. `route` and `citation` are handled above.
                  break;
```

- [ ] **Step 4: 类型检查 + lint**

Run: `cd E:/agent/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint src/lib/sseTransport.ts`
Expected: 无输出（PASS）。`data-route` 与既有 `data-citation` 同属 AI SDK v7 的 `data-${string}` part，类型可编译（现有 citation 已验证此机制）。

- [ ] **Step 5: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/lib/sseTransport.ts
git commit -m "feat(web): sseTransport 把 route 事件映射为 data-route part"
```

---

### Task 3: `lib/labels.ts` 显示词汇表

**Files:**
- Create: `apps/web/src/lib/labels.ts`

- [ ] **Step 1: 写 labels.ts**

```ts
/**
 * Static display vocabulary: the Chinese labels and Tailwind class bundles the
 * panels and chips use. Kept out of components so no magic strings are scattered
 * across files, and out of api.ts because these are presentation, not wire
 * contract. Every map has a documented fallback for unknown keys — the backend
 * vocabularies (Intent/Guard/RouteSource) can grow, and the UI must degrade to a
 * neutral rendering rather than crash or show `undefined`.
 */

/** Gateway built-in tool → display name. Unknown tools fall back to the raw
 *  name at the call site, so a new backend tool shows up without a redeploy. */
export const TOOL_LABELS: Record<string, string> = {
  count_words: "统计字数",
  search_papers: "检索文献",
  get_paper: "读取论文",
  format_citation: "生成引用",
};

/** Routing intent → display name. `intent` is `string | null` on the wire; the
 *  call site falls back to the raw string, then "—" for null. */
export const INTENT_LABELS: Record<string, string> = {
  literature_search: "文献检索",
  outline: "提纲",
  draft_section: "起草小节",
  polish: "润色",
  citation_format: "引用格式",
  unclear: "意图不明",
};

export type GuardStyle = { label: string; chip: string; dot: string };

/** guard → { 中文名, chip 类, 状态点类 }. Mirrors the backend
 *  Literal["proceed","clarify","refuse"]. */
export const GUARD_META: Record<string, GuardStyle> = {
  proceed: {
    label: "放行",
    chip: "border-emerald-500/30 bg-emerald-500/15 text-emerald-300",
    dot: "text-emerald-400",
  },
  clarify: {
    label: "需澄清",
    chip: "border-amber-500/30 bg-amber-500/15 text-amber-300",
    dot: "text-amber-400",
  },
  refuse: {
    label: "已拒绝",
    chip: "border-red-500/30 bg-red-500/15 text-red-300",
    dot: "text-red-400",
  },
};

/** Neutral rendering for a guard value not in GUARD_META (future backend value). */
export const GUARD_FALLBACK: GuardStyle = {
  label: "未知",
  chip: "border-zinc-600 bg-zinc-800 text-zinc-300",
  dot: "text-zinc-400",
};

/** Which routing backend decided. Unknown → raw string at the call site. */
export const SOURCE_LABELS: Record<string, string> = {
  jev: "类型化决策模型 (Jev)",
  small_llm: "小模型降级",
  static: "静态兜底",
};

/** Look up a guard's style with the neutral fallback baked in. */
export function guardStyle(guard: string | null | undefined): GuardStyle {
  return (guard && GUARD_META[guard]) || GUARD_FALLBACK;
}
```

- [ ] **Step 2: 类型检查 + lint**

Run: `cd E:/agent/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint src/lib/labels.ts`
Expected: 无输出（PASS）。

- [ ] **Step 3: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/lib/labels.ts
git commit -m "feat(web): labels.ts 显示词汇表（tool/intent/guard/source + 兜底）"
```

---

### Task 4: `lib/panels.ts` 面板配置

**Files:**
- Create: `apps/web/src/lib/panels.ts`

- [ ] **Step 1: 写 panels.ts**

```ts
/**
 * The workspace-panel registry: which panels exist, their rail order, display
 * metadata, and the mapping from a displayed panel to the `ui_context.active_panel`
 * value sent to the backend.
 *
 * The mapping is the load-bearing part. The backend understands an editor-panel
 * vocabulary (chat/editor/outline/references) and routes on it. Two rail panels —
 * route and activity — are diagnostics with no editor meaning, so they map to the
 * neutral "chat" rather than leaking an unknown value to the backend. This keeps
 * scope A honest (zero backend changes, only values the gateway already accepts)
 * while still making the rail a real routing signal for chat/references/outline.
 */

export type PanelId = "chat" | "references" | "route" | "activity" | "outline";

/** Rail order, top to bottom. "chat" collapses the right workspace panel so the
 *  conversation takes full width — the mainstream "focus the chat" state. */
export const PANELS: readonly PanelId[] = [
  "chat",
  "references",
  "route",
  "activity",
  "outline",
];

export type PanelMeta = { name: string; hint: string };

/** Display name + tooltip per panel. The icon is an inline SVG in IconRail, not
 *  a glyph here (no emoji, no icon-library dependency). */
export const PANEL_META: Record<PanelId, PanelMeta> = {
  chat: { name: "对话", hint: "收起工作区，专注对话" },
  references: { name: "文献", hint: "本次回答引用的参考文献" },
  route: { name: "路由", hint: "每轮的路由 / guard 决策" },
  activity: { name: "活动", hint: "工具调用日志" },
  outline: { name: "大纲", hint: "Week 2 接入文档模型（占位）" },
};

/** Displayed panel → `ui_context.active_panel` value the backend understands. */
export const PANEL_TO_UI_CONTEXT: Record<PanelId, string> = {
  chat: "chat",
  references: "references",
  outline: "outline",
  route: "chat",
  activity: "chat",
};
```

- [ ] **Step 2: 类型检查 + lint**

Run: `cd E:/agent/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint src/lib/panels.ts`
Expected: 无输出（PASS）。

- [ ] **Step 3: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/lib/panels.ts
git commit -m "feat(web): panels.ts 面板注册表 + active_panel 映射（诊断面板收敛到 chat）"
```

---

### Task 5: `components/chatParts.ts` 纯派生函数

**Files:**
- Create: `apps/web/src/components/chatParts.ts`

- [ ] **Step 1: 写 chatParts.ts**

`citationsOf`/`byId`/`citedIds`/`citationMeta`/`summarize` 从 `ChatPanel.tsx` 逐字搬来（源行见注释），新增 `routeOf`/`routesOf`/`activityOf`/`activeCitations`：

```ts
import type { DynamicToolUIPart, UIMessage } from "ai";
import type { Citation, RouteDecision } from "@/lib/api";

/**
 * Pure derivations from the message list. No JSX, no state — every panel and
 * bubble reads its data through these so the logic lives in one testable place
 * and `Workspace` stays the only stateful component.
 */

// --- citations: moved verbatim from ChatPanel.tsx:36-72 ---

/** Citations on one message, keyed by id and in numeric order. Scoped per
 *  message because citation_id restarts at 1 every turn. */
export function citationsOf(message: UIMessage): Citation[] {
  return message.parts
    .filter((part) => part.type === "data-citation")
    .map((part) => (part as unknown as { data: Citation }).data)
    .filter((c) => c && typeof c.citation_id === "number")
    .sort((a, b) => a.citation_id - b.citation_id);
}

export function byId(citations: Citation[]): Map<number, Citation> {
  return new Map(citations.map((c) => [c.citation_id, c]));
}

/** Ids the answer actually references as [n] in its text. */
export function citedIds(message: UIMessage): Set<number> {
  const ids = new Set<number>();
  for (const part of message.parts) {
    if (part.type === "text") {
      for (const match of part.text.matchAll(/\[(\d+)\]/g)) ids.add(Number(match[1]));
    }
  }
  return ids;
}

/** One-line "authors · year · venue" for a chip tooltip or panel entry. */
export function citationMeta(c: Citation): string {
  const authors = c.authors.length
    ? `${c.authors.slice(0, 3).join(", ")}${c.authors.length > 3 ? " 等" : ""}`
    : "";
  return [authors, c.year, c.venue].filter(Boolean).join(" · ");
}

// moved verbatim from ChatPanel.tsx:74-78
export function summarize(output: unknown): string {
  if (output == null) return "";
  const text = typeof output === "string" ? output : JSON.stringify(output);
  return text.length > 60 ? `${text.slice(0, 60)}…` : text;
}

/**
 * The ReferencesPanel's data: the newest answer that cited something, filtered to
 * the [n] markers it actually uses. Moved from ChatPanel.tsx:226-238. A follow-up
 * turn with no literature does not blank the previous answer's references.
 */
export function activeCitations(messages: UIMessage[]): Citation[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const all = citationsOf(messages[i]);
    if (!all.length) continue;
    const cited = citedIds(messages[i]);
    return all.filter((c) => cited.has(c.citation_id));
  }
  return [];
}

// --- route (new) ---

/** The one route decision on a message, or null. `route` arrives once per turn,
 *  so `find` (not `filter`) is correct. Cast mirrors the data-citation read. */
export function routeOf(message: UIMessage): RouteDecision | null {
  const part = message.parts.find((p) => p.type === "data-route");
  return part ? (part as unknown as { data: RouteDecision }).data : null;
}

/** Every turn's route decision, newest first, for the RoutePanel. */
export function routesOf(messages: UIMessage[]): RouteDecision[] {
  const out: RouteDecision[] = [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const r = routeOf(messages[i]);
    if (r) out.push(r);
  }
  return out;
}

// --- activity (new) ---

/** One row in the activity log: the tool part plus the turn index it came from. */
export type ActivityRow = { part: DynamicToolUIPart; turn: number };

/** Every tool call across the conversation, newest first (last call of the last
 *  turn on top), for the ActivityPanel. */
export function activityOf(messages: UIMessage[]): ActivityRow[] {
  const out: ActivityRow[] = [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const tools = messages[i].parts.filter(
      (p): p is DynamicToolUIPart => p.type === "dynamic-tool",
    );
    for (let j = tools.length - 1; j >= 0; j--) out.push({ part: tools[j], turn: i });
  }
  return out;
}
```

- [ ] **Step 2: 类型检查 + lint**

Run: `cd E:/agent/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint src/components/chatParts.ts`
Expected: 无输出（PASS）。若 `p is DynamicToolUIPart` 的类型谓词报错，确认 `DynamicToolUIPart` 从 `"ai"` 导入（现有 ChatPanel.tsx:5 已如此导入）。

- [ ] **Step 3: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/components/chatParts.ts
git commit -m "feat(web): chatParts 纯派生函数（route/activity/citations 一处收敛）"
```

---

### Task 6: `components/BackendBadge.tsx` 抽出

**Files:**
- Create: `apps/web/src/components/BackendBadge.tsx`

- [ ] **Step 1: 写 BackendBadge.tsx**

move verbatim from `ChatPanel.tsx:448-458`，加 `"use client";` 与默认导出：

```tsx
"use client";

/** The /health probe result as a corner badge. Extracted verbatim from the old
 *  ChatPanel so the header logic is unchanged. */
export default function BackendBadge({ up }: { up: boolean | null }) {
  const [label, cls] =
    up === null
      ? ["checking", "bg-zinc-700 text-zinc-300"]
      : up
        ? ["api online", "bg-emerald-900/60 text-emerald-300"]
        : ["api offline", "bg-red-900/60 text-red-300"];
  return (
    <span className={`rounded px-2 py-1 font-mono text-[11px] ${cls}`}>{label}</span>
  );
}
```

- [ ] **Step 2: 类型检查 + lint**

Run: `cd E:/agent/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint src/components/BackendBadge.tsx`
Expected: 无输出（PASS）。

- [ ] **Step 3: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/components/BackendBadge.tsx
git commit -m "refactor(web): 抽出 BackendBadge"
```

---

### Task 7: `components/ChatChips.tsx`（ToolChip / CitationChip / RouteChip / textWithChips）

**Files:**
- Create: `apps/web/src/components/ChatChips.tsx`

- [ ] **Step 1: 写 ChatChips.tsx**

`ToolChip` move verbatim from `ChatPanel.tsx:88-99`（`TOOL_LABELS`/`summarize` 改为 import）；`CitationChip` move verbatim from `ChatPanel.tsx:111-140`（`citationMeta`/`doiUrl` 改 import）；`textWithChips` move verbatim from `ChatPanel.tsx:150-170`；`RouteChip` 为**新增**，完整代码如下：

```tsx
"use client";

import type { DynamicToolUIPart } from "ai";
import { doiUrl, type Citation, type RouteDecision } from "@/lib/api";
import { TOOL_LABELS, INTENT_LABELS, guardStyle } from "@/lib/labels";
import { citationMeta, summarize } from "@/components/chatParts";

// --- ToolChip: moved verbatim from ChatPanel.tsx:88-99 ---
export function ToolChip({ part }: { part: DynamicToolUIPart }) {
  const finished = part.state === "output-available" || part.state === "output-error";
  return (
    <div className="my-1.5 flex items-center gap-2 rounded border border-zinc-800 bg-zinc-950/60 px-2.5 py-1 font-mono text-[11px]">
      <span className={finished ? "text-emerald-400" : "animate-pulse text-zinc-300"}>
        {part.state === "output-error" ? "✕" : finished ? "✓" : "●"}
      </span>
      <span className="text-zinc-300">{TOOL_LABELS[part.toolName] ?? part.toolName}</span>
      {finished && <span className="truncate text-zinc-500">{summarize(part.output)}</span>}
    </div>
  );
}

// --- CitationChip: moved verbatim from ChatPanel.tsx:111-140 ---
export function CitationChip({ citation }: { citation: Citation }) {
  const url = doiUrl(citation.doi);
  return (
    <span className="group relative inline-block">
      <button
        type="button"
        onClick={() => url && window.open(url, "_blank", "noopener,noreferrer")}
        className={`mx-px inline-flex h-[15px] min-w-[15px] items-center justify-center rounded bg-blue-500/20 px-1 align-super text-[10px] font-medium leading-none text-blue-300 transition ${
          url ? "cursor-pointer hover:bg-blue-500/40" : "cursor-default opacity-60"
        }`}
      >
        {citation.citation_id}
      </button>
      <span className="pointer-events-none absolute left-1/2 top-full z-30 mt-1 hidden w-72 -translate-x-1/2 rounded-lg border border-zinc-700 bg-zinc-900 p-2.5 text-left shadow-xl group-hover:block">
        <span className="block text-xs font-medium leading-snug text-zinc-100">
          {citation.title}
        </span>
        <span className="mt-1 block text-[10px] text-zinc-400">{citationMeta(citation)}</span>
        {citation.abstract && (
          <span className="mt-1.5 block max-h-28 overflow-hidden text-[10px] leading-snug text-zinc-400">
            {citation.abstract}
          </span>
        )}
        {url && <span className="mt-1.5 block font-mono text-[10px] text-blue-400">{citation.doi}</span>}
      </span>
    </span>
  );
}

// --- textWithChips: moved verbatim from ChatPanel.tsx:150-170 ---
export function textWithChips(text: string, lookup: Map<number, Citation>): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const re = /\[(\d+)\]/g;
  let last = 0;
  let key = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const citation = lookup.get(Number(match[1]));
    nodes.push(
      citation ? (
        <CitationChip key={`c${key++}`} citation={citation} />
      ) : (
        <span key={`t${key++}`}>{match[0]}</span>
      ),
    );
    last = re.lastIndex;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

/**
 * Inline routing badge at the top of an assistant bubble — NEW.
 *
 * refuse/clarify get a loud chip because the agent never ran and the fixed
 * message that follows only makes sense next to "why". proceed gets a quiet,
 * clickable intent chip that jumps to the RoutePanel. `onOpenRoute` is optional
 * so the chip is inert (not dead-looking) when there is nowhere to jump.
 */
export function RouteChip({
  route,
  onOpenRoute,
}: {
  route: RouteDecision;
  onOpenRoute?: () => void;
}) {
  const g = guardStyle(route.guard);
  const intent = route.intent ? (INTENT_LABELS[route.intent] ?? route.intent) : null;

  if (route.guard === "refuse" || route.guard === "clarify") {
    return (
      <span
        className={`mb-1.5 inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-[11px] font-medium ${g.chip}`}
      >
        <span className={g.dot}>●</span>
        {g.label}
        {intent && <span className="opacity-70">· {intent}</span>}
      </span>
    );
  }

  // proceed (or unknown guard): quiet intent chip, clickable to the route panel.
  const clickable = typeof onOpenRoute === "function";
  return (
    <button
      type="button"
      onClick={onOpenRoute}
      disabled={!clickable}
      className={`mb-1.5 inline-flex items-center gap-1.5 rounded border border-zinc-700/60 bg-zinc-800/50 px-2 py-0.5 text-[11px] text-zinc-400 transition ${
        clickable ? "cursor-pointer hover:border-zinc-500 hover:text-zinc-200" : "cursor-default"
      }`}
    >
      <span className={g.dot}>●</span>
      {intent ?? g.label}
    </button>
  );
}
```

- [ ] **Step 2: 类型检查 + lint**

Run: `cd E:/agent/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint src/components/ChatChips.tsx`
Expected: 无输出（PASS）。

- [ ] **Step 3: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/components/ChatChips.tsx
git commit -m "feat(web): ChatChips（ToolChip/CitationChip/textWithChips 抽出 + RouteChip 新增）"
```

---

### Task 8: `components/MessageBubble.tsx`

**Files:**
- Create: `apps/web/src/components/MessageBubble.tsx`

- [ ] **Step 1: 写 MessageBubble.tsx**

消息渲染逻辑 move from `ChatPanel.tsx:303-333`，抽成独立组件；新增 RouteChip（仅 assistant）与流式光标；`isStreaming` 由父层传入：

```tsx
"use client";

import type { UIMessage } from "ai";
import { byId, citationsOf, routeOf } from "@/components/chatParts";
import { RouteChip, ToolChip, textWithChips } from "@/components/ChatChips";

/**
 * One chat message. Text parts render [n] as citation chips, dynamic-tool parts
 * render as ToolChip, and an assistant message's route decision renders as a
 * RouteChip above the prose. `isStreaming` drives the trailing caret and is
 * computed by the parent (only the last assistant message while status==="streaming").
 */
export default function MessageBubble({
  message,
  isStreaming,
  onOpenRoute,
}: {
  message: UIMessage;
  isStreaming: boolean;
  onOpenRoute?: () => void;
}) {
  const lookup = byId(citationsOf(message));
  const route = message.role === "assistant" ? routeOf(message) : null;
  const isUser = message.role === "user";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80%] whitespace-pre-wrap rounded-lg px-3.5 py-2.5 text-sm leading-relaxed ${
          isUser
            ? "bg-blue-600 text-white"
            : "border border-zinc-800 bg-zinc-900 text-zinc-200"
        }`}
      >
        {route && <RouteChip route={route} onOpenRoute={onOpenRoute} />}
        <div>
          {message.parts.map((part, i) => {
            if (part.type === "text")
              return <span key={i}>{textWithChips(part.text, lookup)}</span>;
            if (part.type === "dynamic-tool")
              return <ToolChip key={`${part.toolCallId}-${i}`} part={part} />;
            return null;
          })}
        </div>
        {isStreaming && <span className="ml-0.5 animate-pulse text-zinc-500">▍</span>}
      </div>
    </div>
  );
}
```

注意：`data-citation`/`data-route` part 不在这里直接渲染（它们经 `textWithChips`/`RouteChip` 消费），`.map` 对它们返回 `null`，与现有 ChatPanel 行为一致。

- [ ] **Step 2: 类型检查 + lint**

Run: `cd E:/agent/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint src/components/MessageBubble.tsx`
Expected: 无输出（PASS）。

- [ ] **Step 3: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/components/MessageBubble.tsx
git commit -m "feat(web): MessageBubble（单条消息 + 内联 RouteChip）"
```

---

### Task 9: 右侧面板（ReferencesPanel / RoutePanel / ActivityPanel / OutlinePanel / PanelHost）

**Files:**
- Create: `apps/web/src/components/panels/ReferencesPanel.tsx`
- Create: `apps/web/src/components/panels/RoutePanel.tsx`
- Create: `apps/web/src/components/panels/ActivityPanel.tsx`
- Create: `apps/web/src/components/panels/OutlinePanel.tsx`
- Create: `apps/web/src/components/panels/PanelHost.tsx`

- [ ] **Step 1: ReferencesPanel.tsx**

move verbatim from `ChatPanel.tsx:393-446`，但**去掉 `hidden ... lg:flex`**（响应式改由 PanelHost 统一控制），根元素改为普通 `flex`：

```tsx
"use client";

import { doiUrl, type Citation } from "@/lib/api";
import { citationMeta } from "@/components/chatParts";

/** Cited-only reference list for the current answer. Extracted from the old
 *  ChatPanel; the `hidden lg:flex` responsive wrapper moved up to PanelHost so
 *  every panel shares one collapse rule. */
export default function ReferencesPanel({ citations }: { citations: Citation[] }) {
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-zinc-800 px-4 py-3">
        <h2 className="text-sm font-semibold">参考文献</h2>
        <p className="font-mono text-[11px] text-zinc-500">{citations.length} 条 · 本次回答</p>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto px-4 py-3">
        {citations.length === 0 ? (
          <p className="text-xs leading-relaxed text-zinc-600">
            检索文献后，可点击的引用会出现在这里。试试「找 5 篇 contrastive learning 的代表性论文」。
          </p>
        ) : (
          citations.map((c) => {
            const url = doiUrl(c.doi);
            return (
              <div key={c.citation_id} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-2.5">
                <div className="flex gap-2">
                  <span className="mt-px inline-flex h-[15px] min-w-[15px] shrink-0 items-center justify-center rounded bg-blue-500/20 px-1 text-[10px] leading-none text-blue-300">
                    {c.citation_id}
                  </span>
                  <div className="min-w-0">
                    <p className="text-xs font-medium leading-snug text-zinc-100">{c.title}</p>
                    <p className="mt-0.5 text-[10px] text-zinc-400">{citationMeta(c)}</p>
                    {url ? (
                      <a href={url} target="_blank" rel="noopener noreferrer" className="mt-1 inline-block truncate font-mono text-[10px] text-blue-400 hover:underline">
                        {c.doi}
                      </a>
                    ) : (
                      <span className="mt-1 inline-block font-mono text-[10px] text-zinc-600">无 DOI</span>
                    )}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: RoutePanel.tsx（新）**

```tsx
"use client";

import type { RouteDecision } from "@/lib/api";
import { INTENT_LABELS, SOURCE_LABELS, guardStyle } from "@/lib/labels";

/**
 * Every turn's routing decision, newest first. This is the D8 `route` event made
 * visible for the first time — the interview-facing surface for "routing is a
 * graph node" and "the L0 guard overrides a leaking model" (a 降重 request the
 * router calls `polish` still shows guard=refuse here).
 */
export default function RoutePanel({ routes }: { routes: RouteDecision[] }) {
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-zinc-800 px-4 py-3">
        <h2 className="text-sm font-semibold">路由决策</h2>
        <p className="font-mono text-[11px] text-zinc-500">{routes.length} 轮 · 最新在上</p>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto px-4 py-3">
        {routes.length === 0 ? (
          <p className="text-xs leading-relaxed text-zinc-600">
            提问后，每轮的路由 / guard 决策会出现在这里（意图、是否放行、哪个后端决定、置信度）。
          </p>
        ) : (
          routes.map((r, i) => {
            const g = guardStyle(r.guard);
            const intent = r.intent ? (INTENT_LABELS[r.intent] ?? r.intent) : "—";
            const source = r.source ? (SOURCE_LABELS[r.source] ?? r.source) : "—";
            const pct = typeof r.confidence === "number" ? Math.round(r.confidence * 100) : null;
            return (
              <div key={i} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-medium text-zinc-100">{intent}</span>
                  <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] ${g.chip}`}>
                    <span className={g.dot}>●</span>
                    {g.label}
                  </span>
                </div>
                <dl className="mt-2 space-y-1 font-mono text-[10px] text-zinc-400">
                  <div className="flex justify-between gap-2">
                    <dt className="text-zinc-500">后端</dt>
                    <dd className="truncate text-zinc-300">{source}</dd>
                  </div>
                  <div className="flex justify-between gap-2">
                    <dt className="text-zinc-500">置信度</dt>
                    <dd className="text-zinc-300">{pct === null ? "—" : `${pct}%`}</dd>
                  </div>
                  {pct !== null && (
                    <div className="h-1 w-full overflow-hidden rounded bg-zinc-800">
                      <div className="h-full bg-blue-500/70" style={{ width: `${pct}%` }} />
                    </div>
                  )}
                  <div className="flex flex-wrap gap-1.5 pt-0.5">
                    {r.from_cache && <Tag>缓存</Tag>}
                    {r.needs_clarify && <Tag>需澄清</Tag>}
                    {r.path_deep && <Tag>深路径</Tag>}
                  </div>
                </dl>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function Tag({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded border border-zinc-700 bg-zinc-800/70 px-1.5 py-0.5 text-[10px] text-zinc-400">
      {children}
    </span>
  );
}
```

- [ ] **Step 3: ActivityPanel.tsx（新）**

```tsx
"use client";

import type { ActivityRow } from "@/components/chatParts";
import { summarize } from "@/components/chatParts";
import { TOOL_LABELS } from "@/lib/labels";

/**
 * A console of every tool call, newest first. Makes the ReAct loop visible — the
 * honest half of "辅助不代写": the numbers in a reply are derived from tools the
 * user can inspect here, not invented by the model.
 */
export default function ActivityPanel({ rows }: { rows: ActivityRow[] }) {
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-zinc-800 px-4 py-3">
        <h2 className="text-sm font-semibold">活动</h2>
        <p className="font-mono text-[11px] text-zinc-500">{rows.length} 次工具调用 · 最新在上</p>
      </div>
      <div className="flex-1 space-y-1.5 overflow-y-auto px-4 py-3">
        {rows.length === 0 ? (
          <p className="text-xs leading-relaxed text-zinc-600">
            还没有工具调用。让 Agent 检索文献或统计字数后，调用记录会出现在这里。
          </p>
        ) : (
          rows.map(({ part, turn }, i) => {
            const finished = part.state === "output-available" || part.state === "output-error";
            return (
              <div key={`${part.toolCallId}-${i}`} className="rounded border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5">
                <div className="flex items-center gap-2 font-mono text-[11px]">
                  <span className={part.state === "output-error" ? "text-red-400" : finished ? "text-emerald-400" : "animate-pulse text-zinc-300"}>
                    {part.state === "output-error" ? "✕" : finished ? "✓" : "●"}
                  </span>
                  <span className="text-zinc-200">{TOOL_LABELS[part.toolName] ?? part.toolName}</span>
                  <span className="ml-auto text-zinc-600">#{turn + 1}</span>
                </div>
                {finished && part.output != null && (
                  <p className="mt-1 truncate font-mono text-[10px] text-zinc-500">{summarize(part.output)}</p>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: OutlinePanel.tsx（新，Week-2 占位）**

```tsx
"use client";

import type { UiContext } from "@/lib/api";

/** The outline signals the workspace currently has, as a read-only preview. */
const STAGES: ReadonlyArray<{ id: string; label: string }> = [
  { id: "topic", label: "选题" },
  { id: "drafting", label: "起草" },
  { id: "revising", label: "修订" },
  { id: "submitting", label: "投稿" },
];

/**
 * Week-2 placeholder, clearly marked. There is no document model yet, so this
 * panel does not fake one: it shows the real ui_context signals that WILL drive
 * the outline (paper_stage, cursor_section) and a ghosted tree to indicate shape.
 */
export default function OutlinePanel({ uiContext }: { uiContext: UiContext }) {
  const stage = uiContext.paper_stage ?? "topic";
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-zinc-800 px-4 py-3">
        <h2 className="text-sm font-semibold">大纲</h2>
        <p className="font-mono text-[11px] text-zinc-500">Week 2 · 占位</p>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
        <p className="rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-[11px] leading-relaxed text-amber-200">
          大纲功能将在 Week 2 接入文档模型。当前仅展示已有的路由上下文信号，尚无正文结构数据。
        </p>

        <div>
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-zinc-500">
            paper_stage（真实信号）
          </p>
          <div className="flex flex-wrap gap-1">
            {STAGES.map((s) => (
              <span
                key={s.id}
                className={`rounded px-2 py-0.5 text-[11px] ${
                  s.id === stage ? "bg-zinc-100 text-zinc-900" : "bg-zinc-800/70 text-zinc-500"
                }`}
              >
                {s.label}
              </span>
            ))}
          </div>
        </div>

        <div>
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-zinc-500">
            cursor_section（真实信号）
          </p>
          <p className="font-mono text-[11px] text-zinc-300">{uiContext.cursor_section ?? "（无）"}</p>
        </div>

        <div>
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-zinc-500">
            形态示意（占位，非真实数据）
          </p>
          <ul className="space-y-1 text-xs text-zinc-600">
            {["摘要", "引言", "方法", "结果", "讨论"].map((s) => (
              <li key={s} className="flex items-center gap-2">
                <span className="text-zinc-700">▸</span>
                {s}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: PanelHost.tsx（新）**

统一承载「`lg` 以下隐藏」与「chat 收起」两条规则，按 `activePanel` 选择面板：

```tsx
"use client";

import type { UIMessage } from "ai";
import type { UiContext } from "@/lib/api";
import type { PanelId } from "@/lib/panels";
import { activeCitations, activityOf, routesOf } from "@/components/chatParts";
import ReferencesPanel from "@/components/panels/ReferencesPanel";
import RoutePanel from "@/components/panels/RoutePanel";
import ActivityPanel from "@/components/panels/ActivityPanel";
import OutlinePanel from "@/components/panels/OutlinePanel";

/**
 * The right workspace column. Returns null when activePanel==="chat" (collapsed)
 * and is hidden below `lg` so the chat stays usable on a narrow window — the same
 * rule the old ReferencesPanel carried, now applied to every panel. All data is
 * derived from `messages` here, so panels stay presentational.
 */
export default function PanelHost({
  activePanel,
  messages,
  uiContext,
}: {
  activePanel: PanelId;
  messages: UIMessage[];
  uiContext: UiContext;
}) {
  if (activePanel === "chat") return null;

  const body =
    activePanel === "references" ? (
      <ReferencesPanel citations={activeCitations(messages)} />
    ) : activePanel === "route" ? (
      <RoutePanel routes={routesOf(messages)} />
    ) : activePanel === "activity" ? (
      <ActivityPanel rows={activityOf(messages)} />
    ) : (
      <OutlinePanel uiContext={uiContext} />
    );

  return (
    <aside className="hidden w-96 shrink-0 flex-col border-l border-zinc-800 lg:flex">
      {body}
    </aside>
  );
}
```

- [ ] **Step 6: 类型检查 + lint**

Run: `cd E:/agent/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint src/components/panels`
Expected: 无输出（PASS）。

- [ ] **Step 7: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/components/panels
git commit -m "feat(web): 右侧工作区面板（文献/路由/活动/大纲占位 + PanelHost 切换）"
```

---

### Task 10: `components/IconRail.tsx`

**Files:**
- Create: `apps/web/src/components/IconRail.tsx`

- [ ] **Step 1: 写 IconRail.tsx**

内联 SVG 图标（无 emoji、无图标库）。选中项高亮，`title` 提供 tooltip：

```tsx
"use client";

import { PANELS, PANEL_META, type PanelId } from "@/lib/panels";

/**
 * The far-left icon rail. Selecting a panel is real navigation (it switches the
 * right workspace column) AND a routing signal (Workspace maps it into
 * ui_context.active_panel). "chat" collapses the workspace. Icons are inline SVG
 * — no emoji, no icon-library dependency.
 */
export default function IconRail({
  activePanel,
  onSelect,
}: {
  activePanel: PanelId;
  onSelect: (id: PanelId) => void;
}) {
  return (
    <nav className="hidden w-14 shrink-0 flex-col items-center gap-1 border-r border-zinc-800 py-3 lg:flex">
      {PANELS.map((id) => {
        const active = id === activePanel;
        return (
          <button
            key={id}
            type="button"
            title={`${PANEL_META[id].name} · ${PANEL_META[id].hint}`}
            aria-label={PANEL_META[id].name}
            aria-pressed={active}
            onClick={() => onSelect(id)}
            className={`flex h-10 w-10 items-center justify-center rounded-lg transition ${
              active
                ? "bg-zinc-100 text-zinc-900"
                : "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
            }`}
          >
            <PanelIcon id={id} />
          </button>
        );
      })}
    </nav>
  );
}

function PanelIcon({ id }: { id: PanelId }) {
  const common = {
    width: 20,
    height: 20,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  switch (id) {
    case "chat":
      return (
        <svg {...common}>
          <path d="M21 12a8 8 0 0 1-8 8H4l2-3a8 8 0 1 1 15-5z" />
        </svg>
      );
    case "references":
      return (
        <svg {...common}>
          <path d="M4 5a2 2 0 0 1 2-2h9v18H6a2 2 0 0 1-2-2z" />
          <path d="M15 3h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-3" />
        </svg>
      );
    case "route":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="M15.5 8.5 13 13l-4.5 2.5L11 11z" />
        </svg>
      );
    case "activity":
      return (
        <svg {...common}>
          <path d="M3 12h4l3 8 4-16 3 8h4" />
        </svg>
      );
    case "outline":
      return (
        <svg {...common}>
          <path d="M8 6h13M8 12h13M8 18h13" />
          <circle cx="3.5" cy="6" r="1" />
          <circle cx="3.5" cy="12" r="1" />
          <circle cx="3.5" cy="18" r="1" />
        </svg>
      );
  }
}
```

- [ ] **Step 2: 类型检查 + lint**

Run: `cd E:/agent/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint src/components/IconRail.tsx`
Expected: 无输出（PASS）。`switch` 覆盖全部 5 个 PanelId，TS 穷尽检查通过（无 default，返回类型 `JSX.Element`）。若报「非所有路径返回值」，在 switch 后加 `default: return null;`。

- [ ] **Step 3: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/components/IconRail.tsx
git commit -m "feat(web): IconRail 图标导航栏（内联 SVG）"
```

---

### Task 11: `components/ChatColumn.tsx` + `components/Workspace.tsx` + 切换 page.tsx + 删除 ChatPanel

**Files:**
- Create: `apps/web/src/components/ChatColumn.tsx`
- Create: `apps/web/src/components/Workspace.tsx`
- Modify: `apps/web/src/app/page.tsx`
- Delete: `apps/web/src/components/ChatPanel.tsx`

- [ ] **Step 1: ChatColumn.tsx**

中间对话列。header + 消息列表 + 输入框 + 错误条，逻辑 move from `ChatPanel.tsx:261-378`，但状态（messages/status/error/uiContext/activePanel）由 props 传入（Workspace 持有）：

```tsx
"use client";

import { useEffect, useRef, useState } from "react";
import type { UIMessage } from "ai";
import type { ChatStatus } from "ai";
import type { UiContext } from "@/lib/api";
import type { PanelId } from "@/lib/panels";
import BackendBadge from "@/components/BackendBadge";
import MessageBubble from "@/components/MessageBubble";

/**
 * The center conversation column: header, message list, input form, error bar.
 * Presentational — all chat state lives in Workspace and arrives via props, so
 * this component has only local UI state (the input box) and the scroll effect.
 */
export default function ChatColumn({
  messages,
  status,
  error,
  threadId,
  backendUp,
  uiContext,
  onSend,
  onStop,
  onClearError,
  onOpenRoute,
}: {
  messages: UIMessage[];
  status: ChatStatus;
  error: Error | undefined;
  threadId: string;
  backendUp: boolean | null;
  uiContext: UiContext;
  onSend: (text: string) => void | Promise<void>;
  onStop: () => void;
  onClearError: () => void;
  onOpenRoute: () => void;
}) {
  const [input, setInput] = useState("");
  const busy = status === "submitted" || status === "streaming";
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, status]);

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    onClearError();
    void onSend(text);
  }

  const lastId = messages[messages.length - 1]?.id;

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <header className="flex items-center justify-between border-b border-zinc-800 px-5 py-3">
        <div>
          <h1 className="text-sm font-semibold">Academic Research Copilot</h1>
          <p className="font-mono text-[11px] text-zinc-500">
            thread {threadId ? threadId.slice(0, 8) : "--------"}
            <span className="ml-2 text-zinc-600">· {status}</span>
            <span className="ml-2 text-zinc-600">· {uiContext.active_panel ?? "chat"}</span>
          </p>
        </div>
        <BackendBadge up={backendUp} />
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto px-5 py-6">
        {messages.length === 0 && (
          <p className="mx-auto max-w-md text-center text-sm text-zinc-500">
            检索文献后，正文里的 [n] 是可点击的引用（悬浮看标题摘要，点开跳 DOI）。左侧导航栏可切换
            文献 / 路由 / 活动 工作区。试试「找 5 篇 contrastive learning 的代表性论文」。
          </p>
        )}
        {messages.map((m) => (
          <MessageBubble
            key={m.id}
            message={m}
            isStreaming={status === "streaming" && m.role === "assistant" && m.id === lastId}
            onOpenRoute={onOpenRoute}
          />
        ))}
        {status === "submitted" && (
          <div className="flex justify-start">
            <div className="rounded-lg border border-zinc-800 bg-zinc-900 px-3.5 py-2.5 text-sm text-zinc-500">
              思考中…
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {error && (
        <div className="mx-5 mb-2 rounded border border-red-900 bg-red-950/50 px-3 py-2 font-mono text-[11px] text-red-300">
          {error.message}
        </div>
      )}

      <form onSubmit={onSubmit} className="border-t border-zinc-800 p-4">
        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="问点什么…（试试「数一下这段有多少字」）"
            disabled={busy}
            className="flex-1 rounded-lg border border-zinc-700 bg-zinc-900 px-3.5 py-2.5 text-sm outline-none placeholder:text-zinc-600 focus:border-zinc-500 disabled:opacity-50"
          />
          {busy ? (
            <button type="button" onClick={onStop} className="rounded-lg border border-zinc-600 px-5 py-2.5 text-sm font-medium text-zinc-300 transition hover:bg-zinc-800">
              停止
            </button>
          ) : (
            <button type="submit" disabled={!input.trim()} className="rounded-lg bg-zinc-100 px-5 py-2.5 text-sm font-medium text-zinc-900 transition hover:bg-white disabled:opacity-40">
              发送
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
```

注意：`import type { ChatStatus } from "ai"` —— 若 v7 未导出 `ChatStatus`，改用 `status: "ready" | "submitted" | "streaming" | "error"` 或从 `useChat` 返回值 `typeof status` 推导。实现时先 `pnpm exec tsc --noEmit` 验证该 import。

- [ ] **Step 2: Workspace.tsx**

外壳，持有全部状态：

```tsx
"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { useChat } from "@ai-sdk/react";
import { API_BASE, CHAT_ENDPOINT, getThreadId, type UiContext } from "@/lib/api";
import { SseChatTransport } from "@/lib/sseTransport";
import { PANEL_TO_UI_CONTEXT, type PanelId } from "@/lib/panels";
import IconRail from "@/components/IconRail";
import ChatColumn from "@/components/ChatColumn";
import PanelHost from "@/components/panels/PanelHost";

// The thread id never changes within a session; useSyncExternalStore still needs
// a stable subscribe fn. SSR/hydration render "" then re-render with the real id
// (replaces a `mounted` flag — React 19 forbids setState-in-effect).
function subscribeToThreadId() {
  return () => {};
}
function getServerThreadId() {
  return "";
}

/**
 * The three-column shell and the single owner of chat state: useChat, uiContext,
 * and which workspace panel is active. Selecting a rail panel both switches the
 * right column and updates ui_context.active_panel (mapped to a value the backend
 * understands), so the rail is a real routing signal, not decoration.
 */
export default function Workspace() {
  const [backendUp, setBackendUp] = useState<boolean | null>(null);
  const [activePanel, setActivePanel] = useState<PanelId>("chat");
  const [uiContext, setUiContext] = useState<UiContext>({
    active_panel: "chat",
    paper_stage: "topic",
  });

  const threadId = useSyncExternalStore(subscribeToThreadId, getThreadId, getServerThreadId);

  // Built once — a new transport every render would reset useChat. ui_context
  // travels per request via sendMessage's body, not on the transport.
  const [transport] = useState(() => new SseChatTransport({ api: CHAT_ENDPOINT }));

  const { messages, sendMessage, status, error, stop, clearError } = useChat({
    id: threadId || undefined,
    transport,
  });

  useEffect(() => {
    fetch(`${API_BASE}/health`)
      .then((r) => setBackendUp(r.ok))
      .catch(() => setBackendUp(false));
  }, []);

  function selectPanel(id: PanelId) {
    setActivePanel(id);
    // Keep the routing hint in sync with what the user is looking at, mapped to
    // the backend's editor-panel vocabulary (diagnostic panels → "chat").
    setUiContext((c) => ({ ...c, active_panel: PANEL_TO_UI_CONTEXT[id] }));
  }

  async function handleSend(text: string) {
    await sendMessage({ text }, { body: { ui_context: uiContext } });
  }

  return (
    <div className="flex h-dvh bg-zinc-950 text-zinc-100">
      <IconRail activePanel={activePanel} onSelect={selectPanel} />
      <ChatColumn
        messages={messages}
        status={status}
        error={error}
        threadId={threadId}
        backendUp={backendUp}
        uiContext={uiContext}
        onSend={handleSend}
        onStop={() => void stop()}
        onClearError={clearError}
        onOpenRoute={() => selectPanel("route")}
      />
      <PanelHost activePanel={activePanel} messages={messages} uiContext={uiContext} />
    </div>
  );
}
```

- [ ] **Step 3: page.tsx 切到 Workspace + 骨架改三栏**

把 `apps/web/src/app/page.tsx` 改为：

```tsx
import { Suspense } from "react";
import Workspace from "@/components/Workspace";

/**
 * Prerendered shell shown while Workspace streams in.
 *
 * The Suspense boundary is load-bearing, not decoration: with `cacheComponents:
 * true`, Next 16 prerenders Client Components at build time and hard-fails on
 * unstable values — and `useChat` calls Math.random() to mint a chat id when none
 * is given. Deferring Workspace behind Suspense keeps that call in the browser
 * (nextjs.org/docs/messages/blocking-prerender-random-client).
 */
function WorkspaceSkeleton() {
  return (
    <div className="flex h-dvh bg-zinc-950 text-zinc-100">
      <div className="hidden w-14 shrink-0 flex-col items-center gap-2 border-r border-zinc-800 py-4 lg:flex">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="h-10 w-10 rounded-lg bg-zinc-800/60" />
        ))}
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-zinc-800 px-5 py-3">
          <div>
            <h1 className="text-sm font-semibold">Academic Research Copilot</h1>
            <p className="font-mono text-[11px] text-zinc-500">thread --------</p>
          </div>
          <span className="rounded bg-zinc-700 px-2 py-1 font-mono text-[11px] text-zinc-300">loading</span>
        </header>
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-zinc-600">正在连接后端…</p>
        </div>
      </div>
    </div>
  );
}

export default function Home() {
  return (
    <Suspense fallback={<WorkspaceSkeleton />}>
      <Workspace />
    </Suspense>
  );
}
```

- [ ] **Step 4: 删除 ChatPanel.tsx**

Run: `cd E:/agent && git rm apps/web/src/components/ChatPanel.tsx`
（若尚未 git 跟踪则 `rm apps/web/src/components/ChatPanel.tsx`。）此时 page.tsx 已不再 import 它，无悬空引用。

- [ ] **Step 5: 全量类型检查 + lint + 构建**

Run:
```bash
cd E:/agent/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint . --max-warnings=0 && pnpm build
```
Expected: tsc 无输出；eslint 无告警；`next build` 成功编译且预渲染 `/`（`cacheComponents` 下 `<Suspense>` 保住预渲染，不报 blocking-prerender-random-client）。

若 `next build` 因 `ChatStatus` 未导出而失败 → 按 Task 11 Step 1 注释改 status 类型。若因其他 React 19 hook 规则失败 → 检查没有在 render 期写 ref、没有在 effect 里同步 setState（本计划的 thread id 用 useSyncExternalStore、per-request 数据用 sendMessage body，均合规）。

- [ ] **Step 6: Commit checkpoint（需明文授权）**

```bash
git add apps/web/src/components/ChatColumn.tsx apps/web/src/components/Workspace.tsx apps/web/src/app/page.tsx
git commit -m "feat(web): 三栏工作区外壳（Workspace/ChatColumn）+ page 切换 + 删除 ChatPanel 单体"
```

---

### Task 12: 浏览器实测验收

**Files:** 无（纯验证）

- [ ] **Step 1: 起后端 + 前端**

后端：按项目惯例起 `apps/api`（FastAPI，:8000）。前端：`cd E:/agent/apps/web && pnpm dev`（:3000）。
（若后端未配路由 backend，route 事件不发 → 路由面板空态，属正常降级；要验证 route/refuse/clarify 需后端配了 router。起服务的具体命令以用户环境为准，必要时请用户确认。）

- [ ] **Step 2: 布局与导航**

浏览器打开 http://localhost:3000，核对：
- 三栏渲染：左侧图标栏（5 个 SVG 图标）、中间对话、右侧面板。
- 点导航栏「文献/路由/活动/大纲」→ 右侧面板切换；点「对话」→ 右侧收起、对话列占满。
- 选中项高亮；hover 出 tooltip（name · hint）。

- [ ] **Step 3: 真实数据流（后端配了 router 时）**

- 发「找 5 篇 contrastive learning 的代表性论文」：
  - 活动面板出现 `检索文献`（search_papers）行，状态 ✓，带 output 摘要。
  - 文献面板出现 cited 条目（[n] + 标题 + DOI 链接）。
  - 路由面板出现一条 `文献检索 / 放行`，后端与置信度有值。
  - 正文 [n] 是可点击 chip，气泡顶部有低调 intent chip。
- 发「帮我把这段降重」：路由面板 `已拒绝`（红），气泡顶部红 RouteChip「已拒绝 · 润色」，回复是拒绝文案（agent 未跑）。
- 发「在吗」：路由面板 `需澄清`（琥珀），气泡琥珀 RouteChip，回复是澄清文案。

- [ ] **Step 4: 响应式与降级**

- 窗口缩到 `lg` 以下（<1024px）：导航栏与右侧面板隐藏，只剩单栏对话；[n] chip 悬浮仍出标题/摘要。
- 大纲面板：显示琥珀色 Week-2 占位横幅 + paper_stage/cursor_section 真实信号 + ghosted 树。
- 后端 offline 时：header 右上 badge 显示 `api offline`（红）。

- [ ] **Step 5: 记录验收结果**

把实测结果（哪些通过、哪些受后端配置限制未验）如实记录，不推算。若要写进 devlog/README，数字必须实测。

- [ ] **Step 6: 最终 Commit checkpoint（需明文授权）**

若前述各任务未逐一提交，此处一次性提交全部前端改动（同样需明文授权）：
```bash
git add apps/web docs/superpowers
git commit -m "feat(web): 主流 Agent 三栏工作区 UI（导航栏 + 对话 + 文献/路由/活动/大纲面板）"
```

---

## Self-Review

**1. Spec coverage（逐节对照 spec）：**
- §3 布局 B 三栏 → Task 10（IconRail）+ Task 11（Workspace/ChatColumn/PanelHost 布局）。✅
- §4 组件分解 → File Structure + Task 5–11 覆盖全部文件。✅（新增 `lib/labels.ts`、`lib/panels.ts` 两个常量模块，已在 File Structure 说明，是 spec §6/§7 未落文件的常量的归置。）
- §5 data-route 数据流 → Task 1（类型）+ Task 2（传输）+ Task 5（routeOf/routesOf）+ Task 7（RouteChip）+ Task 9（RoutePanel）。✅
- §6 active_panel 映射 → Task 4（PANEL_TO_UI_CONTEXT）+ Task 11 Step 2（selectPanel 同步）。✅
- §7 各面板内容 → 对话 Task 11；文献 Task 9 Step 1；路由 Task 9 Step 2；活动 Task 9 Step 3；大纲 Task 9 Step 4；RouteChip Task 7。✅
- §8 响应式与错误处理 → PanelHost 的 `hidden ... lg:flex` + IconRail 的 `hidden ... lg:flex`（Task 9/10）；脏事件 break（Task 2 Step 2 的 `typeof route !== "object"`）；router=None 空态（Task 9 Step 2）；Suspense 保留（Task 11 Step 3）。✅
- §9 明确不做 → 计划未涉及编辑器/Threads/拖拽/移动端抽屉/useChat 泛型。✅
- §10 测试与验收 → 每任务 tsc/eslint，Task 11 build，Task 12 浏览器清单。✅

**2. Placeholder scan：** 无 TBD/TODO。所有「move verbatim from ChatPanel.tsx:A-B」均指向仓库中已存在的确切代码行，非占位。新增代码（RouteDecision/labels/panels/chatParts 新函数/RouteChip/RoutePanel/ActivityPanel/OutlinePanel/PanelHost/IconRail/ChatColumn/Workspace/page）均给出完整代码。✅

**3. Type consistency（跨任务签名核对）：**
- `RouteDecision`（Task 1）字段与 Task 5 `routeOf`、Task 7 `RouteChip`、Task 9 `RoutePanel` 读取一致（intent/guard/needs_clarify/path_deep/source/confidence/from_cache）。✅
- `guardStyle`（Task 3）签名 `(string|null|undefined) => GuardStyle`，Task 7/9 调用一致。✅
- `INTENT_LABELS`/`SOURCE_LABELS`/`TOOL_LABELS`（Task 3）在 Task 7/9 按 `Record<string,string>` + `?? raw` 使用，一致。✅
- `PanelId`/`PANELS`/`PANEL_META`/`PANEL_TO_UI_CONTEXT`（Task 4）在 Task 10/11 使用一致；`PANEL_META[id].name/.hint` 字段匹配。✅
- `ActivityRow`（Task 5）= `{part, turn}`，Task 9 ActivityPanel 解构 `{ part, turn }` 一致。✅
- `activeCitations`/`routesOf`/`activityOf`（Task 5）签名 `(UIMessage[]) => ...`，Task 9 PanelHost 调用一致。✅
- `ChatColumn` props（Task 11 Step 1）与 `Workspace` 传入（Task 11 Step 2）逐字段对齐（messages/status/error/threadId/backendUp/uiContext/onSend/onStop/onClearError/onOpenRoute）。✅
- `PanelHost` props（Task 9 Step 5：activePanel/messages/uiContext）与 Workspace 传入一致。✅
- `MessageBubble` props（Task 8：message/isStreaming/onOpenRoute）与 ChatColumn 传入一致。✅
- 已标注两处需在实现时用 tsc 确认的外部类型：`ChatStatus` 是否由 `ai` v7 导出（Task 11 Step 1 注释给了回退方案）；`PanelIcon` switch 穷尽性（Task 10 Step 2 给了 default 回退）。✅

无发现需补的 spec 缺口。计划可执行。
