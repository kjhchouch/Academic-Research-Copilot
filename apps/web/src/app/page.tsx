import { Suspense } from "react";
import ChatPanel from "@/components/ChatPanel";

/**
 * Prerendered shell shown while ChatPanel streams in.
 *
 * The Suspense boundary is not decoration. With `cacheComponents: true`, Next 16
 * prerenders Client Components at build time and hard-fails on unstable values —
 * and `useChat` calls Math.random() to mint a chat id when none is given. Deferring
 * ChatPanel behind Suspense means that call only ever runs in the browser
 * (nextjs.org/docs/messages/blocking-prerender-random-client).
 */
function ChatPanelSkeleton() {
  return (
    <div className="flex h-dvh flex-col bg-zinc-950 text-zinc-100">
      <header className="flex items-center justify-between border-b border-zinc-800 px-5 py-3">
        <div>
          <h1 className="text-sm font-semibold">Academic Research Copilot</h1>
          <p className="font-mono text-[11px] text-zinc-500">thread --------</p>
        </div>
        <span className="rounded bg-zinc-700 px-2 py-1 font-mono text-[11px] text-zinc-300">
          loading
        </span>
      </header>
      <div className="flex flex-1 items-center justify-center">
        <p className="text-sm text-zinc-600">正在连接后端…</p>
      </div>
    </div>
  );
}

export default function Home() {
  return (
    <Suspense fallback={<ChatPanelSkeleton />}>
      <ChatPanel />
    </Suspense>
  );
}
