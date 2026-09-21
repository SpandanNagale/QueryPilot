"use client";

import { useEffect, useState } from "react";
import { fetchTablePreview, fetchTables } from "@/lib/api";
import { TableInfo, TablePreview } from "@/lib/types";
import { ResultsTable } from "./ResultsTable";

export function TableBrowser({ sessionId, refreshKey }: { sessionId: string; refreshKey: number }) {
  const [tables, setTables] = useState<TableInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openTable, setOpenTable] = useState<string | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    fetchTables(sessionId)
      .then((res) => {
        if (!cancelled) setTables(res.tables);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, refreshKey]);

  return (
    <div>
      <h3 className="text-xs font-telemetry text-ink-2 mb-2">Tables</h3>
      {error && <p className="text-xs text-coral">{error}</p>}
      {!tables && !error && <p className="text-xs font-telemetry text-ink-2">Loading…</p>}
      <ul className="flex flex-col gap-1">
        {tables?.map((t) => (
          <li key={t.name}>
            <button
              onClick={() => setOpenTable(t.name)}
              className="w-full flex items-center justify-between rounded-md px-2.5 py-1.5 text-left text-xs font-telemetry text-ink-1 hover:bg-surface-2"
            >
              <span className="text-ink-0">{t.name}</span>
              <span className="text-ink-2">{t.row_count} rows</span>
            </button>
          </li>
        ))}
      </ul>
      {openTable && (
        <TablePreviewModal sessionId={sessionId} tableName={openTable} onClose={() => setOpenTable(null)} />
      )}
    </div>
  );
}

function TablePreviewModal({
  sessionId,
  tableName,
  onClose,
}: {
  sessionId: string;
  tableName: string;
  onClose: () => void;
}) {
  const [preview, setPreview] = useState<TablePreview | null>(null);
  const [offset, setOffset] = useState(0);
  const limit = 25;

  useEffect(() => {
    let cancelled = false;
    fetchTablePreview(sessionId, tableName, { limit, offset }).then((res) => {
      if (!cancelled) setPreview(res);
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, tableName, offset]);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`${tableName} preview`}
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl max-h-[80vh] overflow-y-auto rounded-lg border border-border-1 bg-surface-1 p-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium text-ink-0">{tableName}</h3>
          <button onClick={onClose} aria-label="Close" className="text-ink-2 hover:text-ink-0 text-sm">
            ✕
          </button>
        </div>
        {!preview ? (
          <p className="text-xs font-telemetry text-ink-2">Loading…</p>
        ) : (
          <>
            <ResultsTable columns={preview.columns} rows={preview.rows} />
            <div className="flex items-center justify-between mt-3 text-xs font-telemetry text-ink-2">
              <span>
                {offset + 1}-{Math.min(offset + limit, preview.row_count)} of {preview.row_count}
              </span>
              <div className="flex gap-2">
                <button
                  onClick={() => setOffset((o) => Math.max(0, o - limit))}
                  disabled={offset === 0}
                  className="rounded border border-border-1 px-2 py-1 disabled:opacity-30"
                >
                  Prev
                </button>
                <button
                  onClick={() => setOffset((o) => o + limit)}
                  disabled={offset + limit >= preview.row_count}
                  className="rounded border border-border-1 px-2 py-1 disabled:opacity-30"
                >
                  Next
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
