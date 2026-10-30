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
