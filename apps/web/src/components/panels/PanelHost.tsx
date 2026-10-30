"use client";

import type { ReactNode } from "react";
import { PANEL_META, type PanelId } from "@/lib/panels";

/** Keep project editors mounted while changing panels; mobile panels never cover chat. */
export default function PanelHost({ activePanel, onClose, sessions, library, outcomes }: {
  activePanel: PanelId | null;
  onClose: () => void;
  sessions: ReactNode;
  library: ReactNode;
  outcomes: ReactNode;
}) {
  return (
    <aside aria-label="项目面板" className={`${activePanel ? "flex" : "hidden"} h-[42dvh] max-h-[50%] min-h-0 shrink-0 flex-col border-t border-zinc-800 bg-zinc-950 lg:h-auto lg:max-h-none lg:w-96 lg:border-t-0 lg:border-l xl:w-[26rem]`}>
      <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-2">
        <h2 className="text-sm font-semibold">{activePanel && PANEL_META[activePanel].name}</h2>
        <button type="button" onClick={onClose} className="rounded px-2 py-1 text-xs text-zinc-400 hover:bg-zinc-800">收起面板</button>
      </div>
      <div className={`${activePanel === "sessions" ? "block" : "hidden"} min-h-0 flex-1 overflow-y-auto p-4`}>{sessions}</div>
      <div className={`${activePanel === "references" ? "block" : "hidden"} min-h-0 flex-1 overflow-y-auto p-4`}>{library}</div>
      <div className={`${activePanel === "outcomes" ? "block" : "hidden"} min-h-0 flex-1 overflow-y-auto p-4`}>{outcomes}</div>
    </aside>
  );
}
