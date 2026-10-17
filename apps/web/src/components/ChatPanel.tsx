"use client";

import { useChat } from "@ai-sdk/react";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { DynamicToolUIPart } from "ai";
import { API_BASE, CHAT_ENDPOINT, getThreadId, type UiContext } from "@/lib/api";
import { SseChatTransport } from "@/lib/sseTransport";

const PANELS = ["chat", "editor", "outline", "references"] as const;

/** Display names for the gateway's built-in tools. Unknown tools fall back to
 *  the raw name, so a new backend tool shows up without a frontend deploy. */
const TOOL_LABELS: Record<string, string> = {
  count_words: "统计字数",
};

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
    <div className="flex h-dvh flex-col bg-zinc-950 text-zinc-100">
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
            D3：后端是 LangGraph ReAct 图，工具调用会显示在回答里。D4 接意图路由，D6 接真实文献检索。
          </p>
        )}
        {messages.map((m) => (
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
                if (part.type === "text") return <span key={i}>{part.text}</span>;
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
