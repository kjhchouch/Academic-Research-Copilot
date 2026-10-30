"use client";

import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import { useChat } from "@ai-sdk/react";
import type { UIMessage } from "ai";
import { CHAT_ENDPOINT } from "@/lib/api";
import { SseChatTransport } from "@/lib/sseTransport";
import { errorMessage, workspaceApi, type KnowledgeSource, type Project, type ProjectDocument, type Session, type SessionHistory } from "@/lib/workspaceApi";
import ChatColumn from "@/components/ChatColumn";

export type ChatSessionHandle = { isBusy: () => boolean; send: (text: string) => boolean; compare: (documents: ProjectDocument[]) => boolean };
type SessionProps = {
  ref?: Ref<ChatSessionHandle>;
  project: Project;
  session: Session;
  selectedDocument: ProjectDocument | null;
  activePanel: "chat" | "references" | "outline";
  onBusyChange: (busy: boolean) => void;
  onFinish: () => void;
  onSource: (source: KnowledgeSource) => void;
  onSaveNote: (message: UIMessage) => void;
  notesBusy: boolean;
  onOpenLibrary: () => void;
};

/** History is fetched and validated before mounting a session-owned useChat. */
export default function ChatSessionLoader(props: SessionProps) {
  const [history, setHistory] = useState<SessionHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [revision, setRevision] = useState(0);
  const { project, session } = props;
  useEffect(() => {
    const controller = new AbortController();
    workspaceApi.history(project.id, session.id, controller.signal).then((result) => {
      if (!controller.signal.aborted) { setHistory(result); setError(null); }
    }).catch((err: unknown) => { if (!controller.signal.aborted) setError(errorMessage(err)); });
    return () => controller.abort();
  }, [project.id, session.id, revision]);
  function reload() { setHistory(null); setError(null); setRevision((n) => n + 1); }

  if (!history) return <main className="flex min-h-[20rem] min-w-0 flex-1 flex-col lg:min-h-0">
    <div className="border-b border-zinc-800 px-5 py-3"><h2 className="truncate text-sm font-medium">{session.title}</h2><p className="text-xs text-zinc-500">{project.name} · 项目会话</p></div>
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center text-sm text-zinc-400">
      <p role={error ? "alert" : "status"}>{error ?? "正在恢复服务器会话历史…"}</p>
      {error && <button type="button" onClick={reload} className="rounded border border-zinc-600 px-4 py-2 text-xs">重试加载历史</button>}
    </div>
  </main>;
  return <ChatSession key={history.session.id} {...props} initialHistory={history} onReload={reload} input={input} onInputChange={setInput} />;
}

function ChatSession({ ref, project, session, selectedDocument, activePanel, initialHistory, onBusyChange, onFinish, onSource, onSaveNote, notesBusy, onOpenLibrary, onReload, input, onInputChange }: SessionProps & {
  initialHistory: SessionHistory;
  onReload: () => void;
  input: string;
  onInputChange: (value: string) => void;
}) {
  const [transport] = useState(() => new SseChatTransport({ api: CHAT_ENDPOINT }));
  const [useKnowledge, setUseKnowledge] = useState(false);
  const [requestDocument, setRequestDocument] = useState<ProjectDocument | null>(null);
  const [requestScope, setRequestScope] = useState<string | null>(null);
  const [turnNotice, setTurnNotice] = useState<string | null>(null);
  const [sendError, setSendError] = useState<Error | undefined>();
  const sending = useRef(false);
  const alive = useRef(true);
  const { messages, sendMessage, setMessages, status, error, stop, clearError } = useChat({
    id: session.id,
    messages: initialHistory.messages,
    transport,
    onFinish: ({ message, isAbort, isError, isDisconnect }) => {
      if (!alive.current) return;
      if (isAbort || isError || isDisconnect) {
        setTurnNotice(isAbort ? "已停止生成。可刷新服务器历史核对已保存内容。" : "本轮未完整结束，请核对来源后再使用。");
        // An aborted connection cannot deliver another SSE part. Keep an honest
        // local warning until the server's persisted turn status is reloaded.
        if (message.role === "assistant") setMessages((current) => current.map((item) => item.id === message.id ? {
          ...item,
          parts: [...item.parts.filter((part) => part.type !== "data-turn-status"), {
            type: "data-turn-status" as const,
            data: { status: isAbort ? "interrupted" : "error" },
          }],
        } : item));
      }
      onFinish();
    },
  });
  const busy = status === "submitted" || status === "streaming";
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; void stop(); };
  }, [stop]);

  function handleSend(text: string, comparison?: ProjectDocument[]): boolean {
    if (!text.trim() || sending.current || busy) return false;
    if (comparison && (comparison.length < 2 || comparison.length > 5)) return false;
    sending.current = true;
    setRequestDocument(selectedDocument);
    setRequestScope(comparison ? `本轮对比范围：${comparison.map((d) => d.name).join("、")}` : null);
    onBusyChange(true);
    clearError();
    setSendError(undefined);
    setTurnNotice(null);
    void sendMessage({ text }, { body: {
      project_id: project.id,
      use_knowledge: comparison ? true : useKnowledge,
      knowledge_task: comparison ? "compare" : "qa",
      document_ids: comparison ? comparison.map((d) => d.id) : selectedDocument ? [selectedDocument.id] : [],
      ui_context: { active_panel: activePanel, paper_stage: "topic" },
    } }).catch((err: unknown) => {
      if (alive.current) setSendError(new Error(errorMessage(err)));
    }).finally(() => {
      sending.current = false;
      if (alive.current) onBusyChange(false);
    });
    return true;
  }
  useImperativeHandle(ref, () => ({
    isBusy: () => sending.current || busy,
    send: handleSend,
    compare: (documents) => handleSend("请依据所选资料，比较研究问题、方法、结果或结论与作者自陈局限。逐项引用来源，区分原文摘录与模型归纳；缺证据的项目明确留空，差异不等于冲突，不推断研究优劣或新颖性。如果资料是合成示例，请明确说明。", documents),
  }));

  return <ChatColumn messages={messages} status={status} error={error ?? sendError} session={session} project={project}
    input={input} onInputChange={onInputChange}
    useKnowledge={useKnowledge} onKnowledgeChange={setUseKnowledge} selectedDocument={busy ? requestDocument : selectedDocument}
    requestScope={busy ? requestScope : null} onSaveNote={onSaveNote} notesBusy={notesBusy} onOpenLibrary={onOpenLibrary}
    onSend={handleSend} onStop={() => void stop()} onReload={onReload} onSource={onSource} turnNotice={turnNotice}
    onClearError={() => { clearError(); setSendError(undefined); }} />;
}
