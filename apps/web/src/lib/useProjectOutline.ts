"use client";

import { useEffect, useRef, useState } from "react";
import { errorMessage, workspaceApi, WorkspaceApiError, type Outline } from "@/lib/workspaceApi";

/** Project-keyed owner of both the server version and the unsaved draft. */
export function useProjectOutline(projectId: string) {
  const [saved, setSaved] = useState<Outline | null>(null);
  const [sections, setSections] = useState<Outline["sections"]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [revision, setRevision] = useState(0);
  const operation = useRef<AbortController | null>(null);
  const dirty = saved !== null && JSON.stringify(sections) !== JSON.stringify(saved.sections);

  useEffect(() => {
    const controller = new AbortController();
    workspaceApi.outline(projectId, controller.signal).then((outline) => {
      if (controller.signal.aborted) return;
      setSaved(outline);
      setSections(outline.sections);
      setLoading(false);
      setConflict(false);
      setError(null);
    }).catch((err: unknown) => {
      if (controller.signal.aborted) return;
      setError(errorMessage(err));
      setLoading(false);
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

  async function save(): Promise<boolean> {
    if (!saved || saving || loading || conflict) return false;
    if (!dirty) return true;
    if (sections.some((s) => !s.title.trim())) {
      setError("章节标题不能为空，请填写后保存。");
      return false;
    }
    const controller = new AbortController();
    operation.current?.abort();
    operation.current = controller;
    setSaving(true);
    setError(null);
    try {
      const outline = await workspaceApi.saveOutline(projectId, { version: saved.version, sections }, controller.signal);
      if (controller.signal.aborted) return false;
      setSaved(outline);
      setSections(outline.sections);
      return true;
    } catch (err) {
      if (!controller.signal.aborted) {
        setConflict(err instanceof WorkspaceApiError && err.status === 409);
        setError(errorMessage(err));
      }
      return false;
    } finally {
      if (!controller.signal.aborted) setSaving(false);
    }
  }

  // On conflict keep the local draft, but fetch the current server version before
  // permitting another explicit save. No silent overwrite of a newer version.
  async function rebase() {
    if (saving) return;
    const controller = new AbortController();
    operation.current?.abort();
    operation.current = controller;
    setSaving(true);
    try {
      const latest = await workspaceApi.outline(projectId, controller.signal);
      if (controller.signal.aborted) return;
      setSaved(latest);
      setConflict(false);
      setError("已读取最新版本并保留本地草稿。请核对后再次保存；保存将以本地章节替换远端章节。");
    } catch (err) {
      if (!controller.signal.aborted) setError(errorMessage(err));
    } finally {
      if (!controller.signal.aborted) setSaving(false);
    }
  }

  function discard() {
    if (saved) setSections(saved.sections);
    setError(null);
    if (conflict) reload();
  }
  function reload() {
    // Reload is only offered after an explicit discard when there is a draft.
    if (saved) setSections(saved.sections);
    setLoading(true);
    setError(null);
    setRevision((n) => n + 1);
  }

  return { saved, sections, setSections, loading, saving, error, conflict, dirty, save, discard, reload, rebase };
}
export type OutlineController = ReturnType<typeof useProjectOutline>;
