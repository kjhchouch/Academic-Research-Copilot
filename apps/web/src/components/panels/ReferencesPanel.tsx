"use client";

import { doiUrl, type Citation } from "@/lib/api";
import { citationMeta } from "@/components/chatParts";

/**
 * Cited-only reference list for the current answer. Extracted from the old
 * ChatPanel; the `hidden lg:flex` responsive wrapper moved up to PanelHost so
 * every panel shares one collapse rule. Every entry links to its DOI — the same
 * "verifiable, not invented" guarantee the inline [n] chip gives, in list form.
 */
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
    </div>
  );
}
