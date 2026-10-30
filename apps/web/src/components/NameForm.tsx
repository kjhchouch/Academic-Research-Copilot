"use client";

import { useState } from "react";

export default function NameForm({ label, initial = "", busy, onSubmit, onCancel }: {
  label: string;
  initial?: string;
  busy: boolean;
  onSubmit: (value: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <form onSubmit={(event) => { event.preventDefault(); if (value.trim() && !busy) onSubmit(value.trim()); }} className="flex flex-wrap items-end gap-2">
      <label className="min-w-0 flex-1 text-xs text-zinc-400">
        {label}
        <input autoFocus value={value} onChange={(event) => setValue(event.target.value)} maxLength={120} required disabled={busy}
          className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-blue-500" />
      </label>
      <button type="submit" disabled={busy || !value.trim()} className="rounded bg-zinc-100 px-3 py-2 text-xs text-zinc-900">{busy ? "保存中…" : "保存"}</button>
      <button type="button" onClick={onCancel} disabled={busy} className="rounded border border-zinc-700 px-3 py-2 text-xs">取消</button>
    </form>
  );
}
