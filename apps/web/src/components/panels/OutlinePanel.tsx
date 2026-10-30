"use client";

import { useState } from "react";
import type { OutlineController } from "@/lib/useProjectOutline";

export default function OutlinePanel({ outline, canDiscuss, onDiscuss }: {
  outline: OutlineController;
  canDiscuss: boolean;
  onDiscuss: () => void;
}) {
  const [confirmReload, setConfirmReload] = useState(false);
  const { sections, setSections, saved, loading, saving, error, dirty, conflict } = outline;
  function move(index: number, delta: number) {
    const next = [...sections];
    [next[index], next[index + delta]] = [next[index + delta], next[index]];
    setSections(next);
  }
  if (loading) return <p role="status" className="text-sm text-zinc-400">正在加载项目大纲…</p>;
  if (!saved) return <div role="alert" className="space-y-3 text-xs text-red-300"><p>{error}</p><button type="button" onClick={outline.reload} className="underline">重试加载大纲</button></div>;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between text-xs text-zinc-400"><span>选题阶段 · v{saved.version}</span><span className={dirty ? "text-amber-300" : "text-emerald-400"}>{dirty ? "有未保存修改" : "已与本机同步"}</span></div>
      {error && <div role="alert" className="space-y-2 rounded border border-amber-800 bg-amber-950/30 p-3 text-xs leading-5 text-amber-200">
        <p>{conflict ? `版本冲突：${error} 本地修改仍保留。` : error}</p>
        {conflict && <button type="button" disabled={saving} onClick={() => void outline.rebase()} className="underline">读取最新版本，保留本地草稿以核对</button>}
      </div>}
      {sections.length === 0 && <p className="rounded border border-dashed border-zinc-700 px-3 py-6 text-center text-sm text-zinc-500">这个项目还没有大纲。添加第一个章节，不会自动生成论文。</p>}
      <fieldset disabled={saving} className="space-y-3">
        <legend className="sr-only">大纲章节</legend>
        {sections.map((section, index) => <div key={section.id} className="rounded-lg border border-zinc-800 bg-zinc-900 p-2.5">
          <label className="flex items-center gap-2 text-xs text-zinc-500"><span>{index + 1}.</span><input aria-label={`第 ${index + 1} 章标题`} value={section.title} maxLength={200} onChange={(event) => setSections(sections.map((item) => item.id === section.id ? { ...item, title: event.target.value } : item))} className="min-w-0 flex-1 rounded border border-zinc-700 bg-zinc-950 px-2 py-2 text-sm text-zinc-100 outline-none focus:border-blue-500" /></label>
          <div className="mt-2 flex justify-end gap-3 text-xs text-zinc-400">
            <button type="button" aria-label={`上移第 ${index + 1} 章`} disabled={index === 0} onClick={() => move(index, -1)}>上移</button>
            <button type="button" aria-label={`下移第 ${index + 1} 章`} disabled={index === sections.length - 1} onClick={() => move(index, 1)}>下移</button>
            <button type="button" aria-label={`移除第 ${index + 1} 章`} onClick={() => setSections(sections.filter((item) => item.id !== section.id))} className="text-red-300">移除</button>
          </div>
        </div>)}
        <button type="button" disabled={sections.length >= 100} onClick={() => setSections([...sections, { id: crypto.randomUUID(), title: "" }])} className="w-full rounded border border-dashed border-zinc-700 py-2 text-xs text-zinc-300">添加章节</button>
      </fieldset>
      <div className="flex flex-wrap gap-2">
        <button type="button" disabled={!dirty || saving || conflict} onClick={() => void outline.save()} className="rounded bg-zinc-100 px-3 py-2 text-xs text-zinc-900">{saving ? "保存中…" : "保存大纲"}</button>
        <button type="button" disabled={saving} onClick={() => dirty ? setConfirmReload(true) : outline.reload()} className="rounded border border-zinc-700 px-3 py-2 text-xs">重新加载</button>
        <button type="button" disabled={!canDiscuss || !sections.length || saving || sections.some((s) => !s.title.trim())} onClick={onDiscuss} className="rounded border border-blue-800 px-3 py-2 text-xs text-blue-300">发送大纲讨论</button>
      </div>
      {confirmReload && <div role="alert" className="space-y-2 rounded border border-amber-700 p-3 text-xs"><p>放弃本地未保存修改，加载服务器最新大纲？</p><div className="flex gap-3"><button type="button" onClick={() => { setConfirmReload(false); outline.reload(); }} className="text-amber-300">确认放弃并加载</button><button type="button" onClick={() => setConfirmReload(false)}>保留修改</button></div></div>}
      <p className="text-[11px] leading-5 text-zinc-500">讨论会把当前大纲作为明确的用户消息发送给模型，用于结构建议，不会自动生成整篇论文。</p>
      {saved.sections.length > 0 && dirty && <details className="text-xs text-zinc-500"><summary className="cursor-pointer">对照已加载的服务器版本</summary><ol className="mt-2 list-inside list-decimal space-y-1">{saved.sections.map((s) => <li key={s.id}>{s.title}</li>)}</ol></details>}
    </div>
  );
}
