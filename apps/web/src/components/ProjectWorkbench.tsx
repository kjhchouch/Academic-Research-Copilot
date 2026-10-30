"use client";

import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import type { UIMessage } from "ai";
import type { PanelId } from "@/lib/panels";
import { errorMessage, recalledSession, rememberSession, workspaceApi, type KnowledgeSource, type Project, type ProjectDocument, type Session } from "@/lib/workspaceApi";
import { useProjectOutline } from "@/lib/useProjectOutline";
import { useProjectNotes } from "@/lib/useProjectNotes";
import IconRail from "@/components/IconRail";
import ChatSessionLoader, { type ChatSessionHandle } from "@/components/ChatSession";
import PanelHost from "@/components/panels/PanelHost";
import SessionsPanel from "@/components/panels/SessionsPanel";
import LibraryPanel, { type LibraryHandle } from "@/components/panels/LibraryPanel";
import OutlinePanel from "@/components/panels/OutlinePanel";
import NotesPanel from "@/components/panels/NotesPanel";

export type WorkbenchHandle = { navigate: (action: () => void) => void };

export default function ProjectWorkbench({ ref, project, locked, onBusyChange }: {
  ref?: Ref<WorkbenchHandle>;
  project: Project;
  locked: boolean;
  onBusyChange: (busy: boolean) => void;
}) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [mutating, setMutating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [activePanel, setActivePanel] = useState<PanelId | null>("sessions");
  const [outcomeTab, setOutcomeTab] = useState<"notes" | "outline">("notes");
  const [selectedDocument, setSelectedDocument] = useState<ProjectDocument | null>(null);
  const [pendingNavigation, setPendingNavigation] = useState<{ action: () => void } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const mutation = useRef<AbortController | null>(null);
  const refreshRequest = useRef<AbortController | null>(null);
  const chat = useRef<ChatSessionHandle | null>(null);
  const library = useRef<LibraryHandle | null>(null);
  const guardDialog = useRef<HTMLDialogElement | null>(null);
  const outline = useProjectOutline(project.id);
  const notes = useProjectNotes(project.id);
  const session = sessions.find((s) => s.id === selectedId) ?? null;
  const dirty = outline.dirty || notes.dirty;
  const saving = outline.saving || notes.saving;
  const conflict = outline.conflict || notes.conflict;

  useEffect(() => {
    if (pendingNavigation && guardDialog.current && !guardDialog.current.open) guardDialog.current.showModal();
  }, [pendingNavigation]);

  useEffect(() => {
    const controller = new AbortController();
    workspaceApi.sessions(project.id, controller.signal).then((items) => {
      if (controller.signal.aborted) return;
      const scoped = items.filter((s) => s.project_id === project.id);
      const remembered = recalledSession(project.id);
      const selected = scoped.find((s) => s.id === remembered) ?? scoped[0];
      setSessions(scoped);
      setSelectedId(selected?.id ?? null);
      if (selected) rememberSession(project.id, selected.id);
      setLoading(false);
      setError(null);
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) { setLoading(false); setError(errorMessage(err)); }
    });
    return () => controller.abort();
  }, [project.id, revision]);
  useEffect(() => () => { mutation.current?.abort(); refreshRequest.current?.abort(); }, []);

  function guardEdits(action: () => void) {
    if (saving) return;
    if (dirty) setPendingNavigation({ action });
    else action();
  }
  function navigate(action: () => void) {
    if (locked || busy || chat.current?.isBusy() || mutating || saving) return;
    guardEdits(action);
  }
  useImperativeHandle(ref, () => ({ navigate }));

  function changeBusy(value: boolean) { setBusy(value); onBusyChange(value); }
  function selectPanel(panel: PanelId | null) {
    const next = panel === activePanel ? null : panel;
    if (saving) return;
    if (dirty && activePanel === "outcomes" && next !== "outcomes") guardEdits(() => setActivePanel(next));
    else setActivePanel(next);
  }
  function openLibrary() { guardEdits(() => setActivePanel("references")); }

  async function refreshSessions() {
    refreshRequest.current?.abort();
    const controller = new AbortController();
    refreshRequest.current = controller;
    try {
      const items = await workspaceApi.sessions(project.id, controller.signal);
      if (!controller.signal.aborted) { setSessions(items.filter((s) => s.project_id === project.id)); setError(null); }
    } catch (err) {
      if (!controller.signal.aborted) setError(errorMessage(err));
    }
  }

  async function mutateSession(title: string, done: () => void, existing?: Session) {
    if (busy || mutating || chat.current?.isBusy()) return;
    refreshRequest.current?.abort();
    const controller = new AbortController();
    mutation.current?.abort();
    mutation.current = controller;
    setMutating(true);
    setError(null);
    try {
      const saved = existing
        ? await workspaceApi.renameSession(project.id, existing.id, title, controller.signal)
        : await workspaceApi.createSession(project.id, title, controller.signal);
      if (controller.signal.aborted) return;
      if (saved.project_id !== project.id) throw new Error("会话不属于当前项目。");
      setSessions((items) => existing ? items.map((s) => s.id === saved.id ? saved : s) : [saved, ...items]);
      if (!existing) { setSelectedId(saved.id); setSelectedDocument(null); rememberSession(project.id, saved.id); }
      done();
    } catch (err) {
      if (!controller.signal.aborted) setError(errorMessage(err));
    } finally {
      if (!controller.signal.aborted) setMutating(false);
    }
  }

  function openSource(source: KnowledgeSource) {
    if (source.project_id !== project.id) { setNotice("该来源不属于当前项目，已阻止跨项目访问。"); return; }
    guardEdits(() => { setActivePanel("references"); library.current?.showSource(source); });
  }

  function adopt(message: UIMessage) {
    if (!session) return;
    const serverPart = message.parts.find((part) => part.type === "data-server-message-id");
    const messageId = serverPart ? (serverPart as unknown as { data: { id: string } }).data.id : message.id;
    navigate(() => {
      setActivePanel("outcomes");
      setOutcomeTab("notes");
      void notes.create({ title: `${session.title} · 阅读笔记`.slice(0, 200), session_id: session.id, message_id: messageId });
    });
  }
  function compare(documents: ProjectDocument[]) {
    navigate(() => {
      if (!chat.current?.compare(documents)) setNotice("请先创建会话并等待历史加载完成，再开始对比。");
    });
  }

  async function finishNavigation(save: boolean) {
    if (!pendingNavigation || saving) return;
    if (save) {
      if (outline.dirty && !(await outline.save())) return;
      if (notes.dirty && !(await notes.save())) return;
    } else { outline.discard(); notes.discard(); }
    const action = pendingNavigation.action;
    setPendingNavigation(null);
    action();
  }

  const uiPanel = activePanel === "references" ? "references" : activePanel === "outcomes" && outcomeTab === "outline" ? "outline" : "chat";
  return (
    <div inert={locked} className="relative flex min-h-0 flex-1 flex-col lg:flex-row">
      <IconRail activePanel={activePanel} onSelect={selectPanel} />
      <div inert={mutating || Boolean(pendingNavigation)} className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto lg:flex-row lg:overflow-visible">
        {session ? <ChatSessionLoader key={session.id} ref={chat} project={project} session={session} selectedDocument={selectedDocument} activePanel={uiPanel} onBusyChange={changeBusy} onFinish={() => void refreshSessions()} onSource={openSource} onSaveNote={adopt} notesBusy={notes.loading || saving} onOpenLibrary={openLibrary} /> : <main className="flex min-h-[20rem] min-w-0 flex-1 flex-col lg:min-h-0">
          <div className="border-b border-zinc-800 px-5 py-3 text-xs text-zinc-400">{project.name} · 项目独立工作区</div>
          <div className="flex flex-1 flex-col items-center justify-center gap-3 overflow-y-auto px-6 py-8 text-center">
            <h2 className="text-lg">{loading ? "正在加载会话…" : error ? "暂时无法加载会话" : "从论文对比，开始第一份阅读笔记"}</h2>
            <p className="max-w-sm text-sm leading-6 text-zinc-500">创建会话 → 上传资料 → 对比并核查证据 → 保存阅读笔记。资料、会话与成果仅属于当前项目。</p>
            {!loading && <button type="button" onClick={() => error ? setActivePanel("sessions") : navigate(() => void mutateSession("文献对比与阅读笔记", () => setActivePanel("references")))} className="rounded border border-blue-800 px-4 py-2 text-xs text-blue-300">{error ? "打开会话面板重试" : "创建会话，开始整理资料"}</button>}
            {!loading && <button type="button" onClick={openLibrary} className="text-xs text-zinc-400 underline">先查看资料库</button>}
          </div>
        </main>}
        <PanelHost activePanel={activePanel} onClose={() => selectPanel(null)}
          sessions={<SessionsPanel sessions={sessions} selectedId={selectedId} loading={loading} busy={busy || saving} mutating={mutating} error={error}
            onSelect={(next) => { if (next.id !== selectedId) navigate(() => { setSelectedId(next.id); setSelectedDocument(null); setNotice(null); rememberSession(project.id, next.id); }); }}
            onCreate={(title, done) => navigate(() => void mutateSession(title, done))}
            onRename={(existing, title, done) => void mutateSession(title, done, existing)}
            onRetry={() => { if (sessions.length) void refreshSessions(); else { setLoading(true); setError(null); setRevision((n) => n + 1); } }} />}
          library={<LibraryPanel key={selectedId ?? "no-session"} ref={library} projectId={project.id} selected={selectedDocument} selectionLocked={busy} onSelect={setSelectedDocument} canCompare={Boolean(session) && !saving} onCompare={compare} />}
          outcomes={<div className="space-y-4">
            <div role="tablist" aria-label="项目成果类型" className="flex gap-1 rounded-lg bg-zinc-900 p-1">
              {(["notes", "outline"] as const).map((tab) => <button key={tab} type="button" role="tab" aria-selected={outcomeTab === tab} disabled={saving} onClick={() => { if (tab !== outcomeTab) guardEdits(() => setOutcomeTab(tab)); }} className={`flex-1 rounded px-3 py-2 text-xs ${outcomeTab === tab ? "bg-zinc-700 text-zinc-100" : "text-zinc-400"}`}>{tab === "notes" ? "阅读笔记" : "项目大纲"}</button>)}
            </div>
            <div role="tabpanel" aria-label="阅读笔记" hidden={outcomeTab !== "notes"}><NotesPanel key={notes.selected?.id ?? "empty"} notes={notes} navigate={navigate} onSource={openSource} onOpenLibrary={openLibrary} /></div>
            <div role="tabpanel" aria-label="项目大纲" hidden={outcomeTab !== "outline"}><OutlinePanel outline={outline} canDiscuss={Boolean(session) && !busy} onDiscuss={() => {
              const text = `请讨论以下项目大纲的结构、逻辑和可改进之处，不要生成整篇论文。\n\n${outline.sections.map((s, i) => `${i + 1}. ${s.title}`).join("\n")}`;
              if (!chat.current?.send(text)) setNotice("请等待会话历史加载完成或停止当前生成，再发送大纲。");
            }} /></div>
          </div>} />
      </div>
      {notice && <div role="alert" className="absolute inset-x-4 bottom-3 z-20 flex items-center justify-between gap-3 rounded border border-amber-700 bg-zinc-900 p-3 text-xs text-amber-200">{notice}<button type="button" onClick={() => setNotice(null)} className="shrink-0 underline">关闭</button></div>}
      {pendingNavigation && <dialog ref={guardDialog} aria-labelledby="outcome-guard-title" onCancel={(event) => { if (saving) event.preventDefault(); else setPendingNavigation(null); }} className="m-auto w-[calc(100%-2rem)] max-w-md space-y-4 rounded-xl border border-zinc-700 bg-zinc-900 p-5 text-zinc-100 shadow-xl backdrop:bg-black/70">
        <h2 id="outcome-guard-title" className="text-sm font-semibold">成果有未保存的修改</h2>
        <p className="text-xs leading-6 text-zinc-400">{notes.dirty ? "阅读笔记" : "大纲"}尚未保存。请保存或明确放弃修改后再离开。</p>
        {(outline.error || notes.error) && <p role="alert" className="text-xs text-amber-300">{conflict ? "版本冲突，请取消并在成果面板核对最新版本。" : notes.error || outline.error}</p>}
        <div className="flex flex-wrap gap-2">
          <button type="button" autoFocus disabled={saving || conflict} onClick={() => void finishNavigation(true)} className="rounded bg-zinc-100 px-3 py-2 text-xs text-zinc-900">{saving ? "保存中…" : "保存并继续"}</button>
          <button type="button" disabled={saving} onClick={() => void finishNavigation(false)} className="rounded border border-red-900 px-3 py-2 text-xs text-red-300">放弃修改并继续</button>
          <button type="button" disabled={saving} onClick={() => setPendingNavigation(null)} className="rounded border border-zinc-700 px-3 py-2 text-xs">取消</button>
        </div>
      </dialog>}
    </div>
  );
}
