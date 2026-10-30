import type { UIMessage } from "ai";
import { API_BASE, type Citation } from "@/lib/api";

export type Project = { id: string; name: string; created_at: string };
export type Session = { id: string; project_id: string; title: string; updated_at: string };
export type SessionHistory = { session: Session; messages: UIMessage[] };
export type Outline = { sections: { id: string; title: string }[]; version: number };
export type ProjectDocument = {
  id: string;
  project_id: string;
  name: string;
  kind: "pdf" | "markdown";
  status: "pending" | "processing" | "ready" | "needs_ocr" | "failed";
  error: string | null;
  chunk_count: number;
  created_at: string;
};
export type Chunk = {
  id: string;
  document_id: string;
  name: string;
  text: string;
  location: string;
  page: number | null;
  line_start: number | null;
  line_end: number | null;
};
export type KnowledgeSource = Chunk & { source_id: string; project_id: string };
export type DocumentDetail = { document: ProjectDocument; chunks: Chunk[] };
export type KnowledgeCoverage = {
  task: "compare";
  documents: { document_id: string; name: string; source_ids: string[]; status: "retrieved" | "missing" }[];
};
export type ResearchNote = {
  id: string;
  project_id: string;
  title: string;
  body: string;
  sources: KnowledgeSource[];
  citations: Citation[];
  origin_session_id: string | null;
  origin_message_id: string | null;
  version: number;
  created_at: string;
  updated_at: string;
};
export type NoteInput = { title: string; session_id?: string; message_id?: string };

export class WorkspaceApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "WorkspaceApiError";
  }
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请重试。";
}

async function request<T>(path: string, signal: AbortSignal, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { ...init, signal });
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : typeof body.message === "string" ? body.message : "";
    } catch { /* A proxy may return non-JSON errors. */ }
    const fallback = response.status === 404
      ? "内容不存在或已被删除，请刷新后重试。"
      : response.status === 409 ? "内容已在其他窗口更新，请重新加载后合并修改。" : `请求失败（${response.status}），请重试。`;
    throw new WorkspaceApiError(response.status, detail || fallback);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const projectPath = (p: string) => `/api/projects/${encodeURIComponent(p)}`;
const sessionPath = (p: string, s: string) => `${projectPath(p)}/sessions/${encodeURIComponent(s)}`;
const documentPath = (p: string, d: string) => `${projectPath(p)}/documents/${encodeURIComponent(d)}`;

export const workspaceApi = {
  projects: (signal: AbortSignal) => request<Project[]>("/api/projects", signal),
  createProject: (name: string, signal: AbortSignal) => request<Project>("/api/projects", signal, json("POST", { name })),
  renameProject: (p: string, name: string, signal: AbortSignal) => request<Project>(projectPath(p), signal, json("PATCH", { name })),
  sessions: (p: string, signal: AbortSignal) => request<Session[]>(`${projectPath(p)}/sessions`, signal),
  createSession: (p: string, title: string, signal: AbortSignal) => request<Session>(`${projectPath(p)}/sessions`, signal, json("POST", { title })),
  renameSession: (p: string, s: string, title: string, signal: AbortSignal) => request<Session>(sessionPath(p, s), signal, json("PATCH", { title })),
  history: async (p: string, s: string, signal: AbortSignal) => {
    const result = await request<SessionHistory>(sessionPath(p, s), signal);
    if (result.session.project_id !== p || result.session.id !== s) throw new Error("会话不属于当前项目，已阻止加载。");
    return result;
  },
  outline: (p: string, signal: AbortSignal) => request<Outline>(`${projectPath(p)}/outline`, signal),
  saveOutline: (p: string, outline: Outline, signal: AbortSignal) => request<Outline>(`${projectPath(p)}/outline`, signal, json("PUT", outline)),
  notes: (p: string, signal: AbortSignal) => request<ResearchNote[]>(`${projectPath(p)}/notes`, signal),
  note: (p: string, n: string, signal: AbortSignal) => request<ResearchNote>(`${projectPath(p)}/notes/${encodeURIComponent(n)}`, signal),
  createNote: (p: string, input: NoteInput, signal: AbortSignal) => request<ResearchNote>(`${projectPath(p)}/notes`, signal, json("POST", input)),
  saveNote: (p: string, n: string, input: { title: string; body: string; version: number }, signal: AbortSignal) => request<ResearchNote>(`${projectPath(p)}/notes/${encodeURIComponent(n)}`, signal, json("PUT", input)),
  deleteNote: (p: string, n: string, signal: AbortSignal) => request<void>(`${projectPath(p)}/notes/${encodeURIComponent(n)}`, signal, { method: "DELETE" }),
  documents: (p: string, signal: AbortSignal) => request<ProjectDocument[]>(`${projectPath(p)}/documents`, signal),
  document: async (p: string, d: string, signal: AbortSignal) => {
    const result = await request<DocumentDetail>(documentPath(p, d), signal);
    if (result.document.project_id !== p || result.document.id !== d) throw new Error("资料不属于当前项目，已阻止加载。");
    return result;
  },
  upload: (p: string, file: File, signal: AbortSignal) => {
    if (file.size > 20 * 1024 * 1024) throw new Error("文件超过 20 MiB 限制。");
    if (!/\.(pdf|md)$/i.test(file.name)) throw new Error("仅支持 PDF 或 Markdown (.md) 文件。");
    return request<ProjectDocument>(`${projectPath(p)}/documents?filename=${encodeURIComponent(file.name)}`, signal, {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
    });
  },
  retryDocument: (p: string, d: string, signal: AbortSignal) => request<ProjectDocument>(`${documentPath(p, d)}/retry`, signal, { method: "POST" }),
  deleteDocument: (p: string, d: string, signal: AbortSignal) => request<void>(documentPath(p, d), signal, { method: "DELETE" }),
  fileUrl: (p: string, d: string) => `${API_BASE}${documentPath(p, d)}/file`,
  search: (p: string, q: string, documentId: string | null, signal: AbortSignal) => {
    const params = new URLSearchParams({ q });
    if (documentId) params.set("document_id", documentId);
    return request<Chunk[]>(`${projectPath(p)}/search?${params}`, signal);
  },
};

const PROJECT_KEY = "paper-agent.workspace.project";
const sessionKey = (p: string) => `paper-agent.workspace.session.${p}`;
export function recalledProject(): string | null { return recalled(PROJECT_KEY); }
export function recalledSession(p: string): string | null { return recalled(sessionKey(p)); }
export function rememberProject(p: string) { remember(PROJECT_KEY, p); }
export function rememberSession(p: string, s: string) { remember(sessionKey(p), s); }
function recalled(key: string): string | null {
  try { return window.localStorage.getItem(key); } catch { return null; }
}
function remember(key: string, value: string) {
  try { window.localStorage.setItem(key, value); } catch { /* Storage is optional. */ }
}
