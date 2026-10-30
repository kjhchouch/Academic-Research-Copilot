import assert from "node:assert/strict";
import test from "node:test";
import { noteFilename, noteMarkdown, unresolvedMarkers } from "./noteExport.ts";

const note = {
  id: "n", project_id: "p", title: "方法比较", body: "样本量37 [K1]。相关研究 [1]。",
  sources: [{ source_id: "K1", id: "chunk", document_id: "doc", project_id: "p", name: "示例.md", location: "第3行", text: "原始样本量37。" }],
  citations: [{ citation_id: 1, title: "Research", authors: ["Author"], year: 2024, venue: null, doi: "10.1234/test" }],
  origin_session_id: "session", origin_message_id: "message", version: 2, updated_at: "2026-10-28", created_at: "2026-10-28",
};

test("exports edited content with immutable source snapshots and provenance", () => {
  const text = noteMarkdown({ ...note, body: "人工修订 [K1]" });
  assert.match(text, /人工修订 \[K1\]/);
  assert.match(text, /原始样本量37/);
  assert.match(text, /原消息：message/);
  assert.match(text, /DOI: 10.1234\/test/);
  assert.match(text, /未经独立事实核验/);
});

test("unresolved markers are deduplicated and never presented as real sources", () => {
  assert.deepEqual(unresolvedMarkers({ ...note, body: "[K1] [K9] [K9] [2] [1]" }), ["K9", "2"]);
  assert.match(noteMarkdown({ ...note, body: "[K99]" }), /没有对应的来源快照/);
});

test("untrusted snapshots cannot escape their literal fenced block", () => {
  const text = noteMarkdown({ ...note, sources: [{ ...note.sources[0], text: "```\n<img src=x>\n````" }] });
  assert.ok(text.includes("`````text\n[K1]"));
  assert.ok(text.includes("```\n<img src=x>\n````\n`````"));
});

test("download names cannot introduce directories or invalid Windows characters", () => {
  const filename = noteFilename('../a\\b:*?<>|"');
  assert.ok(filename.endsWith(".md"));
  assert.doesNotMatch(filename, /[<>:"/\\|?*\u0000-\u001f]/);
  assert.equal(noteFilename(""), "阅读笔记-未命名.md");
});
