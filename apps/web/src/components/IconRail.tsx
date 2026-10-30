"use client";

import { PANELS, PANEL_META, type PanelId } from "@/lib/panels";

export default function IconRail({ activePanel, onSelect }: {
  activePanel: PanelId | null;
  onSelect: (id: PanelId) => void;
}) {
  return (
    <nav aria-label="项目工作区" className="flex shrink-0 justify-center gap-1 border-b border-zinc-800 p-2 lg:w-20 lg:flex-col lg:justify-start lg:border-r lg:border-b-0">
      {PANELS.map((id) => (
        <button key={id} type="button" title={PANEL_META[id].hint} aria-pressed={id === activePanel}
          onClick={() => onSelect(id)} className={`flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-xs transition lg:flex-col lg:px-1 ${id === activePanel ? "bg-zinc-100 text-zinc-900" : "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"}`}>
          <PanelIcon id={id} />{PANEL_META[id].name}
        </button>
      ))}
    </nav>
  );
}

function PanelIcon({ id }: { id: PanelId }) {
  return (
    <svg aria-hidden="true" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      {id === "sessions" ? <path d="M21 12a8 8 0 0 1-8 8H4l2-3a8 8 0 1 1 15-5z" />
        : id === "references" ? <><path d="M4 5a2 2 0 0 1 2-2h9v18H6a2 2 0 0 1-2-2z" /><path d="M15 3h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-3" /></>
          : <><path d="M8 6h13M8 12h13M8 18h13" /><path d="M3 6h1M3 12h1M3 18h1" /></>}
    </svg>
  );
}
