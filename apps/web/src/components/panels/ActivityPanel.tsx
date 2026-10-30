"use client";

import { summarize, type ActivityRow } from "@/components/chatParts";
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
        <p className="font-mono text-[11px] text-zinc-500">
          {rows.length} 次工具调用 · 最新在上
        </p>
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
              <div
                key={`${part.toolCallId}-${i}`}
                className="rounded border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5"
              >
                <div className="flex items-center gap-2 font-mono text-[11px]">
                  <span
                    className={
                      part.state === "output-error"
                        ? "text-red-400"
                        : finished
                          ? "text-emerald-400"
                          : "animate-pulse text-zinc-300"
                    }
                  >
                    {part.state === "output-error" ? "✕" : finished ? "✓" : "●"}
                  </span>
                  <span className="text-zinc-200">{TOOL_LABELS[part.toolName] ?? part.toolName}</span>
                  <span className="ml-auto text-zinc-600">#{turn + 1}</span>
                </div>
                {finished && part.output != null && (
                  <p className="mt-1 truncate font-mono text-[10px] text-zinc-500">
                    {summarize(part.output)}
                  </p>
                )}
                <details className="mt-2 text-[10px] text-zinc-400">
                  <summary className="cursor-pointer">调用参数与结果</summary>
                  <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-zinc-950 p-2">{JSON.stringify({ input: part.input, output: part.output, error: part.errorText }, null, 2)}</pre>
                </details>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
