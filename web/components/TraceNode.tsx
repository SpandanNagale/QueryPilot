"use client";

import { useState } from "react";
import { nodeLabel } from "@/lib/nodeStateStyle";
import { NODE_STATE_STYLE } from "@/lib/nodeStateStyle";
import { NodeTraceEntry } from "@/lib/pipeline";

function durationLabel(info: NodeTraceEntry): string | null {
  if (info.startedAtMs == null) return null;
  const end = info.finishedAtMs ?? Date.now();
  return `${((end - info.startedAtMs) / 1000).toFixed(1)}s`;
}

export function TraceNode({ info }: { info: NodeTraceEntry }) {
  const [expanded, setExpanded] = useState(false);
  const style = NODE_STATE_STYLE[info.state];
  const duration = durationLabel(info);
  const lastAttempt = info.attempts[info.attempts.length - 1];
  const canExpand =
    info.attempts.length > 0 ||
    info.cacheHits.length > 0 ||
    (info.validatorChecks?.length ?? 0) > 0 ||
    info.guardrailVerdict != null;

  return (
    <div className={`rounded-lg border bg-surface-1 ${style.ring} transition-colors`}>
      <button
        type="button"
        onClick={() => canExpand && setExpanded((v) => !v)}
        className={`w-full flex items-center gap-3 px-3 py-2.5 text-left ${canExpand ? "cursor-pointer" : "cursor-default"}`}
        aria-expanded={expanded}
      >
        <span className={`inline-block size-2.5 rounded-full shrink-0 ${style.dot}`} />
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="text-sm font-medium text-ink-0">{nodeLabel(info.node)}</span>
            <span className={`text-[11px] font-telemetry ${style.text}`}>{style.label}</span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-0.5 text-[11px] text-ink-2 font-telemetry">
            {duration && <span>{duration}</span>}
            {lastAttempt?.provider && <span>{lastAttempt.provider}</span>}
            {info.cacheHits.length > 0 && (
              <span className="rounded bg-teal-dim text-teal px-1.5 py-0.5">cached</span>
            )}
          </div>
        </div>
        {canExpand && <span className="text-ink-2 text-xs shrink-0">{expanded ? "▲" : "▼"}</span>}
      </button>

      {expanded && (
        <div className="border-t border-border-0 px-3 py-2.5 text-[11px] font-telemetry text-ink-1 flex flex-col gap-1.5">
          {info.guardrailVerdict && (
            <div className={info.guardrailVerdict.blocked ? "text-coral" : "text-teal"}>
              guardrail: {info.guardrailVerdict.blocked ? `blocked — ${info.guardrailVerdict.reason}` : "passed"}
            </div>
          )}
          {info.validatorChecks?.map((c, i) => (
            <div key={i} className={c.passed ? "text-teal" : "text-coral"}>
              {c.name}: {c.passed ? "passed" : "failed"}
              {c.injected !== undefined && !c.injected && " (already present)"}
            </div>
          ))}
          {info.attempts.map((a, i) => (
            <div key={i} className="flex flex-wrap gap-x-2 gap-y-0.5">
              <span className="text-ink-2">{a.alias}</span>
              <span>{a.provider}/{a.model}</span>
              <span className={a.success ? "text-teal" : "text-coral"}>{a.success ? "ok" : a.error ?? "failed"}</span>
              {a.latency_ms != null && <span>{a.latency_ms.toFixed(0)}ms</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
