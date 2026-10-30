"use client";

import { useState } from "react";
import type { NotesController } from "@/lib/useProjectNotes";
import type { KnowledgeSource } from "@/lib/workspaceApi";
import { noteFilename, noteMarkdown, unresolvedMarkers } from "@/lib/noteExport";
import ResearchMarkdown from "@/components/ResearchMarkdown";

export default function NotesPanel({ notes, navigate, onSource, onOpenLibrary }: {
  notes: NotesController;
  navigate: (action: () => void) => void;
  onSource: (source: KnowledgeSource) => void;
  onOpenLibrary: () => void;
}) {
  const [preview, setPreview] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const { selected, title, body, dirty, saving, loading, error, conflict } = notes;
  const missing = selected ? unresolvedMarkers({ ...selected, body }) : [];

  function download() {
    if (!selected || dirty || saving) return;
    const url = URL.createObjectURL(new Blob([noteMarkdown(selected)], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = noteFilename(selected.title);
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  if (loading) return <p role="status" className="text-xs text-zinc-400">正在加载阅读笔记…</p>;
  return <div className="space-y-4">
    <p className="text-xs leading-5 text-zinc-500">把已完成的回答保存成阅读笔记，在这里整理和导出。正文可编辑；采纳时的来源快照只读，不会随原资料删除而消失。</p>
    <div className="flex flex-wrap gap-2">
      <button type="button" disabled={saving} onClick={() => navigate(() => void notes.create({ title: "未命名阅读笔记" }))} className="rounded border border-zinc-600 px-3 py-2 text-xs">新建阅读笔记</button>
      <button type="button" disabled={saving} onClick={() => navigate(notes.reload)} className="rounded border border-zinc-700 px-3 py-2 text-xs text-zinc-400">刷新笔记</button>
    </div>
    {error && <div role="alert" className="space-y-2 rounded border border-amber-800 p-3 text-xs leading-5 text-amber-200">
      <p>{conflict ? `版本冲突：${error} 本地草稿仍保留。` : error}</p>
      {conflict && <button type="button" disabled={saving} onClick={() => void notes.rebase()} className="underline">读取最新版本，保留草稿以核对</button>}
    </div>}
    {notes.items.length > 0 && <label className="block text-xs text-zinc-400">项目阅读笔记
      <select aria-label="选择阅读笔记" value={selected?.id ?? ""} disabled={saving} onChange={(event) => { const next = notes.items.find((n) => n.id === event.target.value) ?? null; navigate(() => notes.select(next)); }} className="mt-2 w-full rounded border border-zinc-700 bg-zinc-950 p-2 text-xs text-zinc-100">
        {notes.items.map((note) => <option key={note.id} value={note.id}>{note.title}</option>)}
      </select>
    </label>}
    {!selected ? <div className="rounded border border-dashed border-zinc-700 p-4 text-xs leading-6 text-zinc-400"><p>还没有阅读笔记。先选几篇资料进行对比，再点击回答下方的“保存为阅读笔记”。</p><button type="button" onClick={onOpenLibrary} className="mt-2 text-blue-300 underline">前往资料库</button></div> : <>
      <div className="flex justify-between gap-2 text-[11px] text-zinc-400"><span>v{selected.version} · 本机保存</span><span className={dirty ? "text-amber-300" : "text-zinc-400"}>{dirty ? "有未保存修改" : "已保存"}</span></div>
      <fieldset disabled={saving} className="min-w-0 space-y-3">
        <label className="block text-xs text-zinc-400">笔记标题<input aria-label="笔记标题" value={title} onChange={(event) => notes.setTitle(event.target.value)} maxLength={200} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 p-2 text-sm text-zinc-100" /></label>
        <div className="flex gap-3 text-xs"><button type="button" aria-pressed={!preview} onClick={() => setPreview(false)} className={!preview ? "text-blue-300" : "text-zinc-500"}>编辑正文</button><button type="button" aria-pressed={preview} onClick={() => setPreview(true)} className={preview ? "text-blue-300" : "text-zinc-500"}>预览排版</button></div>
        {preview ? <div className="rounded border border-zinc-800 bg-zinc-900/50 p-3"><ResearchMarkdown text={body || "尚无正文。"} sources={selected.sources} citations={selected.citations} onSource={onSource} /></div> : <textarea aria-label="阅读笔记正文" value={body} onChange={(event) => notes.setBody(event.target.value)} maxLength={100000} rows={14} placeholder="整理研究问题、方法、结论与局限；保留 [K1] 等来源编号。" className="w-full resize-y rounded border border-zinc-700 bg-zinc-900 p-3 font-mono text-xs leading-6 text-zinc-200" />}
      </fieldset>
      <p className="text-[11px] leading-5 text-amber-200/80">模型归纳与人工修改均需核查；有来源编号不代表结论已验证。修改正文后请重新核对引用是否仍支持结论。</p>
      {missing.length > 0 && <p role="alert" className="text-xs text-amber-300">未附来源的编号：{missing.map((id) => `[${id}]`).join("、")}。导出时也会保留此提醒。</p>}
      <div className="flex flex-wrap gap-2">
        <button type="button" disabled={saving || !dirty || conflict || !title.trim()} onClick={() => void notes.save()} className="rounded bg-zinc-100 px-3 py-2 text-xs text-zinc-900">{saving ? "保存中…" : "保存笔记"}</button>
        <button type="button" disabled={saving || dirty} onClick={download} className="rounded border border-blue-800 px-3 py-2 text-xs text-blue-300">导出 Markdown</button>
        <button type="button" disabled={saving || dirty} onClick={() => setConfirmDelete(true)} className="rounded border border-red-900 px-3 py-2 text-xs text-red-300">删除笔记</button>
      </div>
      {dirty && <p className="text-[11px] text-zinc-500">保存后才能导出或删除；导出包含来源快照与原会话标识。</p>}
      {confirmDelete && <div role="alert" className="space-y-2 rounded border border-red-900 p-3 text-xs"><p>确认删除“{selected.title}”？原聊天和资料不受影响。</p><div className="flex gap-3"><button type="button" disabled={saving || dirty} onClick={() => { setConfirmDelete(false); void notes.remove(); }} className="text-red-300">确认删除笔记</button><button type="button" disabled={saving} onClick={() => setConfirmDelete(false)}>取消</button></div></div>}
      {selected.sources.length > 0 && <section className="space-y-2 border-t border-zinc-800 pt-3"><h3 className="text-xs font-medium">原文证据 · 采纳时的片段快照</h3>{selected.sources.map((source) => <details key={source.source_id} className="rounded border border-zinc-800 p-2 text-xs"><summary className="cursor-pointer break-words text-blue-300">[{source.source_id}] {source.name} · {source.location}</summary><blockquote className="mt-2 whitespace-pre-wrap break-words border-l-2 border-zinc-700 pl-2 leading-6 text-zinc-400">{source.text}</blockquote><button type="button" onClick={() => onSource(source)} className="mt-2 underline">定位当前原文</button></details>)}<p className="text-[10px] leading-5 text-zinc-500">删除或重新解析资料后可能无法定位旧片段；快照仅记录当时提供给模型的文本。</p></section>}
      {selected.citations.length > 0 && <details className="text-xs text-zinc-400"><summary className="cursor-pointer">公开参考文献 · {selected.citations.length}</summary><ul className="mt-2 space-y-2">{selected.citations.map((c) => <li key={c.citation_id}>[{c.citation_id}] {c.title} · {c.doi ?? "无 DOI"}</li>)}</ul></details>}
      {selected.origin_session_id && <p className="break-all text-[10px] leading-5 text-zinc-500">采纳自会话 {selected.origin_session_id.slice(0, 8)} · 消息 {selected.origin_message_id?.slice(0, 8)}</p>}
      {dirty && <details className="text-xs text-zinc-500"><summary className="cursor-pointer">对照已加载的服务器版本</summary><p className="mt-2">{selected.title}</p><pre className="mt-2 whitespace-pre-wrap break-words font-sans leading-5">{selected.body}</pre></details>}
    </>}
  </div>;
}
