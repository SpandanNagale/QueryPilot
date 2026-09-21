"use client";

import { useEffect, useRef, useState } from "react";
import { resetSession, waitForBackend } from "@/lib/api";
import { getOrCreateSessionId, replaceSessionId } from "@/lib/session";
import { useChat } from "@/lib/useChat";
import { ResultsPane } from "./ResultsPane";
import { SandboxPane } from "./SandboxPane";
import { TracePane } from "./TracePane";

export function Dashboard() {
  // Lazy initializer, not an effect: getOrCreateSessionId() reads/writes
  // localStorage, which only exists client-side -- the function itself
  // no-ops (returns "") when called during server rendering.
  const [sessionId, setSessionId] = useState(() => getOrCreateSessionId());
  const [backendState, setBackendState] = useState<"waking" | "awake" | "unreachable">("waking");
  const [elapsedMs, setElapsedMs] = useState(0);
  const [tablesRefreshKey, setTablesRefreshKey] = useState(0);
  const [auditRefreshKey, setAuditRefreshKey] = useState(0);
  const lastMessageRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    waitForBackend((ms) => !cancelled && setElapsedMs(ms)).then((ok) => {
      if (cancelled) return;
      setBackendState(ok ? "awake" : "unreachable");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const { state, sendMessage, confirm, cancelStream } = useChat(sessionId);
  const [prevStatus, setPrevStatus] = useState(state.status);

  function handleSend(message: string, opts?: { byok?: { provider: string; apiKey: string } }) {
    lastMessageRef.current = message;
    sendMessage(message, opts);
  }

  function handleConfirm() {
    confirm(true);
  }

  function handleCancel() {
    confirm(false);
  }

  function handleRetry() {
    if (lastMessageRef.current) sendMessage(lastMessageRef.current);
  }

  async function handleResetSandbox() {
    cancelStream();
    await resetSession(sessionId).catch(() => {});
    const fresh = replaceSessionId();
    setSessionId(fresh);
    setTablesRefreshKey((k) => k + 1);
    setAuditRefreshKey((k) => k + 1);
  }

  // A confirm/cancel resolution, or a completed manipulation, changes what
  // the sandbox's own data and audit log look like -- refresh both once a
  // turn settles. "Adjusting state when a prop/state changes" during render
  // (React's own documented pattern for this), not inside an effect: it
  // reacts to the exact transition into done/cancelled, once, without an
  // extra commit-then-effect round trip.
  if (state.status !== prevStatus) {
    setPrevStatus(state.status);
    if (state.status === "done" || state.status === "cancelled") {
      setTablesRefreshKey((k) => k + 1);
      setAuditRefreshKey((k) => k + 1);
    }
  }

  if (backendState !== "awake") {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="text-center font-telemetry text-sm text-ink-1 max-w-sm">
          {backendState === "unreachable" ? (
            <p className="text-coral">
              Can&apos;t reach the backend after {Math.round(elapsedMs / 1000)}s. Confirm it&apos;s running and
              NEXT_PUBLIC_API_BASE_URL is correct, then reload.
            </p>
          ) : (
            <>
              <p>
                Waking up the backend<span className="animate-qp-pulse">…</span>
              </p>
              <p className="text-ink-2 mt-2">
                Render&apos;s free tier spins down after 15 minutes idle — this can take up to a minute.
              </p>
              <p className="text-ink-2 mt-1">{Math.round(elapsedMs / 1000)}s elapsed</p>
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col lg:flex-row flex-1 min-h-0">
      <SandboxPane
        sessionId={sessionId}
        running={state.status === "running"}
        onSend={handleSend}
        onResetSandbox={handleResetSandbox}
        tablesRefreshKey={tablesRefreshKey}
      />
      <TracePane chat={state} />
      <ResultsPane
        chat={state}
        sessionId={sessionId}
        onConfirm={handleConfirm}
        onCancel={handleCancel}
        onRetry={handleRetry}
        onResetSandbox={handleResetSandbox}
        onShowByok={() =>
          document.getElementById("byok-section")?.scrollIntoView({ behavior: "smooth", block: "center" })
        }
        onFollowupClick={handleSend}
        auditRefreshKey={auditRefreshKey}
      />
    </div>
  );
}
