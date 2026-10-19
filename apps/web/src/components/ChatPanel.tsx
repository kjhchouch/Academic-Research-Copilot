"use client";

import { useChat } from "@ai-sdk/react";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { DynamicToolUIPart, UIMessage } from "ai";
import {
  API_BASE,
  CHAT_ENDPOINT,
  getThreadId,
  doiUrl,
  type Citation,
  type UiContext,
} from "@/lib/api";
import { SseChatTransport } from "@/lib/sseTransport";

const PANELS = ["chat", "editor", "outline", "references"] as const;

/** Display names for the gateway's built-in tools. Unknown tools fall back to
 *  the raw name, so a new backend tool shows up without a frontend deploy. */
const TOOL_LABELS: Record<string, string> = {
  count_words: "统计字数",
  search_papers: "检索文献",
  get_paper: "读取论文",
  format_citation: "生成引用",
};

/**
 * The citations carried by one message, keyed by id and in numeric order.
 *
 * Scoped per message on purpose: citation_id restarts at 1 every turn, so a [1]
 * in one answer and a [1] in another are different papers. Reading them from the
 * same message the [n] appears in is what keeps the chip pointing at the right
 * entry. The parts are the `data-citation` chunks the transport turned the
 * gateway's `citation` events into.
 */
function citationsOf(message: UIMessage): Citation[] {
  return message.parts
    .filter((part) => part.type === "data-citation")
    .map((part) => (part as unknown as { data: Citation }).data)
    .filter((c) => c && typeof c.citation_id === "number")
    .sort((a, b) => a.citation_id - b.citation_id);
}

function byId(citations: Citation[]): Map<number, Citation> {
  return new Map(citations.map((c) => [c.citation_id, c]));
}

/**
 * Citation ids the answer actually references as [n] in its text.
 *
 * The gateway registers every paper the tools returned -- it assigns ids before
 * the model writes, so it cannot know in advance which will be cited. A broad
 * search can therefore register dozens of papers the model then ignores. The
 * panel filters to this set: references = works cited, not works skimmed. Same
 * narrow regex as `textWithChips`, so a marker becomes a chip and appears here
 * under exactly one condition.
 */
function citedIds(message: UIMessage): Set<number> {
  const ids = new Set<number>();
  for (const part of message.parts) {
    if (part.type === "text") {
      for (const match of part.text.matchAll(/\[(\d+)\]/g)) ids.add(Number(match[1]));
    }
  }
  return ids;
}

/** One-line "authors · year · venue" for a chip tooltip or panel entry. */
function citationMeta(c: Citation): string {
  const authors = c.authors.length ? `${c.authors.slice(0, 3).join(", ")}${c.authors.length > 3 ? " 等" : ""}` : "";
  return [authors, c.year, c.venue].filter(Boolean).join(" · ");
}

function summarize(output: unknown): string {
  if (output == null) return "";
  const text = typeof output === "string" ? output : JSON.stringify(output);
  return text.length > 60 ? `${text.slice(0, 60)}…` : text;
}

/**
 * One tool call, rendered inline where it happened in the reply.
 *
 * Not decoration: a ReAct agent that calls a tool and then answers leaves the
 * user staring at a pause with no explanation. Showing the call is also the
 * honest half of "辅助不代写" — the numbers in the reply are visibly derived
 * from a tool the user can inspect, not invented by the model.
 */
function ToolChip({ part }: { part: DynamicToolUIPart }) {
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
 * a tool actually returned -- that is the "辅助不代写, 引用可核实" half of the
 * product made visible. A `[n]` with no matching entry (a number the model
 * invented) is rendered as inert text by `textWithChips` instead, so a fabricated
 * reference cannot masquerade as a real link.
 */
function CitationChip({ citation }: { citation: Citation }) {
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
 * The regex is deliberately narrow -- a bracketed run of digits and nothing else
 * -- so ordinary bracketed text like "[注]" or "[12, 15]" is not half-consumed. A
 * number with no citation behind it stays literal text: the honest rendering of a
 * reference the tools never returned.
 */
function textWithChips(text: string, lookup: Map<number, Citation>): React.ReactNode[] {
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


// The thread id never changes within a session, so there is nothing to
// subscribe to — but useSyncExternalStore still requires a stable function.
function subscribeToThreadId() {
  return () => {};
}

// SSR and the hydration pass both have no window, so both must render the same
// empty value or React reports a hydration mismatch. React then re-renders with
// the real client snapshot. This is what replaces a `mounted` flag: setting
// state synchronously inside an effect is an error under React 19's
// react-hooks/set-state-in-effect rule.
function getServerThreadId() {
  return "";
}

export default function ChatPanel() {
  const [input, setInput] = useState("");
  const [backendUp, setBackendUp] = useState<boolean | null>(null);

  // Simulated editor state. From D4 onward these are real routing features:
  // "格式不对" typed in the references panel needs no NLP to disambiguate.
  const [uiContext, setUiContext] = useState<UiContext>({
    active_panel: "chat",
    paper_stage: "topic",
  });

  const threadId = useSyncExternalStore(
    subscribeToThreadId,
    getThreadId,
    getServerThreadId,
  );

  // Built once — handing useChat a new transport every render would reset it.
  // Nothing dynamic lives on it: ui_context travels per request instead, via
  // sendMessage's body option. (Mirroring it into a ref during render is what
  // React 19's react-hooks/refs rule forbids, and it could lag a render anyway.)
  const [transport] = useState(() => new SseChatTransport({ api: CHAT_ENDPOINT }));

  const { messages, sendMessage, status, error, stop, clearError } = useChat({
    // useChat keys its internal Chat instance on this id, so the "" -> UUID
    // transition after hydration rebuilds it once. That is safe because it
    // happens before any message exists; verified against
    // @ai-sdk/react's useChat (chatKey = options.id, useMemo([chatKey])).
    id: threadId || undefined,
    transport,
  });

  const busy = status === "submitted" || status === "streaming";
  const endRef = useRef<HTMLDivElement>(null);

  // The reference panel follows the newest answer that actually cited something,
  // so a follow-up turn with no literature does not blank out the previous
  // answer's references.
  const activeCitations = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const all = citationsOf(messages[i]);
      if (!all.length) continue;
      // Most recent answer that retrieved literature. Show only what it cites as
      // [n], not everything the tools returned. While streaming this is briefly
      // empty until the model writes its markers -- correct, since nothing is
      // referenced yet.
      const cited = citedIds(messages[i]);
      return all.filter((c) => cited.has(c.citation_id));
    }
    return [] as Citation[];
  })();

  useEffect(() => {
    fetch(`${API_BASE}/health`)
      .then((r) => setBackendUp(r.ok))
      .catch(() => setBackendUp(false));
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, status]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    clearError();
    // ui_context is sent per request so the value the agent sees is the one the
    // editor had at click time, not the one captured when the transport was built.
    await sendMessage({ text }, { body: { ui_context: uiContext } });
  }

  return (
    <div className="flex h-dvh bg-zinc-950 text-zinc-100">
      <div className="flex min-w-0 flex-1 flex-col">
      <header className="flex items-center justify-between border-b border-zinc-800 px-5 py-3">
        <div>
          <h1 className="text-sm font-semibold">Academic Research Copilot</h1>
          <p className="font-mono text-[11px] text-zinc-500">
            thread {threadId ? threadId.slice(0, 8) : "--------"}
            <span className="ml-2 text-zinc-600">· {status}</span>
          </p>
        </div>
        <BackendBadge up={backendUp} />
      </header>

      {/* D4: this bar becomes the skill chips / candidate-skill display. */}
      <div className="flex items-center gap-2 border-b border-zinc-800/60 px-5 py-2">
        <span className="text-[11px] text-zinc-500">ui_context</span>
        <div className="flex gap-1">
          {PANELS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setUiContext((c) => ({ ...c, active_panel: p }))}
              className={`rounded px-2 py-0.5 text-[11px] transition ${
                uiContext.active_panel === p
                  ? "bg-zinc-100 text-zinc-900"
                  : "bg-zinc-800/70 text-zinc-400 hover:bg-zinc-700"
              }`}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto px-5 py-6">
        {messages.length === 0 && (
          <p className="mx-auto max-w-md text-center text-sm text-zinc-500">
            检索文献后，正文里的 [n] 是可点击的引用（悬浮看标题摘要，点开跳 DOI），右侧列出参考文献。
            试试「找 5 篇 contrastive learning 的代表性论文」。
          </p>
        )}
        {messages.map((m) => {
          const lookup = byId(citationsOf(m));
          return (
            <div
              key={m.id}
              className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
            >
              <div
                className={`max-w-[80%] whitespace-pre-wrap rounded-lg px-3.5 py-2.5 text-sm leading-relaxed ${
                  m.role === "user"
                    ? "bg-blue-600 text-white"
                    : "border border-zinc-800 bg-zinc-900 text-zinc-200"
                }`}
              >
                {m.parts.map((part, i) => {
                  if (part.type === "text")
                    return <span key={i}>{textWithChips(part.text, lookup)}</span>;
                  if (part.type === "dynamic-tool")
                    return <ToolChip key={`${part.toolCallId}-${i}`} part={part} />;
                  return null;
                })}
                {/* Caret only on the message currently being streamed. */}
                {m.role === "assistant" &&
                  m.id === messages[messages.length - 1]?.id &&
                  status === "streaming" && (
                    <span className="ml-0.5 animate-pulse text-zinc-500">▍</span>
                  )}
              </div>
            </div>
          );
        })}
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
            <button
              type="button"
              onClick={() => void stop()}
              className="rounded-lg border border-zinc-600 px-5 py-2.5 text-sm font-medium text-zinc-300 transition hover:bg-zinc-800"
            >
              停止
            </button>
          ) : (
            <button
              type="submit"
              disabled={!input.trim()}
              className="rounded-lg bg-zinc-100 px-5 py-2.5 text-sm font-medium text-zinc-900 transition hover:bg-white disabled:opacity-40"
            >
              发送
            </button>
          )}
        </div>
      </form>
      </div>

      <ReferencesPanel citations={activeCitations} />
    </div>
  );
}

/**
 * The right-hand reference list for the current answer.
 *
 * Hidden below `lg` so the chat stays usable on a narrow window; the [n] chips
 * still carry the same title/abstract on hover there, so nothing is lost, only
 * the always-visible panel. Every entry links to its DOI -- the same
 * "verifiable, not invented" guarantee the chip gives, in list form.
 */
function ReferencesPanel({ citations }: { citations: Citation[] }) {
  return (
    <aside className="hidden w-80 shrink-0 flex-col border-l border-zinc-800 lg:flex">
      <div className="border-b border-zinc-800 px-4 py-3">
        <h2 className="text-sm font-semibold">参考文献</h2>
        <p className="font-mono text-[11px] text-zinc-500">
          {citations.length} 条 · 本次回答
        </p>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto px-4 py-3">
        {citations.length === 0 ? (
          <p className="text-xs leading-relaxed text-zinc-600">
            检索文献后，可点击的引用会出现在这里。试试「找 5 篇 contrastive learning
            的代表性论文」。
          </p>
        ) : (
          citations.map((c) => {
            const url = doiUrl(c.doi);
            return (
              <div
                key={c.citation_id}
                className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-2.5"
              >
                <div className="flex gap-2">
                  <span className="mt-px inline-flex h-[15px] min-w-[15px] shrink-0 items-center justify-center rounded bg-blue-500/20 px-1 text-[10px] leading-none text-blue-300">
                    {c.citation_id}
                  </span>
                  <div className="min-w-0">
                    <p className="text-xs font-medium leading-snug text-zinc-100">{c.title}</p>
                    <p className="mt-0.5 text-[10px] text-zinc-400">{citationMeta(c)}</p>
                    {url ? (
                      <a
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="mt-1 inline-block truncate font-mono text-[10px] text-blue-400 hover:underline"
                      >
                        {c.doi}
                      </a>
                    ) : (
                      <span className="mt-1 inline-block font-mono text-[10px] text-zinc-600">
                        无 DOI
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}

function BackendBadge({ up }: { up: boolean | null }) {
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
