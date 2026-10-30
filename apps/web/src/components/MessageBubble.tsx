"use client";

import type { UIMessage } from "ai";
import type { KnowledgeCoverage, KnowledgeSource } from "@/lib/workspaceApi";
import { activityOf, citationsOf, knowledgeSourcesOf, routeOf, turnStatusOf } from "@/components/chatParts";
import { KnowledgeChip } from "@/components/ChatChips";
import ResearchMarkdown from "@/components/ResearchMarkdown";
import RoutePanel from "@/components/panels/RoutePanel";
import ActivityPanel from "@/components/panels/ActivityPanel";
import ReferencesPanel from "@/components/panels/ReferencesPanel";

export default function MessageBubble({ message, isStreaming, onSource, onSaveNote, savingDisabled }: {
  message: UIMessage;
  isStreaming: boolean;
  onSource: (source: KnowledgeSource) => void;
  onSaveNote: (message: UIMessage) => void;
  savingDisabled: boolean;
}) {
  const citations = citationsOf(message);
  const sources = knowledgeSourcesOf(message);
  const assistant = message.role === "assistant";
  const route = assistant ? routeOf(message) : null;
  const activity = assistant ? activityOf([message]) : [];
  const turn = turnStatusOf(message);
  const partial = turn && (turn.status === "interrupted" || turn.status === "error");
  const text = message.parts.filter((part) => part.type === "text").map((part) => part.text).join("\n\n");
  const coveragePart = message.parts.find((part) => part.type === "data-knowledge-coverage");
  const coverage = coveragePart ? (coveragePart as unknown as { data: KnowledgeCoverage }).data : null;

  return (
    <article aria-label={assistant ? "助手回复" : "你的消息"} className={`flex ${assistant ? "justify-start" : "justify-end"}`}>
      <div className={`min-w-0 max-w-[95%] rounded-xl px-3.5 py-3 text-sm leading-relaxed sm:max-w-[88%] ${assistant ? "border border-zinc-800 bg-zinc-900 text-zinc-200" : "bg-blue-600 text-white"}`}>
        {coverage?.task === "compare" && <div className="mb-3 space-y-1 rounded border border-zinc-700 bg-zinc-950/50 p-2 text-[11px] leading-5">
          <p className="font-medium text-zinc-300">逐篇取证 · 只比较本轮检索到的片段</p>
          {coverage.documents.map((doc) => <p key={doc.document_id} className={doc.status === "missing" ? "text-amber-300" : "text-zinc-400"}>{doc.name} · {doc.status === "missing" ? "未检索到证据，不能据此比较" : `${doc.source_ids.length} 个片段（非全文核查）`}</p>)}
        </div>}
        {assistant ? <ResearchMarkdown text={text} sources={sources} citations={citations} onSource={onSource} /> : <p className="whitespace-pre-wrap break-words">{text}</p>}
        {isStreaming && <span className="ml-0.5 animate-pulse text-zinc-500">▍</span>}
        {partial && <p role="status" className="mt-3 rounded border border-amber-800 bg-amber-950/30 px-2 py-1.5 text-xs text-amber-200">{turn.status === "interrupted" ? "本轮已中断，回答可能不完整。" : "本轮发生错误，以下为已保存的部分内容。"}{turn.message && ` ${turn.message}`}</p>}
        {assistant && sources.length > 0 && <div className="mt-3 border-t border-zinc-800 pt-2"><p className="mb-1 text-[10px] text-zinc-500">本轮检索片段 · 点击定位原文 · 不代表结论已验证</p><div className="flex flex-wrap gap-1">{sources.map((source) => <span key={source.source_id} className="inline-flex min-w-0 max-w-full items-center gap-1"><KnowledgeChip source={source} onOpen={onSource} /><span className="truncate text-[10px] text-zinc-500">{source.name}</span></span>)}</div></div>}
        {assistant && citations.length > 0 && <details className="mt-3 border-t border-zinc-800 pt-2"><summary className="cursor-pointer text-xs text-blue-300">本轮参考文献 · {citations.length}</summary><ReferencesPanel citations={citations} /></details>}
        {assistant && text && turn?.status === "complete" && route?.guard !== "refuse" && route?.guard !== "clarify" && <button type="button" disabled={isStreaming || savingDisabled} onClick={() => onSaveNote(message)} className="mt-3 rounded border border-blue-800 px-3 py-1.5 text-xs text-blue-300 hover:bg-blue-950/40">保存为阅读笔记</button>}
        {assistant && (route || activity.length > 0) && <details className="mt-3 border-t border-zinc-800 pt-2"><summary className="cursor-pointer text-xs text-zinc-400">执行详情{activity.length > 0 ? ` · ${activity.length} 次工具调用` : " · 路由决策"}{isStreaming ? " · 执行中" : ""}</summary><div className="mt-2 space-y-2">{route && <RoutePanel routes={[route]} />}{activity.length > 0 && <ActivityPanel rows={activity} />}</div></details>}
      </div>
    </article>
  );
}
