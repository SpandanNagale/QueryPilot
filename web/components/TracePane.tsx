"use client";

import { ChatState } from "@/lib/pipeline";
import { TraceNode } from "./TraceNode";

export function TracePane({ chat }: { chat: ChatState }) {
  return (
    <section className="flex flex-col w-full lg:w-[34%] lg:min-w-[320px] border-b lg:border-b-0 lg:border-r border-border-0 min-h-0">
      <header className="px-4 py-3 border-b border-border-0 shrink-0">
        <h2 className="text-sm font-medium text-ink-0">Agent trace</h2>
        <p className="text-[11px] text-ink-2 font-telemetry mt-0.5">Live LangGraph node pipeline</p>
      </header>
      <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-2">
        {chat.nodeOrder.length === 0 && chat.status === "idle" && (
          <p className="text-xs font-telemetry text-ink-2 px-1 py-4">
            Ask a question in the left pane to see the pipeline run here.
          </p>
        )}
        {chat.nodeOrder.map((node) => (
          <TraceNode key={node} info={chat.nodes[node]} />
        ))}
        {chat.status === "running" && chat.lastHeartbeatAtMs && (
          <p className="text-[11px] font-telemetry text-ink-2 px-1 animate-qp-pulse">still working…</p>
        )}
      </div>
    </section>
  );
}
