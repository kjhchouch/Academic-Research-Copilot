"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";
import type { ChatStatus, UIMessage } from "ai";
import type { KnowledgeSource, Project, ProjectDocument, Session } from "@/lib/workspaceApi";
import MessageBubble from "@/components/MessageBubble";

export default function ChatColumn({ messages, status, error, project, session, useKnowledge, onKnowledgeChange, selectedDocument, requestScope, onSaveNote, notesBusy, onOpenLibrary, onSend, onStop, onReload, onSource, turnNotice, onClearError, input, onInputChange }: {
  input: string;
  onInputChange: (value: string) => void;
  messages: UIMessage[];
  status: ChatStatus;
  error: Error | undefined;
  project: Project;
  session: Session;
  useKnowledge: boolean;
  onKnowledgeChange: (enabled: boolean) => void;
  selectedDocument: ProjectDocument | null;
  requestScope: string | null;
  onSaveNote: (message: UIMessage) => void;
  notesBusy: boolean;
  onOpenLibrary: () => void;
  onSend: (text: string) => boolean;
  onStop: () => void;
  onReload: () => void;
  onSource: (source: KnowledgeSource) => void;
  turnNotice: string | null;
  onClearError: () => void;
}) {
  const busy = status === "submitted" || status === "streaming";
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const railRef = useRef<HTMLDivElement>(null);
  const dragOffset = useRef<number | null>(null);
  const follow = useRef(true);
  const [scroll, setScroll] = useState({ position: 0, size: 1, visible: false });
  const scrollId = `chat-messages-${session.id}`;
  const measureScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const maximum = el.scrollHeight - el.clientHeight;
    const position = maximum > 0 ? Math.max(0, Math.min(1, el.scrollTop / maximum)) : 0;
    const size = Math.min(1, Math.max(el.clientHeight / Math.max(1, el.scrollHeight), 20 / Math.max(1, el.clientHeight - 24)));
    const visible = maximum > 1 && el.clientHeight > 48;
    setScroll((current) => current.position === position && current.size === size && current.visible === visible ? current : { position, size, visible });
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    const content = contentRef.current;
    const rail = railRef.current;
    if (!el || !content || !rail) return;
    const observer = new ResizeObserver(measureScroll);
    observer.observe(el);
    observer.observe(content);
    const wheel = (event: WheelEvent) => {
      if (!event.deltaY) return;
      event.preventDefault();
      el.scrollTop += event.deltaY * (event.deltaMode === 1 ? 20 : event.deltaMode === 2 ? el.clientHeight : 1);
    };
    rail.addEventListener("wheel", wheel, { passive: false });
    return () => { observer.disconnect(); rail.removeEventListener("wheel", wheel); };
  }, [measureScroll]);
  useEffect(() => {
    const el = scrollRef.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
  }, [messages, status]);
  const lastId = messages[messages.length - 1]?.id;

  function scrollToPointer(event: PointerEvent<HTMLDivElement>) {
    const el = scrollRef.current;
    if (!el || dragOffset.current === null) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const position = (event.clientY - bounds.top - dragOffset.current) / Math.max(1, bounds.height * (1 - scroll.size));
    el.scrollTop = Math.max(0, Math.min(1, position)) * (el.scrollHeight - el.clientHeight);
  }

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.focus({ preventScroll: true });
    event.currentTarget.setPointerCapture(event.pointerId);
    const bounds = event.currentTarget.getBoundingClientRect();
    const offset = event.clientY - bounds.top;
    const thumbHeight = bounds.height * scroll.size;
    const thumbTop = bounds.height * scroll.position * (1 - scroll.size);
    dragOffset.current = offset >= thumbTop && offset <= thumbTop + thumbHeight ? offset - thumbTop : thumbHeight / 2;
    scrollToPointer(event);
  }

  function scrollWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    const el = scrollRef.current;
    if (!el) return;
    const positions: Record<string, number> = {
      ArrowUp: el.scrollTop - 40, ArrowDown: el.scrollTop + 40,
      PageUp: el.scrollTop - el.clientHeight, PageDown: el.scrollTop + el.clientHeight,
      Home: 0, End: el.scrollHeight,
    };
    if (!(event.key in positions)) return;
    event.preventDefault();
    el.scrollTop = positions[event.key];
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!input.trim() || busy) return;
    follow.current = true;
    if (onSend(input.trim())) onInputChange("");
  }

  return (
    <main className="flex min-h-[20rem] min-w-0 flex-1 flex-col lg:min-h-0">
      <header className="flex shrink-0 items-start justify-between gap-3 border-b border-zinc-800 px-4 py-3 sm:px-5">
        <div className="min-w-0"><h1 className="truncate text-sm font-semibold">{session.title}</h1><p className="mt-1 truncate text-[11px] text-zinc-500">{project.name} · {session.id.slice(0, 8)} · {busy ? "生成中" : "本机历史"}</p>
          <p className="mt-1 break-words text-[11px] text-blue-300">{requestScope ?? (useKnowledge ? `资料范围：${selectedDocument?.name ?? "当前项目全部资料"}` : "普通对话 · 未使用项目资料")}{!requestScope && !useKnowledge && selectedDocument ? `（已选 ${selectedDocument.name}，尚未启用）` : ""}</p>
        </div>
        <button type="button" disabled={busy} onClick={onReload} className="shrink-0 rounded px-2 py-1 text-[11px] text-zinc-400 hover:bg-zinc-800">刷新历史</button>
      </header>
      <div className="relative min-h-0 flex-1">
        <div ref={scrollRef} id={scrollId} role="region" aria-label="对话消息" tabIndex={0} onScroll={() => { const el = scrollRef.current; if (el) follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100; measureScroll(); }} className="h-full overflow-y-auto overscroll-contain py-5 pr-8 pl-4 sm:pl-5">
          <div ref={contentRef} className="space-y-4">
            {messages.length === 0 && <div className="mx-auto max-w-md space-y-3 py-6 text-center"><p className="text-[11px] tracking-widest text-blue-300">从资料到阅读笔记</p><h2 className="text-base font-medium text-zinc-300">比较几篇论文，留下可核查的成果</h2><ol className="space-y-2 text-left text-xs leading-6 text-zinc-400"><li>1. 在资料库上传 PDF / Markdown，或导入明确标注的合成示例。</li><li>2. 选择 2–5 篇资料，确认范围后开始对比；点击 [K1] 核查原文。</li><li>3. 将回答保存为阅读笔记，在“成果”中编辑、保存并导出。</li></ol><button type="button" onClick={onOpenLibrary} className="rounded border border-blue-800 px-4 py-2 text-xs text-blue-300">打开资料库，开始对比</button><p className="text-[11px] leading-5 text-zinc-500">也可继续普通对话。只有明确启用资料问答或确认对比时，相关片段才会发送至模型。</p></div>}
            {messages.map((message) => <MessageBubble key={message.id} message={message} isStreaming={status === "streaming" && message.role === "assistant" && message.id === lastId} onSource={onSource} onSaveNote={onSaveNote} savingDisabled={busy || notesBusy} />)}
            {status === "submitted" && <p role="status" className="inline-block rounded-lg border border-zinc-800 bg-zinc-900 px-3.5 py-2.5 text-sm text-zinc-500">思考中…</p>}
          </div>
        </div>
        <div ref={railRef} hidden={!scroll.visible} role="scrollbar" aria-label="问答滚动位置" aria-controls={scrollId} aria-orientation="vertical" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(scroll.position * 100)} tabIndex={0}
          className="chat-scrollbar" style={{ "--scroll-top": `${scroll.position * (1 - scroll.size) * 100}%`, "--scroll-bottom": `${(1 - scroll.position) * (1 - scroll.size) * 100}%` } as CSSProperties}
          onPointerDown={startDrag} onPointerMove={scrollToPointer} onPointerUp={() => { dragOffset.current = null; }} onPointerCancel={() => { dragOffset.current = null; }} onLostPointerCapture={() => { dragOffset.current = null; }} onKeyDown={scrollWithKeyboard} />
      </div>
      {turnNotice && <p role="status" className="mx-4 mb-2 rounded border border-amber-900 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">{turnNotice}</p>}
      {error && <div role="alert" className="mx-4 mb-2 space-y-2 rounded border border-red-900 bg-red-950/30 px-3 py-2 text-xs text-red-300"><p className="break-words">{error.message}</p><div className="flex gap-3"><button type="button" disabled={busy} onClick={onReload} className="underline">重新加载服务器历史</button><button type="button" onClick={onClearError} className="underline">清除错误后继续提问</button></div></div>}
      <form onSubmit={submit} className="shrink-0 border-t border-zinc-800 p-3 sm:p-4">
        <label className="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" checked={useKnowledge} disabled={busy} onChange={(event) => onKnowledgeChange(event.target.checked)} className="accent-blue-500" />使用项目资料回答</label>
        <p className="mt-1 mb-3 text-[10px] leading-4 text-zinc-500">开启后将相关片段发送至已配置的模型；上传/索引留在本机</p>
        <div className="flex items-end gap-2"><textarea aria-label="提问内容" value={input} onChange={(event) => onInputChange(event.target.value)} placeholder="提出问题，或讨论你的研究思路…" disabled={busy} maxLength={8000} rows={2}
          onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
          className="min-w-0 flex-1 resize-none rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm outline-none placeholder:text-zinc-600 focus:border-zinc-500 disabled:opacity-50" />
          {busy ? <button type="button" onClick={onStop} className="rounded-lg border border-zinc-600 px-4 py-2.5 text-sm text-zinc-200 hover:bg-zinc-800">停止</button> : <button type="submit" disabled={!input.trim()} className="rounded-lg bg-zinc-100 px-4 py-2.5 text-sm font-medium text-zinc-900 hover:bg-white">发送</button>}
        </div>
      </form>
    </main>
  );
}
