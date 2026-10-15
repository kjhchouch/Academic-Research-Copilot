export type UiContext = {
  /** Which editor panel has focus: "editor" | "outline" | "references" | "chat" */
  active_panel?: string;
  /** Section the caret is in, e.g. "3.2" */
  cursor_section?: string;
  /** Text the user has selected, if any */
  selected_text?: string;
  /** Where the paper is in its lifecycle: "topic" | "drafting" | "revising" | "submitting" */
  paper_stage?: string;
};

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export const CHAT_ENDPOINT = `${API_BASE}/api/chat`;

const THREAD_KEY = "paper-agent.thread-id";

/**
 * Conversation id, stable across reloads. Passed to `useChat` as `id`, which the
 * transport puts on the wire as `id` and the gateway reads as `thread_id` —
 * the exact key LangGraph's checkpointer wants on D7.
 */
export function getThreadId(): string {
  if (typeof window === "undefined") return "";
  let id = window.localStorage.getItem(THREAD_KEY);
  if (!id) {
    id = crypto.randomUUID();
    window.localStorage.setItem(THREAD_KEY, id);
  }
  return id;
}
