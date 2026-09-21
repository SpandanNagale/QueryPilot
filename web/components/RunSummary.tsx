import { RunSummary as RunSummaryType } from "@/lib/types";

export function RunSummary({ summary }: { summary: RunSummaryType }) {
  const providers = [...new Set(summary.llm_attempts.filter((a) => a.success).map((a) => a.provider))];
  const totalTokens = summary.llm_attempts.reduce(
    (sum, a) => sum + (a.prompt_tokens ?? 0) + (a.completion_tokens ?? 0),
    0
  );
  const totalCost = summary.llm_attempts.reduce((sum, a) => sum + (a.cost_usd ?? 0), 0);

  return (
    <div className="rounded-lg border border-border-0 bg-surface-1 px-3 py-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] font-telemetry text-ink-1">
      <span>{(summary.total_latency_ms / 1000).toFixed(1)}s total</span>
      {providers.length > 0 && <span>via {providers.join(", ")}</span>}
      {totalTokens > 0 && <span>{totalTokens} tok</span>}
      {totalCost > 0 && <span>${totalCost.toFixed(4)}</span>}
      {summary.cache_hits > 0 && (
        <span className="text-teal">
          {summary.cache_hits} cache hit{summary.cache_hits > 1 ? "s" : ""} (
          {summary.cache_seconds_saved.toFixed(1)}s saved)
        </span>
      )}
      {summary.trace_url && (
        <a href={summary.trace_url} target="_blank" rel="noreferrer" className="text-teal underline underline-offset-2">
          view trace
        </a>
      )}
    </div>
  );
}
