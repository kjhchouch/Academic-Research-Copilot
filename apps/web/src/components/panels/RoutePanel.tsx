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
                  <span
                    className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] ${g.chip}`}
                  >
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
