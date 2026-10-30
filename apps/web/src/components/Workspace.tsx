"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE } from "@/lib/api";
import { errorMessage, recalledProject, rememberProject, workspaceApi, type Project } from "@/lib/workspaceApi";
import BackendBadge from "@/components/BackendBadge";
import NameForm from "@/components/NameForm";
import ProjectWorkbench, { type WorkbenchHandle } from "@/components/ProjectWorkbench";

export default function Workspace() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [backendUp, setBackendUp] = useState<boolean | null>(null);
  const [healthRevision, setHealthRevision] = useState(0);
  const [form, setForm] = useState<"create" | "rename" | null>(null);
  const [mutating, setMutating] = useState(false);
  const [busy, setBusy] = useState(false);
  const workbench = useRef<WorkbenchHandle | null>(null);
  const mutation = useRef<AbortController | null>(null);
  const project = projects.find((p) => p.id === selectedId) ?? null;

  useEffect(() => {
    const controller = new AbortController();
    workspaceApi.projects(controller.signal).then((items) => {
      if (controller.signal.aborted) return;
      const remembered = recalledProject();
      const selected = items.find((p) => p.id === remembered) ?? items[0];
      setProjects(items);
      setSelectedId(selected?.id ?? null);
      if (selected) rememberProject(selected.id);
      setLoading(false);
      setError(null);
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) { setError(errorMessage(err)); setLoading(false); }
    });
    return () => controller.abort();
  }, [revision]);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_BASE}/health`, { signal: controller.signal }).then((r) => {
      if (!controller.signal.aborted) setBackendUp(r.ok);
    }).catch(() => { if (!controller.signal.aborted) setBackendUp(false); });
    return () => controller.abort();
  }, [healthRevision]);
  useEffect(() => () => mutation.current?.abort(), []);

  function navigate(action: () => void) {
    if (busy || mutating) return;
    if (workbench.current) workbench.current.navigate(action);
    else action();
  }

  async function saveProject(name: string) {
    const mode = form;
    if (!mode || mutating || busy) return;
    const controller = new AbortController();
    mutation.current?.abort();
    mutation.current = controller;
    setMutating(true);
    setError(null);
    try {
      const saved = mode === "rename" && project
        ? await workspaceApi.renameProject(project.id, name, controller.signal)
        : await workspaceApi.createProject(name, controller.signal);
      if (controller.signal.aborted) return;
      setProjects((items) => mode === "rename" ? items.map((p) => p.id === saved.id ? saved : p) : [...items, saved]);
      setSelectedId(saved.id);
      rememberProject(saved.id);
      setForm(null);
    } catch (err) {
      if (!controller.signal.aborted) setError(errorMessage(err));
    } finally {
      if (!controller.signal.aborted) setMutating(false);
    }
  }

  return (
    <div className="flex h-dvh min-h-0 flex-col bg-zinc-950 text-zinc-100">
      <header className="shrink-0 border-b border-zinc-800 bg-zinc-900/50 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="mr-auto text-sm font-semibold tracking-tight">Academic Research Copilot</span>
          <button type="button" title="重新检测后端" onClick={() => { setBackendUp(null); setHealthRevision((n) => n + 1); }}><BackendBadge up={backendUp} /></button>
          <label className="flex min-w-0 items-center gap-2 text-xs text-zinc-400">
            项目
            <select aria-label="当前项目" value={project?.id ?? ""} disabled={loading || busy || mutating} onChange={(event) => {
              const id = event.target.value;
              if (id === selectedId || !projects.some((p) => p.id === id)) return;
              navigate(() => { setSelectedId(id); rememberProject(id); setForm(null); setError(null); });
            }} className="max-w-48 rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-zinc-100 sm:max-w-64">
              {!project && <option value="">{loading ? "正在加载…" : "尚无项目"}</option>}
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <button type="button" disabled={loading || busy || mutating} onClick={() => navigate(() => setForm("create"))} className="rounded border border-zinc-700 px-2.5 py-1.5 text-xs hover:bg-zinc-800">新建项目</button>
          <button type="button" disabled={!project || busy || mutating} onClick={() => setForm("rename")} className="rounded px-2 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800">重命名</button>
        </div>
        {form && <div className="mt-3 max-w-xl"><NameForm key={`${form}-${project?.id}`} label={form === "create" ? "新项目名称" : "项目名称"} initial={form === "rename" ? project?.name : ""} busy={mutating || busy} onCancel={() => setForm(null)} onSubmit={(name) => navigate(() => void saveProject(name))} /></div>}
        {error && <div role="alert" className="mt-2 flex flex-wrap items-center gap-2 text-xs text-red-300">{error}
          {!project && !form && <button type="button" className="underline" disabled={loading} onClick={() => { setLoading(true); setError(null); setRevision((n) => n + 1); }}>重新加载项目</button>}
        </div>}
        {busy && <p role="status" className="mt-2 text-xs text-amber-300">正在回答 · 请先停止生成，再切换项目或会话。</p>}
      </header>
      {project ? <ProjectWorkbench key={project.id} ref={workbench} project={project} locked={mutating} onBusyChange={setBusy} /> : (
        <main className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
          <h1 className="text-lg font-medium">{loading ? "正在连接项目工作区…" : "从一个真实项目开始"}</h1>
          <p className="max-w-md text-sm leading-6 text-zinc-400">每个项目独立保存会话、资料与大纲。上传和索引留在本机；只有主动启用资料回答时，相关片段才会发送给模型。</p>
          {!loading && !error && <button type="button" onClick={() => setForm("create")} className="rounded-lg bg-zinc-100 px-4 py-2 text-sm text-zinc-900">创建第一个项目</button>}
        </main>
      )}
    </div>
  );
}
