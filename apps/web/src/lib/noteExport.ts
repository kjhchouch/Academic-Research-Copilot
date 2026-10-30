import type { ResearchNote } from "./workspaceApi";

export function unresolvedMarkers(note: Pick<ResearchNote, "body" | "sources" | "citations">): string[] {
  const available = new Set([...note.sources.map((s) => s.source_id), ...note.citations.map((c) => String(c.citation_id))]);
  return [...new Set(Array.from(note.body.matchAll(/\[(K\d+|\d+)\]/g), (m) => m[1]))].filter((id) => !available.has(id));
}

function literal(text: string): string {
  const longest = Math.max(2, ...Array.from(text.matchAll(/`+/g), (m) => m[0].length));
  const fence = "`".repeat(longest + 1);
  return `${fence}text\n${text}\n${fence}`;
}

export function noteMarkdown(note: ResearchNote): string {
  const missing = unresolvedMarkers(note);
  const metadata = [
    `标题：${note.title}`,
    `笔记版本：${note.version} · 保存时间：${note.updated_at}`,
    `项目：${note.project_id}`,
    ...(note.origin_session_id ? [`原会话：${note.origin_session_id} · 原消息：${note.origin_message_id}`] : []),
  ].join("\n");
  const sections = [literal(metadata), "> 阅读笔记可经人工修改；模型归纳未经独立事实核验。以下仅包含采纳时的来源快照，不代表完整论文或最新原文。", note.body];
  if (missing.length) sections.push(`> 注意：正文中 ${missing.map((id) => `[${id}]`).join("、")} 没有对应的来源快照，请核对后使用。`);
  if (note.sources.length) sections.push("## 资料来源快照", ...note.sources.map((source) => literal(`[${source.source_id}] ${source.name}\n${source.location}\n文档：${source.document_id} · 片段：${source.id}\n\n${source.text}`)));
  if (note.citations.length) sections.push("## 公开参考文献", ...note.citations.map((c) => literal(`[${c.citation_id}] ${c.title}\n${c.authors.join(", ")} · ${c.year ?? "年份未知"} · ${c.venue ?? ""}\nDOI: ${c.doi ?? "未提供"}`)));
  return `${sections.join("\n\n")}\n`;
}

export function noteFilename(title: string): string {
  const safe = title.replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_").replace(/[. ]+$/g, "").slice(0, 100);
  return `阅读笔记-${safe || "未命名"}.md`;
}
