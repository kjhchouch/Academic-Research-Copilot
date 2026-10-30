import { Suspense } from "react";
import Workspace from "@/components/Workspace";

export default function Home() {
  return (
    <Suspense fallback={<div className="flex h-dvh items-center justify-center bg-zinc-950 px-6 text-sm text-zinc-400" role="status">正在加载项目工作区…</div>}>
      <Workspace />
    </Suspense>
  );
}
