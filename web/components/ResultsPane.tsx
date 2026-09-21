"use client";

import { useState } from "react";
import { ChatState } from "@/lib/pipeline";
import { SqlHighlight } from "@/lib/sqlHighlight";
import { AuditTab } from "./AuditTab";
import { ChartView } from "./ChartView";
import { ConfirmationCard } from "./ConfirmationCard";
import { ErrorBanner } from "./ErrorBanner";
import { ResultsTable } from "./ResultsTable";
import { RunSummary } from "./RunSummary";

export function ResultsPane({
  chat,
  sessionId,
  onConfirm,
  onCancel,
  onRetry,
  onResetSandbox,
  onShowByok,
  onFollowupClick,
  auditRefreshKey,
}: {
  chat: ChatState;
  sessionId: string;
  onConfirm: () => void;
  onCancel: () => void;
  onRetry: () => void;
  onResetSandbox: () => void;
  onShowByok: () => void;
  onFollowupClick: (question: string) => void;
  auditRefreshKey: number;
}) {
  const [tab, setTab] = useState<"results" | "audit">("results");

  return (
    <section className="flex flex-col flex-1 min-h-0">
      <header className="px-4 py-3 border-b border-border-0 shrink-0 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-medium text-ink-0">Results</h2>
          <p className="text-[11px] text-ink-2 font-telemetry mt-0.5">Generated SQL, output, and audit trail</p>
        </div>
        <div className="flex gap-1 text-xs font-telemetry" role="tablist">
          {(["results", "audit"] as const).map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={`rounded-md px-2.5 py-1 ${
                tab === t ? "bg-surface-2 text-ink-0" : "text-ink-2 hover:text-ink-1"
              }`}
            >
              {t === "results" ? "Results" : "Audit"}
            </button>
          ))}
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-4">
        {tab === "audit" ? (
          <AuditTab sessionId={sessionId} refreshKey={auditRefreshKey} />
        ) : (
          <>
            {chat.status === "idle" && (
              <p className="text-xs font-telemetry text-ink-2">
                Results, generated SQL, and charts will appear here once you ask something.
              </p>
            )}

            {chat.generatedSql && (
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-xs font-telemetry text-ink-2">Generated SQL</span>
                  {chat.validationResult && (
                    <span className={`text-[11px] font-telemetry ${chat.validationResult.passed ? "text-teal" : "text-coral"}`}>
                      {chat.validationResult.passed ? "validated" : "blocked"}
                    </span>
                  )}
                </div>
                <div className="rounded-lg border border-border-0 bg-surface-1 px-3 py-2.5 overflow-x-auto">
                  <SqlHighlight sql={chat.generatedSql} />
                </div>
                {chat.validationResult && chat.validationResult.checks.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mt-2">
                    {chat.validationResult.checks.map((c) => (
                      <span
                        key={c.name}
                        className={`rounded px-1.5 py-0.5 text-[10px] font-telemetry ${
                          c.passed ? "bg-teal-dim text-teal" : "bg-coral-dim text-coral"
                        }`}
                      >
                        {c.name.replace(/_/g, " ")}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}

            {chat.errors.map((err, i) => (
              <ErrorBanner
                key={i}
                error={err}
                onRetry={onRetry}
                onResetSandbox={onResetSandbox}
                onShowByok={onShowByok}
              />
            ))}

            {chat.status === "awaiting_confirmation" && chat.preview && (
              <ConfirmationCard
                key={chat.userMessage ?? "confirmation"}
                preview={chat.preview}
                generatedSql={chat.generatedSql}
                timeoutSeconds={chat.confirmationTimeoutSeconds}
                onConfirm={onConfirm}
                onCancel={onCancel}
              />
            )}

            {chat.finalResponse && (
              <p className="text-sm text-ink-0 rounded-lg border border-border-0 bg-surface-1 px-3.5 py-3">
                {chat.finalResponse}
              </p>
            )}

            {/* A write's execution_result carries no rows worth tabulating
                (row_count without a real result set) -- the final_response
                message above already says what happened. */}
            {chat.executionResult && chat.executionResult.columns.length > 0 && (
              <div>
                <span className="text-xs font-telemetry text-ink-2 mb-1.5 block">
                  {chat.executionResult.row_count} row(s)
                </span>
                <ResultsTable
                  columns={chat.executionResult.columns}
                  rows={chat.executionResult.rows}
                  truncated={chat.executionResult.truncated}
                />
              </div>
            )}

            {chat.chartSpec && chat.executionResult && (
              <ChartView spec={chat.chartSpec} rows={chat.executionResult.rows} />
            )}

            {chat.insightText && (
              <div className="rounded-lg border border-border-0 bg-surface-1 px-3.5 py-3">
                <p className="prose-insight text-sm text-ink-0 leading-relaxed">{chat.insightText}</p>
              </div>
            )}

            {chat.followups.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {chat.followups.map((f, i) => (
                  <button
                    key={i}
                    onClick={() => onFollowupClick(f)}
                    className="rounded-full border border-border-1 px-3 py-1 text-xs text-ink-1 font-telemetry hover:border-teal hover:text-teal transition-colors"
                  >
                    {f}
                  </button>
                ))}
              </div>
            )}

            {chat.summary && <RunSummary summary={chat.summary} />}
          </>
        )}
      </div>
    </section>
  );
}
