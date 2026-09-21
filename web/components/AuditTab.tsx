"use client";

import { useEffect, useState } from "react";
import { fetchAudit } from "@/lib/api";
import { AuditEvent } from "@/lib/types";

export function AuditTab({ sessionId, refreshKey }: { sessionId: string; refreshKey: number }) {
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    fetchAudit(sessionId)
      .then((res) => {
        if (!cancelled) setEvents(res.events);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, refreshKey]);

  if (error) return <p className="text-xs font-telemetry text-coral">Couldn&apos;t load audit log: {error}</p>;
  if (!events) return <p className="text-xs font-telemetry text-ink-2">Loading…</p>;
  if (events.length === 0) return <p className="text-xs font-telemetry text-ink-2">No events yet this session.</p>;

  return (
    <ul className="flex flex-col gap-2 text-[11px] font-telemetry">
      {events.map((e, i) => (
        <li key={i} className="rounded-md border border-border-0 bg-surface-1 px-2.5 py-2">
          <div className="flex items-center justify-between text-ink-1">
            <span className="text-ink-0">{e.event}</span>
            <span className="text-ink-2">{new Date(e.timestamp).toLocaleTimeString()}</span>
          </div>
          {typeof e.generated_sql === "string" && (
            <p className="text-ink-2 mt-1 break-words">{e.generated_sql}</p>
          )}
          {typeof e.reason === "string" && <p className="text-coral mt-1">{e.reason}</p>}
        </li>
      ))}
    </ul>
  );
}
