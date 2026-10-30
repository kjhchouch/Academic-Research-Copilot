"use client";

import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import { errorMessage, workspaceApi, type Chunk, type KnowledgeSource, type ProjectDocument } from "@/lib/workspaceApi";
import DocumentPreview, { type PreviewTarget } from "@/components/panels/DocumentPreview";

export type LibraryHandle = { showSource: (source: KnowledgeSource) => void };
type Upload = { id: string; file: File; error: string | null };
const STATUS: Record<ProjectDocument["status"], string> = {
  pending: "等待解析", processing: "解析中", ready: "可检索", needs_ocr: "需要 OCR", failed: "解析失败",
};
const pending = (document: ProjectDocument) => document.status === "pending" || document.status === "processing";
const SYNTHETIC_EXAMPLES = [
  {
    name: "SYNTHETIC-合成示例-A-关键词检索.md",
    text: `# SYNTHETIC 合成示例 A：学习笔记的关键词检索

> 仅用于界面演示，不是真实论文或研究发现。所有资料、方法描述和数值均为人工编造，不得作为学术证据引用。

## 问题
如何在学习笔记中找到与问题相关的段落？

## 方法
演示方案 A 使用词频匹配，对人工编写的笔记按关键词重叠排序，返回前三个段落。不处理同义表达。

## 结果（人工编造）
在虚构的 50 个查询中，人工设定 37 个查询的前三个结果包含目标段落。该数字仅用于测试片段引用和对比表格，不来自实验。

## 局限
查询与笔记共享关键词，未覆盖同义词、真实用户或噪声文本；没有重复试验、统计检验或独立评估。
与合成示例 B 的查询设置不同，37 与 42 不能用于推断真实优劣、显著性或推广效果。
`,
  },
  {
    name: "SYNTHETIC-合成示例-B-同义词扩展.md",
    text: `# SYNTHETIC 合成示例 B：学习笔记的同义词扩展检索

> 仅用于界面演示，不是真实论文或研究发现。所有资料、方法描述和数值均为人工编造，不得作为学术证据引用。

## 问题
如何在学习笔记中找到与问题相关的段落？

## 方法
演示方案 B 先用手工同义词表扩展查询，再按关键词匹配排序，返回前三个段落。同义词表由示例作者预先编写。

## 结果（人工编造）
在另一组虚构的 50 个查询中，人工设定 42 个查询的前三个结果包含目标段落，并设定出现词义歧义导致的误匹配。数字仅用于演示，不来自实验。

## 局限
同义词表可能泄露目标词，维护成本和延迟未测量；查询集与示例 A 不同，没有统一基准或统计检验。
42 与 37 都是人工数值，不能据此声称方案 B 优于 A，亦不能推出真实研究结论。
`,
  },
];

/** Keyed by project AND session: switching cancels requests, uploads and polls. */
export default function LibraryPanel({ ref, projectId, selected, selectionLocked, onSelect, canCompare, onCompare }: {
  ref?: Ref<LibraryHandle>;
  projectId: string;
  selected: ProjectDocument | null;
  selectionLocked: boolean;
  onSelect: (document: ProjectDocument | null) => void;
  canCompare: boolean;
  onCompare: (documents: ProjectDocument[]) => void;
}) {
  const [documents, setDocuments] = useState<ProjectDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [working, setWorking] = useState<string[]>([]);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Chunk[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewTarget | null>(null);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  // Consent applies only to this exact selection, never to later additions.
  const [consentedSelection, setConsentedSelection] = useState<string | null>(null);
  const [importingExamples, setImportingExamples] = useState(false);
  const exampleImportActive = useRef(false);
  const requests = useRef(new Set<AbortController>());
  const uploadRequests = useRef(new Map<string, AbortController>());
  const searchRequest = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const previewSequence = useRef(0);
  const selectedId = selected?.id ?? null;
  const pendingKey = documents.filter(pending).map((d) => `${d.id}:${d.status}`).join(",");
  const compareDocuments = documents.filter((d) => d.project_id === projectId && d.status === "ready" && compareIds.includes(d.id));
  const compareSelection = JSON.stringify([projectId, ...compareDocuments.map((d) => d.id).sort()]);
  const compareConsent = compareDocuments.length > 0 && consentedSelection === compareSelection;
  const operationsPending = loading || importingExamples || uploads.some((u) => !u.error) || working.length > 0;
  const compareDisabled = !canCompare || selectionLocked || operationsPending || deleteId !== null || compareDocuments.length < 2 || compareDocuments.length > 5 || !compareConsent;
  const importDisabled = selectionLocked || operationsPending;

  useImperativeHandle(ref, () => ({
    showSource(source) {
      if (source.project_id !== projectId) { setError("引用不属于当前项目，已阻止访问。"); return; }
      setPreview({ documentId: source.document_id, chunkId: source.id, source, nonce: ++previewSequence.current });
    },
  }), [projectId]);

  useEffect(() => {
    const controller = new AbortController();
    const version = generation.current;
    workspaceApi.documents(projectId, controller.signal).then((items) => {
      if (controller.signal.aborted) return;
      if (version === generation.current) {
        const currentDocuments = items.filter((d) => d.project_id === projectId);
        setDocuments(currentDocuments);
        setCompareIds((ids) => ids.filter((id) => currentDocuments.some((d) => d.id === id && d.status === "ready")));
      }
      setLoading(false);
      setError(null);
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) { setError(errorMessage(err)); setLoading(false); }
    });
    return () => controller.abort();
  }, [projectId, revision]);

  useEffect(() => {
    if (!pendingKey) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const version = generation.current;
      try {
        const items = await workspaceApi.documents(projectId, controller.signal);
        if (controller.signal.aborted) return;
        if (version === generation.current) {
          const currentDocuments = items.filter((d) => d.project_id === projectId);
          setDocuments(currentDocuments);
          setCompareIds((ids) => ids.filter((id) => currentDocuments.some((d) => d.id === id && d.status === "ready")));
        }
        if (items.some(pending) || version !== generation.current) timer = setTimeout(poll, 2000);
      } catch (err) {
        // A failed poll stops here. The visible refresh button restarts it.
        if (!controller.signal.aborted) setError(`状态刷新失败：${errorMessage(err)}`);
      }
    }
    timer = setTimeout(poll, 1500);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [projectId, pendingKey, revision]);

  useEffect(() => {
    const active = requests.current;
    return () => { for (const request of active) request.abort(); searchRequest.current?.abort(); };
  }, []);
  useEffect(() => () => searchRequest.current?.abort(), [selectedId]);
  useEffect(() => {
    if (!loading && !error && !selectionLocked && selectedId && !documents.some((d) => d.id === selectedId)) onSelect(null);
  }, [documents, loading, error, selectionLocked, selectedId, onSelect]);

  function select(document: ProjectDocument | null) {
    if (selectionLocked) return;
    searchRequest.current?.abort();
    setSearching(false);
    setResults(null);
    setSearchError(null);
    onSelect(document);
  }
  function refresh() { setLoading(true); setError(null); setRevision((n) => n + 1); }
  function open(documentId: string, chunkId?: string) { setPreview({ documentId, chunkId, nonce: ++previewSequence.current }); }
  function controllerForOperation() {
    const controller = new AbortController();
    requests.current.add(controller);
    return controller;
  }

  function toggleCompare(document: ProjectDocument, checked: boolean) {
    if (selectionLocked || loading || working.includes(document.id) || document.project_id !== projectId || document.status !== "ready") return;
    setConsentedSelection(null);
    setCompareIds((ids) => {
      const currentIds = ids.filter((id) => documents.some((d) => d.id === id && d.project_id === projectId && d.status === "ready"));
      if (!checked) return currentIds.filter((id) => id !== document.id);
      return currentIds.includes(document.id) || currentIds.length >= 5 ? currentIds : [...currentIds, document.id];
    });
  }

  function startCompare() {
    if (compareDisabled || exampleImportActive.current || uploadRequests.current.size > 0) return;
    setConsentedSelection(null);
    onCompare(compareDocuments);
  }

  async function importExamples() {
    if (importDisabled || exampleImportActive.current || uploadRequests.current.size > 0) return;
    // A ref closes the double-click window before the disabled state renders.
    exampleImportActive.current = true;
    setImportingExamples(true);
    try {
      await Promise.all(SYNTHETIC_EXAMPLES.map((example) => upload(new File([example.text], example.name, { type: "text/markdown" }))));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      exampleImportActive.current = false;
      setImportingExamples(false);
    }
  }

  async function upload(file: File, id = crypto.randomUUID()) {
    const controller = controllerForOperation();
    uploadRequests.current.set(id, controller);
    setUploads((items) => [...items.filter((u) => u.id !== id), { id, file, error: null }]);
    try {
      const document = await workspaceApi.upload(projectId, file, controller.signal);
      if (controller.signal.aborted) return;
      generation.current++;
      setDocuments((items) => [document, ...items.filter((d) => d.id !== document.id)]);
      if (document.status !== "ready") setCompareIds((ids) => ids.filter((item) => item !== document.id));
      setUploads((items) => items.filter((u) => u.id !== id));
    } catch (err) {
      if (!controller.signal.aborted) setUploads((items) => items.map((u) => u.id === id ? { ...u, error: errorMessage(err) } : u));
    } finally {
      requests.current.delete(controller);
      if (uploadRequests.current.get(id) === controller) uploadRequests.current.delete(id);
    }
  }

  async function mutate(document: ProjectDocument, action: "retry" | "delete") {
    if (working.includes(document.id) || (selectionLocked && action === "delete")) return;
    const controller = controllerForOperation();
    setWorking((items) => [...items, document.id]);
    setCompareIds((ids) => ids.filter((id) => id !== document.id));
    if (compareIds.includes(document.id)) setConsentedSelection(null);
    setError(null);
    try {
      const updated = action === "retry"
        ? await workspaceApi.retryDocument(projectId, document.id, controller.signal)
        : await workspaceApi.deleteDocument(projectId, document.id, controller.signal);
      if (controller.signal.aborted) return;
      generation.current++;
      setDocuments((items) => updated ? items.map((d) => d.id === updated.id ? updated : d) : items.filter((d) => d.id !== document.id));
      if (action === "delete") {
        setDeleteId(null);
        searchRequest.current?.abort();
        setSearching(false);
        setResults(null);
        setPreview((current) => current?.documentId === document.id ? null : current);
      }
    } catch (err) {
      if (!controller.signal.aborted) setError(errorMessage(err));
    } finally {
      requests.current.delete(controller);
      if (!controller.signal.aborted) setWorking((items) => items.filter((id) => id !== document.id));
    }
  }

  async function search(event: React.FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    searchRequest.current?.abort();
    const controller = new AbortController();
    searchRequest.current = controller;
    setSearching(true);
    setSearchError(null);
    setResults(null);
    try {
      const chunks = await workspaceApi.search(projectId, query.trim(), selectedId, controller.signal);
      if (!controller.signal.aborted) setResults(chunks);
    } catch (err) {
      if (!controller.signal.aborted) setSearchError(errorMessage(err));
    } finally {
      if (!controller.signal.aborted) setSearching(false);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-xs leading-5 text-zinc-500">PDF / Markdown · 单个文件 ≤ 20 MiB。上传与索引留在本机。切换项目或会话会取消当前上传和检索；已接收的文件可刷新查看。</p>
      <div className="flex items-center gap-2">
        <label className={`relative flex-1 rounded-lg border border-dashed border-zinc-600 px-3 py-3 text-center text-xs ${loading ? "opacity-50" : "hover:bg-zinc-900"}`}>
          上传 PDF / Markdown
          <input aria-label="上传 PDF 或 Markdown" type="file" accept=".pdf,.md" multiple disabled={loading} className="absolute inset-0 w-full cursor-pointer opacity-0" onChange={(event) => {
            for (const file of Array.from(event.target.files ?? [])) void upload(file);
            event.target.value = "";
          }} />
        </label>
        <button type="button" disabled={operationsPending} onClick={refresh} className="rounded border border-zinc-700 px-3 py-3 text-xs">刷新</button>
      </div>
      <div className="space-y-2 rounded-lg border border-amber-900/70 bg-amber-950/20 p-3 text-xs">
        <p className="font-medium text-amber-200">SYNTHETIC 合成演示 · 非真实论文 / 研究发现</p>
        <p className="leading-5 text-amber-200/80">可选导入两篇同主题的人工 Markdown。方法与 37 / 42 等数值均为编造，不代表真实优劣。仅上传到本地资料库，不自动选择资料、勾选同意或调用模型。</p>
        <button type="button" disabled={importDisabled} onClick={() => void importExamples()} className="rounded border border-amber-800 px-3 py-2 text-amber-200 disabled:cursor-not-allowed disabled:opacity-40">导入合成示例（2 篇）</button>
        {importingExamples && <p role="status" className="text-amber-200">正在导入合成示例…</p>}
      </div>
      <section aria-label="多篇资料对比" className="space-y-3 rounded-lg border border-zinc-700 bg-zinc-900/40 p-3 text-xs">
        <h3 className="font-medium text-zinc-200">多篇资料对比</h3>
        <p className="leading-5 text-zinc-400">上传资料 → 勾选 2–5 篇可检索资料 → 同意后开始对比 → 核对引用并保存笔记。对比勾选不改变下方的单篇提问或检索范围。</p>
        <div aria-live="polite" className="space-y-1">
          <p className="text-zinc-300">已选 {compareDocuments.length} / 5 篇（至少 2 篇）</p>
          {compareDocuments.length > 0 ? <ul className="list-inside list-disc space-y-1 text-zinc-400">{compareDocuments.map((document) => <li key={document.id} className="break-words">{document.name}</li>)}</ul> : <p className="text-zinc-500">请在资料卡片中勾选“加入对比”。解析中、失败或需要 OCR 的资料不可选。</p>}
        </div>
        <label className="flex items-start gap-2 leading-5 text-zinc-300">
          <input type="checkbox" checked={compareConsent} disabled={selectionLocked || operationsPending || compareDocuments.length < 2} onChange={(event) => setConsentedSelection(event.target.checked ? compareSelection : null)} className="mt-1 shrink-0 accent-blue-500 disabled:opacity-40" />
          我同意将所选资料的相关片段发送至已配置的模型进行对比
        </label>
        <p className="text-[11px] leading-5 text-zinc-500">仅本次所选资料获得授权；更改勾选或再次对比需重新同意。请勿发送无权分享的敏感资料。</p>
        <button type="button" disabled={compareDisabled} onClick={startCompare} className="rounded bg-blue-600 px-3 py-2 font-medium text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-40">开始对比</button>
        {!canCompare && <p role="status" className="text-zinc-400">当前暂不可对比，请检查模型配置与会话状态。</p>}
      </section>
      {uploads.map((item) => <div key={item.id} role="status" className="rounded border border-zinc-700 p-2 text-xs">
        <p className="break-words">{item.file.name} · {item.error ? "上传失败" : "上传中…"}</p>
        {item.error ? <><p className="mt-1 text-red-300">{item.error}</p><div className="mt-2 flex gap-3"><button type="button" onClick={() => void upload(item.file, item.id)} className="text-blue-300">重试上传</button><button type="button" onClick={() => setUploads((items) => items.filter((u) => u.id !== item.id))}>移除记录</button></div></> : <button type="button" className="mt-2 text-zinc-400" onClick={() => {
          uploadRequests.current.get(item.id)?.abort();
          setUploads((items) => items.map((u) => u.id === item.id ? { ...u, error: "上传已取消；服务端可能已接收，请先刷新列表确认。" } : u));
        }}>取消上传</button>}
      </div>)}
      {error && <p role="alert" className="rounded border border-red-900 p-2 text-xs text-red-300">{error} 请点击刷新重试。</p>}
      {loading && <p role="status" className="text-xs text-zinc-400">正在加载资料…</p>}
      {!loading && !error && documents.length === 0 && <p className="py-4 text-center text-xs text-zinc-500">项目资料库为空。上传后才能检索本地片段。</p>}
      <ul className="space-y-2">{documents.map((document) => <li key={document.id} className={`rounded-lg border p-3 ${selectedId === document.id ? "border-blue-700 bg-blue-950/20" : "border-zinc-800 bg-zinc-900/50"}`}>
        <div className="flex items-start justify-between gap-2"><button type="button" onClick={() => open(document.id)} className="min-w-0 break-words text-left text-xs font-medium hover:text-blue-300">{document.name}</button><span className={`shrink-0 text-[10px] ${document.status === "ready" ? "text-emerald-400" : document.status === "failed" || document.status === "needs_ocr" ? "text-amber-300" : "text-zinc-400"}`}>{STATUS[document.status]}</span></div>
        <p className="mt-1 text-[10px] text-zinc-500">{document.kind.toUpperCase()} · {document.chunk_count} 个片段</p>
        {document.status === "needs_ocr" && <p className="mt-2 text-xs leading-5 text-amber-200">扫描 PDF 没有可检索文本，未完成索引。请先在本机 OCR，再上传含文本的文件。</p>}
        {document.error && <p className="mt-2 break-words text-xs text-red-300">{document.error}</p>}
        <label className="mt-2 flex items-start gap-2 text-xs leading-5 text-zinc-300">
          <input type="checkbox" aria-label={`加入对比：${document.name}`} checked={compareDocuments.some((d) => d.id === document.id)} disabled={selectionLocked || loading || document.status !== "ready" || working.includes(document.id) || (compareDocuments.length >= 5 && !compareIds.includes(document.id))} onChange={(event) => toggleCompare(document, event.target.checked)} className="mt-1 shrink-0 accent-blue-500 disabled:opacity-40" />
          <span className="min-w-0 break-words">加入对比：{document.name}</span>
        </label>
        <div className="mt-2 flex flex-wrap gap-3 text-xs text-zinc-400">
          <button type="button" disabled={selectionLocked} onClick={() => select(selectedId === document.id ? null : document)} className="text-blue-300">{selectedId === document.id ? "取消限定" : "用于提问"}</button>
          <button type="button" onClick={() => open(document.id)}>片段 / 原文件</button>
          {document.status === "failed" && <button type="button" disabled={working.includes(document.id)} onClick={() => void mutate(document, "retry")}>重试解析</button>}
          <button type="button" disabled={selectionLocked || working.includes(document.id)} onClick={() => setDeleteId(document.id)} className="text-red-300">删除</button>
        </div>
        {working.includes(document.id) && <p role="status" className="mt-2 text-xs text-zinc-500">正在处理…</p>}
        {deleteId === document.id && <div role="alert" className="mt-3 space-y-2 border-t border-red-900 pt-2 text-xs"><p>确认删除原文件和索引？历史引用将无法打开该文档。</p><div className="flex gap-3"><button type="button" disabled={selectionLocked || working.includes(document.id)} onClick={() => void mutate(document, "delete")} className="text-red-300">确认删除</button><button type="button" disabled={working.includes(document.id)} onClick={() => setDeleteId(null)}>取消</button></div></div>}
      </li>)}</ul>
      <form onSubmit={(event) => void search(event)} className="space-y-2 border-t border-zinc-800 pt-4">
        <label className="block text-xs text-zinc-400">检索 / 提问限定文档
          <select aria-label="限定文档" disabled={selectionLocked} value={selectedId ?? ""} onChange={(event) => select(documents.find((d) => d.id === event.target.value) ?? null)} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 p-2 text-xs text-zinc-200">
            <option value="">全部项目资料</option>{documents.map((d) => <option key={d.id} value={d.id}>{d.name} · {STATUS[d.status]}</option>)}
          </select>
        </label>
        <div className="flex gap-2"><input aria-label="检索本地资料" placeholder="检索本地片段…" value={query} onChange={(event) => setQuery(event.target.value)} maxLength={500} className="min-w-0 flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-2 text-xs outline-none focus:border-blue-500" /><button type="submit" disabled={!query.trim() || searching} className="rounded bg-zinc-200 px-3 py-2 text-xs text-zinc-900">{searching ? "检索中…" : "检索"}</button></div>
        {searchError && <p role="alert" className="text-xs text-red-300">{searchError} 可重新点击检索。</p>}
        {results && <div className="space-y-2"><p role="status" className="text-xs text-zinc-500">{results.length ? `${results.length} 个本地匹配片段` : "没有匹配片段。尝试更具体的关键词，或检查索引状态。"}</p>{results.map((chunk) => <button key={chunk.id} type="button" onClick={() => open(chunk.document_id, chunk.id)} className="block w-full rounded border border-zinc-800 p-2 text-left"><span className="block text-xs text-blue-300">{chunk.name} · {chunk.location}</span><span className="mt-1 line-clamp-3 block whitespace-pre-wrap break-words text-xs leading-5 text-zinc-400">{chunk.text}</span></button>)}</div>}
      </form>
      {selectionLocked && <p role="status" className="text-[11px] text-amber-300">回答期间资料范围已锁定；停止生成后可更换文档或删除资料。</p>}
      {preview && <DocumentPreview key={preview.nonce} projectId={projectId} target={preview} selectionLocked={selectionLocked} onSelect={select} onClose={() => setPreview(null)} />}
    </div>
  );
}
