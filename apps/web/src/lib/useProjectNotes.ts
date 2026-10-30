"use client";

import { useEffect, useRef, useState } from "react";
import { errorMessage, workspaceApi, WorkspaceApiError, type NoteInput, type ResearchNote } from "@/lib/workspaceApi";

export function useProjectNotes(projectId: string) {
  const [items, setItems] = useState<ResearchNote[]>([]);
  const [selected, setSelected] = useState<ResearchNote | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [revision, setRevision] = useState(0);
  const operation = useRef<AbortController | null>(null);
  const dirty = selected !== null && (title !== selected.title || body !== selected.body);

  function select(note: ResearchNote | null) {
    setSelected(note);
    setTitle(note?.title ?? "");
    setBody(note?.body ?? "");
    setConflict(false);
    setError(null);
  }
  function accept(note: ResearchNote) {
    if (note.project_id !== projectId) throw new Error("笔记不属于当前项目。");
    setItems((current) => [note, ...current.filter((n) => n.id !== note.id)]);
    select(note);
  }

  useEffect(() => {
    const controller = new AbortController();
    workspaceApi.notes(projectId, controller.signal).then((notes) => {
      if (controller.signal.aborted) return;
      const scoped = notes.filter((n) => n.project_id === projectId);
      setItems(scoped);
      select(scoped[0] ?? null);
      setLoading(false);
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) { setLoading(false); setError(errorMessage(err)); }
    });
    return () => controller.abort();
  }, [projectId, revision]);
  useEffect(() => () => operation.current?.abort(), []);
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  async function create(input: NoteInput): Promise<boolean> {
    if (operation.current || loading) return false;
    const controller = new AbortController();
    operation.current = controller;
    setSaving(true);
    setError(null);
    try {
      const note = await workspaceApi.createNote(projectId, input, controller.signal);
      if (controller.signal.aborted) return false;
      accept(note);
      return true;
    } catch (err) {
      if (!controller.signal.aborted) setError(errorMessage(err));
      return false;
    } finally {
      operation.current = null;
      if (!controller.signal.aborted) setSaving(false);
    }
  }

  async function save(): Promise<boolean> {
    if (operation.current || loading || conflict || !selected) return false;
    if (!dirty) return true;
    if (!title.trim()) { setError("笔记标题不能为空。"); return false; }
    const controller = new AbortController();
    operation.current = controller;
    setSaving(true);
    setError(null);
    try {
      const note = await workspaceApi.saveNote(projectId, selected.id, { title: title.trim(), body, version: selected.version }, controller.signal);
      if (controller.signal.aborted) return false;
      accept(note);
      return true;
    } catch (err) {
      if (!controller.signal.aborted) {
        setConflict(err instanceof WorkspaceApiError && err.status === 409);
        setError(errorMessage(err));
      }
      return false;
    } finally {
      operation.current = null;
      if (!controller.signal.aborted) setSaving(false);
    }
  }

  async function rebase() {
    if (operation.current || !selected) return;
    const controller = new AbortController();
    operation.current = controller;
    setSaving(true);
    try {
      const latest = await workspaceApi.note(projectId, selected.id, controller.signal);
      if (controller.signal.aborted) return;
      if (latest.project_id !== projectId) throw new Error("笔记不属于当前项目。");
      setSelected(latest);
      setItems((current) => current.map((n) => n.id === latest.id ? latest : n));
      setConflict(false);
      setError("已读取最新版本并保留本地草稿。请对照后保存；保存会用本地正文替换服务器正文。");
    } catch (err) {
      if (!controller.signal.aborted) setError(errorMessage(err));
    } finally {
      operation.current = null;
      if (!controller.signal.aborted) setSaving(false);
    }
  }

  async function remove() {
    if (operation.current || !selected || dirty) return;
    const controller = new AbortController();
    operation.current = controller;
    setSaving(true);
    setError(null);
    try {
      await workspaceApi.deleteNote(projectId, selected.id, controller.signal);
      if (controller.signal.aborted) return;
      const remaining = items.filter((n) => n.id !== selected.id);
      setItems(remaining);
      select(remaining[0] ?? null);
    } catch (err) {
      if (!controller.signal.aborted) setError(errorMessage(err));
    } finally {
      operation.current = null;
      if (!controller.signal.aborted) setSaving(false);
    }
  }

  function discard() { select(selected); }
  function reload() { setLoading(true); setError(null); setRevision((n) => n + 1); }
  return { items, selected, select, title, setTitle, body, setBody, loading, saving, error, conflict, dirty, create, save, rebase, remove, discard, reload };
}
export type NotesController = ReturnType<typeof useProjectNotes>;
