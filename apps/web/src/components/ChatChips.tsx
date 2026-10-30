"use client";

import type { DynamicToolUIPart } from "ai";
import { doiUrl, type Citation, type RouteDecision } from "@/lib/api";
import { TOOL_LABELS, INTENT_LABELS, guardStyle } from "@/lib/labels";
import { citationMeta, summarize } from "@/components/chatParts";
import type { KnowledgeSource } from "@/lib/workspaceApi";

export function KnowledgeChip({ source, onOpen }: { source: KnowledgeSource; onOpen: (source: KnowledgeSource) => void }) {
  return <button type="button" title={`${source.name} · ${source.location}`} onClick={() => onOpen(source)} className="mx-0.5 inline-flex rounded border border-emerald-800 bg-emerald-950/50 px-1.5 py-0.5 text-[11px] text-emerald-300 hover:border-emerald-500">[{source.source_id}]</button>;
}

/**
 * One tool call, rendered inline where it happened in the reply.
 *
 * Not decoration: a ReAct agent that calls a tool and then answers leaves the
 * user staring at a pause with no explanation. Showing the call is also the
 * honest half of "辅助不代写" — the numbers in the reply are visibly derived
 * from a tool the user can inspect, not invented by the model.
 */
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

/**
 * A `[n]` marker rendered as a clickable superscript chip.
 *
 * Hover shows the title and abstract preview; click opens the DOI in a new tab.
 * The chip only exists because the gateway assigned this citation_id from a paper
 * a tool actually returned — a `[n]` with no matching entry (a number the model
 * invented) is rendered as inert text by `textWithChips` instead, so a fabricated
 * reference cannot masquerade as a real link.
 */
export function CitationChip({ citation }: { citation: Citation }) {
  const url = doiUrl(citation.doi);
  return (
    <span className="group relative inline-block">
      <button
        type="button"
        // No DOI means nothing to open; keep it a visible but inert marker rather
        // than a dead-looking link.
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

/**
 * Split reply text on `[n]` markers into text and citation chips.
 *
 * The regex is deliberately narrow — a bracketed run of digits and nothing else —
 * so ordinary bracketed text like "[注]" or "[12, 15]" is not half-consumed. A
 * number with no citation behind it stays literal text: the honest rendering of a
 * reference the tools never returned.
 */
export function textWithChips(text: string, lookup: Map<number, Citation>, sources?: Map<string, KnowledgeSource>, onSource?: (source: KnowledgeSource) => void): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const re = /\[(K\d+|\d+)\]/g;
  let last = 0;
  let key = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const citation = lookup.get(Number(match[1]));
    const source = sources?.get(match[1]);
    nodes.push(
      source && onSource ? <KnowledgeChip key={`k${key++}`} source={source} onOpen={onSource} /> : citation ? (
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
 * Inline routing badge at the top of an assistant bubble.
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
