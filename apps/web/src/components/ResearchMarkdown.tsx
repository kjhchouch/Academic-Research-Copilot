"use client";

import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Citation } from "@/lib/api";
import type { KnowledgeSource } from "@/lib/workspaceApi";
import { CitationChip, KnowledgeChip } from "@/components/ChatChips";

type MarkdownNode = { type: string; value?: string; url?: string; children?: MarkdownNode[] };
function citationMarkers() {
  return (tree: MarkdownNode) => {
    function visit(node: MarkdownNode) {
      if (!node.children || ["link", "linkReference", "code", "inlineCode", "html"].includes(node.type)) return;
      node.children = node.children.flatMap((child) => {
        if (child.type !== "text" || !child.value) { visit(child); return [child]; }
        const parts: MarkdownNode[] = [];
        let start = 0;
        for (const match of child.value.matchAll(/\[(K\d+|\d+)\]/g)) {
          if (match.index > start) parts.push({ type: "text", value: child.value.slice(start, match.index) });
          parts.push({ type: "link", url: `#evidence-${match[1]}`, children: [{ type: "text", value: match[0] }] });
          start = match.index + match[0].length;
        }
        if (start < child.value.length) parts.push({ type: "text", value: child.value.slice(start) });
        return parts;
      });
    }
    visit(tree);
  };
}

export default function ResearchMarkdown({ text, sources = [], citations = [], onSource }: {
  text: string;
  sources?: KnowledgeSource[];
  citations?: Citation[];
  onSource: (source: KnowledgeSource) => void;
}) {
  const sourceLookup = new Map(sources.map((s) => [s.source_id, s]));
  const citationLookup = new Map(citations.map((c) => [String(c.citation_id), c]));
  return <div className="min-w-0 space-y-3 break-words text-sm leading-7">
    <Markdown skipHtml remarkPlugins={[remarkGfm, citationMarkers]} components={{
      h1: ({ children }) => <h3 className="mt-4 text-lg font-semibold">{children}</h3>,
      h2: ({ children }) => <h4 className="mt-4 font-semibold text-zinc-100">{children}</h4>,
      h3: ({ children }) => <h5 className="mt-3 font-semibold">{children}</h5>,
      p: ({ children }) => <p className="whitespace-pre-wrap">{children}</p>,
      ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
      ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5">{children}</ol>,
      blockquote: ({ children }) => <blockquote className="border-l-2 border-zinc-600 pl-3 text-zinc-400">{children}</blockquote>,
      table: ({ children }) => <div className="max-w-full overflow-x-auto"><table className="w-full border-collapse text-left text-xs leading-6">{children}</table></div>,
      th: ({ children }) => <th className="min-w-24 border border-zinc-700 bg-zinc-800/70 px-2 py-2 align-top font-medium">{children}</th>,
      td: ({ children }) => <td className="min-w-24 border border-zinc-700 px-2 py-2 align-top">{children}</td>,
      pre: ({ children }) => <pre className="max-w-full overflow-x-auto rounded bg-zinc-950 p-3 text-xs leading-5">{children}</pre>,
      code: ({ children }) => <code className="rounded bg-zinc-950 px-1 font-mono text-xs">{children}</code>,
      img: ({ alt }) => <span className="text-xs text-zinc-500">[图片未自动加载：{alt || "无说明"}]</span>,
      a: ({ href, children }) => {
        const id = href?.match(/^#evidence-(K\d+|\d+)$/)?.[1];
        if (id) {
          const source = sourceLookup.get(id);
          if (source) return <KnowledgeChip source={source} onOpen={onSource} />;
          const citation = citationLookup.get(id);
          if (citation) return <CitationChip citation={citation} />;
          return <span title="当前内容未附此编号的来源，请核查" className="text-amber-300">[{id}]</span>;
        }
        if (!href || !/^https?:\/\//i.test(href)) return <span>{children}</span>;
        return <a href={href} target="_blank" rel="noopener noreferrer" className="break-all text-blue-300 underline">{children}</a>;
      },
    }}>{text}</Markdown>
  </div>;
}
