"use client";

import { useEffect, useRef, useState } from "react";
import { errorMessage, workspaceApi, type DocumentDetail, type KnowledgeSource, type ProjectDocument } from "@/lib/workspaceApi";

export type PreviewTarget = { documentId: string; chunkId?: string; source?: KnowledgeSource; nonce: number };

export default function DocumentPreview({ projectId, target, selectionLocked, onSelect, onClose }: {
  projectId: string;
  target: PreviewTarget;
  selectionLocked: boolean;
  onSelect: (document: ProjectDocument) => void;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [selectedChunk, setSelectedChunk] = useState<string | null>(null);
  const container = useRef<HTMLElement | null>(null);
  useEffect(() => { container.current?.scrollIntoView({ block: "nearest" }); }, []);
  useEffect(() => {
    const controller = new AbortController();
    workspaceApi.document(projectId, target.documentId, controller.signal).then((result) => {
      if (!controller.signal.aborted) { setDetail(result); setError(null); }
    }).catch((err: unknown) => { if (!controller.signal.aborted) setError(errorMessage(err)); });
    return () => controller.abort();
  }, [projectId, target.documentId, revision]);

  const original = detail?.chunks.find((c) => c.id === target.chunkId);
  const snapshot = Boolean(detail && target.source && !original && !selectedChunk);
  const chunk = detail?.chunks.find((c) => c.id === selectedChunk) ?? original ?? (snapshot ? target.source : detail?.chunks[0]);
  return (
    <section ref={container} aria-label="资料片段预览" className="space-y-3 rounded-lg border border-blue-900 bg-blue-950/10 p-3">
      <div className="flex items-center justify-between gap-2"><h3 className="min-w-0 truncate text-sm font-medium">{detail?.document.name ?? "资料预览"}</h3><button type="button" onClick={onClose} className="shrink-0 text-xs text-zinc-400">关闭预览</button></div>
      {error ? <div role="alert" className="space-y-2 text-xs text-red-300"><p>{error}</p><button type="button" className="underline" onClick={() => { setError(null); setDetail(null); setRevision((n) => n + 1); }}>重试预览</button></div>
        : !detail ? <p role="status" className="text-xs text-zinc-400">正在读取原文片段…</p> : <>
          <div className="flex flex-wrap gap-3 text-xs">
            <button type="button" disabled={selectionLocked} onClick={() => onSelect(detail.document)} className="text-blue-300 underline">选择此文档用于提问</button>
            <a href={workspaceApi.fileUrl(projectId, detail.document.id)} target="_blank" rel="noopener noreferrer" className="text-zinc-400 underline">下载原文件</a>
          </div>
          {detail.chunks.length > 0 && <label className="block text-xs text-zinc-400">定位片段
            <select value={snapshot ? "snapshot" : chunk?.id ?? ""} onChange={(event) => setSelectedChunk(event.target.value)} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 p-2 text-xs">
              {snapshot && <option value="snapshot">本轮引用快照（原片段已变更）</option>}
              {detail.chunks.map((c, i) => <option key={c.id} value={c.id}>{i + 1}. {c.location || (c.page ? `第 ${c.page} 页` : "文本片段")}</option>)}
            </select>
          </label>}
          {snapshot && <p className="text-xs text-amber-300">原文档的索引已变更。以下是该轮保存的引用快照，不代表当前索引。</p>}
          {chunk ? <div className="space-y-2">
            <p className="break-words text-[11px] text-blue-300">{target.source && `[${target.source.source_id}] · `}{chunk.location}{chunk.page !== null && ` · 第 ${chunk.page} 页`}{chunk.line_start !== null && ` · 行 ${chunk.line_start}–${chunk.line_end ?? chunk.line_start}`}</p>
            <p className="max-h-80 overflow-y-auto whitespace-pre-wrap break-words rounded bg-zinc-950 p-3 text-xs leading-6 text-zinc-300">{chunk.text}</p>
          </div> : <p className="text-xs text-zinc-500">此文档还没有可用片段。待解析完成后重新打开预览；扫描 PDF 需要先做 OCR。</p>}
        </>}
    </section>
  );
}
