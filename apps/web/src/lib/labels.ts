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
  knowledge_qa: "资料问答",
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
  knowledge: "资料模式（确定性）",
};

/** Look up a guard's style with the neutral fallback baked in. */
export function guardStyle(guard: string | null | undefined): GuardStyle {
  return (guard && GUARD_META[guard]) || GUARD_FALLBACK;
}
