import type { DynamicToolUIPart, UIMessage } from "ai";
import type { Citation, RouteDecision } from "@/lib/api";
import type { KnowledgeSource } from "@/lib/workspaceApi";

export function knowledgeSourcesOf(message: UIMessage): KnowledgeSource[] {
  const sources = message.parts
    .filter((part) => part.type === "data-knowledge-source")
    .map((part) => (part as unknown as { data: KnowledgeSource }).data)
    .filter((source) => source && typeof source.source_id === "string" && typeof source.project_id === "string" && typeof source.document_id === "string" && typeof source.text === "string");
  return [...new Map(sources.map((source) => [source.source_id, source])).values()];
}

export function turnStatusOf(message: UIMessage): { status: "interrupted" | "error" | "complete"; message?: string } | null {
  const part = message.parts.findLast((p) => p.type === "data-turn-status");
  return part ? (part as unknown as { data: { status: "interrupted" | "error" | "complete"; message?: string } }).data : null;
}

/**
 * Pure derivations from the message list. No JSX, no state — every panel and
 * bubble reads its data through these so the logic lives in one testable place
 * and `Workspace` stays the only stateful component.
 */

// --- citations: moved verbatim from the old ChatPanel.tsx ---

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

export function summarize(output: unknown): string {
  if (output == null) return "";
  const text = typeof output === "string" ? output : JSON.stringify(output);
  return text.length > 60 ? `${text.slice(0, 60)}…` : text;
}

/**
 * The ReferencesPanel's data: the newest answer that cited something, filtered to
 * the [n] markers it actually uses. A follow-up turn with no literature does not
 * blank the previous answer's references.
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
