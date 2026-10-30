"use client";

import { useState } from "react";
import type { Session } from "@/lib/workspaceApi";
import NameForm from "@/components/NameForm";

export default function SessionsPanel({ sessions, selectedId, loading, busy, mutating, error, onSelect, onCreate, onRename, onRetry }: {
  sessions: Session[];
  selectedId: string | null;
  loading: boolean;
  busy: boolean;
  mutating: boolean;
  error: string | null;
  onSelect: (session: Session) => void;
  onCreate: (title: string, done: () => void) => void;
  onRename: (session: Session, title: string, done: () => void) => void;
  onRetry: () => void;
}) {
  const [form, setForm] = useState<"create" | Session | null>(null);
  const close = () => setForm(null);
  return (
    <div className="space-y-4">
      <p className="text-xs leading-5 text-zinc-500">只显示当前项目的会话。消息与执行结果由服务器保存。</p>
      {error && <div role="alert" className="space-y-2 text-xs text-red-300"><p>{error}</p><button type="button" onClick={onRetry} disabled={busy || mutating || loading} className="underline">刷新会话列表</button></div>}
      {loading ? <p role="status" className="text-sm text-zinc-400">正在加载会话…</p> : <>
        {form ? <NameForm key={form === "create" ? "create" : form.id} label={form === "create" ? "新会话标题" : "会话标题"} initial={form === "create" ? "" : form.title} busy={busy || mutating} onCancel={close} onSubmit={(title) => form === "create" ? onCreate(title, close) : onRename(form, title, close)} /> : <button type="button" onClick={() => setForm("create")} disabled={busy || mutating} className="w-full rounded-lg border border-zinc-600 py-2 text-sm hover:bg-zinc-800">新建会话</button>}
        {sessions.length === 0 && <p className="py-4 text-center text-xs text-zinc-500">还没有会话。创建后即可开始提问。</p>}
        <ul className="space-y-2">{sessions.map((session) => <li key={session.id} className={`rounded-lg border p-2 ${session.id === selectedId ? "border-blue-700 bg-blue-950/20" : "border-zinc-800 bg-zinc-900/40"}`}>
          <button type="button" aria-current={session.id === selectedId ? "true" : undefined} disabled={busy || mutating} onClick={() => onSelect(session)} className="w-full text-left">
            <span className="block truncate text-sm text-zinc-200">{session.title}</span>
            <time dateTime={session.updated_at} className="mt-1 block text-[10px] text-zinc-500">{new Date(session.updated_at).toLocaleString()}</time>
          </button>
          <button type="button" disabled={busy || mutating} onClick={() => setForm(session)} className="mt-2 text-xs text-zinc-400 hover:text-zinc-100">重命名</button>
        </li>)}</ul>
      </>}
    </div>
  );
}
