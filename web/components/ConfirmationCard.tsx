"use client";

import { useEffect, useState } from "react";
import { SqlHighlight } from "@/lib/sqlHighlight";
import { PreviewInfo } from "@/lib/types";
import { ResultsTable } from "./ResultsTable";

export function ConfirmationCard({
  preview,
  generatedSql,
  timeoutSeconds,
  onConfirm,
  onCancel,
}: {
  preview: PreviewInfo;
  generatedSql: string | null;
  timeoutSeconds: number | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const total = timeoutSeconds ?? 60;
  // Initialized from `total` via the lazy initializer, not reset inside the
  // effect below -- the parent keys each ConfirmationCard instance fresh
  // per confirmation turn (see ResultsPane), so a genuinely new `total`
  // always arrives on a fresh mount, not a prop change on a reused instance.
  const [remaining, setRemaining] = useState(total);

  useEffect(() => {
    const startedAt = Date.now();
    const id = setInterval(() => {
      const left = Math.max(0, total - Math.floor((Date.now() - startedAt) / 1000));
      setRemaining(left);
      if (left <= 0) {
        clearInterval(id);
        onCancel();
      }
    }, 250);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [total]);

  const pct = Math.max(0, Math.min(100, (remaining / total) * 100));

  return (
    <div className="rounded-lg border border-amber bg-amber-dim/30 p-4 flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-ink-0">Confirm this change?</h3>
        <span className="text-xs font-telemetry text-amber">{remaining}s</span>
      </div>
      <div className="h-1 rounded bg-surface-2 overflow-hidden">
        <div className="h-full bg-amber transition-[width] duration-200" style={{ width: `${pct}%` }} />
      </div>

      {generatedSql && (
        <div className="rounded-md bg-surface-2 px-3 py-2 overflow-x-auto">
          <SqlHighlight sql={generatedSql} />
        </div>
      )}

      <div className="text-xs font-telemetry text-ink-1">
        <span className="text-ink-0 font-medium">{preview.operation}</span> on{" "}
        <span className="text-ink-0 font-medium">{preview.table}</span> — {preview.row_count} row(s) affected
      </div>

      {preview.operation === "INSERT" && preview.insert_values && (
        <ResultsTable
          columns={Object.keys(preview.insert_values)}
          rows={[preview.insert_values]}
        />
      )}
      {preview.operation !== "INSERT" && preview.rows.length > 0 && (
        <ResultsTable columns={Object.keys(preview.rows[0])} rows={preview.rows} />
      )}

      {preview.cascades && preview.cascades.length > 0 && (
        <div className="flex flex-col gap-2">
          <p className="text-xs font-telemetry text-coral">Cascading deletes:</p>
          {preview.cascades.map((c) => (
            <div key={c.table} className="rounded-md border border-coral-dim bg-surface-2 p-2">
              <p className="text-[11px] font-telemetry text-ink-1 mb-1">
                {c.row_count} row(s) in <span className="text-coral">{c.table}</span> via {c.column}
              </p>
              <ResultsTable columns={Object.keys(c.rows[0] ?? {})} rows={c.rows} />
            </div>
          ))}
        </div>
      )}

      <div className="flex gap-2 mt-1">
        <button
          onClick={onConfirm}
          className="rounded-md bg-teal px-4 py-2 text-sm font-medium text-surface-0 hover:opacity-90"
        >
          Confirm
        </button>
        <button
          onClick={onCancel}
          className="rounded-md border border-border-1 px-4 py-2 text-sm font-medium text-ink-0 hover:bg-surface-2"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
