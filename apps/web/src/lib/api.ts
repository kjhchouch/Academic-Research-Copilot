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

/**
 * One reference the agent is allowed to cite, and the wire mirror of
 * `paper_agent.citations.Citation`. The two must stay in sync by hand — there is
 * no codegen between the Python gateway and this client — which is exactly why
 * the shape is kept small and every field is spelled out.
 *
 * `citation_id` is assigned by the gateway from a paper a tool actually
 * returned, never by the model, so a `[n]` in the prose always resolves to a real
 * entry here or to nothing. `span` is reserved for Week 2 grounding and is
 * always null today; it is present so the shape does not change when that lands.
 */
export type Citation = {
  citation_id: number;
  title: string;
  authors: string[];
  year: number | null;
  venue: string | null;
  doi: string | null;
  abstract: string | null;
  span: unknown | null;
};

/** `https://doi.org/{doi}`, or null when a paper has no DOI to link to. The bare
 *  DOI is normalized upstream, so this only has to prefix it. */
export function doiUrl(doi: string | null): string | null {
  return doi ? `https://doi.org/${doi}` : null;
}


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
