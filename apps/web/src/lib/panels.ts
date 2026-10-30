export type PanelId = "sessions" | "references" | "outcomes";

export const PANELS: readonly PanelId[] = ["sessions", "references", "outcomes"];
export const PANEL_META: Record<PanelId, { name: string; hint: string }> = {
  sessions: { name: "会话", hint: "当前项目的会话与历史" },
  references: { name: "资料库", hint: "上传、对比资料与核查原文" },
  outcomes: { name: "成果", hint: "阅读笔记与项目大纲" },
};
