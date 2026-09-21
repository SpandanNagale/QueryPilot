import {
  AuditEvent,
  BudgetHealth,
  HealthResponse,
  ProviderHealth,
  TableInfo,
  TablePreview,
} from "./types";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

/** Pings the backend with a bounded retry, so a cold-starting Render free
 * tier instance (spins down after 15 minutes idle, ~1 minute cold start)
 * shows a "waking up" state with elapsed time instead of the UI just
 * looking broken. */
export async function waitForBackend(
  onTick?: (elapsedMs: number) => void,
  opts: { timeoutMs?: number; intervalMs?: number } = {}
): Promise<boolean> {
  const timeoutMs = opts.timeoutMs ?? 90_000;
  const intervalMs = opts.intervalMs ?? 2_000;
  const startedAt = Date.now();
  const deadline = startedAt + timeoutMs;

  while (Date.now() < deadline) {
    onTick?.(Date.now() - startedAt);
    try {
      const res = await fetch(apiUrl("/health"), { cache: "no-store" });
      if (res.ok) return true;
    } catch {
      // backend not reachable yet -- keep retrying until the deadline
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return false;
}

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch(apiUrl("/health"), { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /health failed: ${res.status}`);
  return res.json();
}

// Real, billable pings to every configured provider deployment -- call
// sparingly (once on mount, or a manual refresh), never on a timer.
export async function fetchProviderHealth(): Promise<ProviderHealth> {
  const res = await fetch(apiUrl("/health/providers"), { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /health/providers failed: ${res.status}`);
  return res.json();
}

export async function fetchBudgetHealth(): Promise<BudgetHealth> {
  const res = await fetch(apiUrl("/health/budget"), { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /health/budget failed: ${res.status}`);
  return res.json();
}

export async function fetchSchema(): Promise<{ schema: string; tour: string }> {
  const res = await fetch(apiUrl("/schema"), { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /schema failed: ${res.status}`);
  return res.json();
}

export async function fetchTables(sessionId: string): Promise<{ tables: TableInfo[] }> {
  const res = await fetch(apiUrl(`/tables?session_id=${encodeURIComponent(sessionId)}`), { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /tables failed: ${res.status}`);
  return res.json();
}

export async function fetchTablePreview(
  sessionId: string,
  tableName: string,
  opts: { limit?: number; offset?: number } = {}
): Promise<TablePreview> {
  const params = new URLSearchParams({
    session_id: sessionId,
    limit: String(opts.limit ?? 50),
    offset: String(opts.offset ?? 0),
  });
  const res = await fetch(apiUrl(`/tables/${encodeURIComponent(tableName)}/preview?${params}`), { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /tables/${tableName}/preview failed: ${res.status}`);
  return res.json();
}

export async function fetchAudit(sessionId: string, limit = 50): Promise<{ events: AuditEvent[] }> {
  const params = new URLSearchParams({ session_id: sessionId, limit: String(limit) });
  const res = await fetch(apiUrl(`/audit?${params}`), { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /audit failed: ${res.status}`);
  return res.json();
}

export async function resetSession(sessionId: string): Promise<void> {
  const res = await fetch(apiUrl("/session/reset"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!res.ok) throw new Error(`POST /session/reset failed: ${res.status}`);
}

export interface ByokCredentials {
  provider: string;
  apiKey: string;
}

/** Raw HTTPException detail shape ({code, message}) FastAPI returns for a
 * non-2xx /chat or /confirm response -- these never enter the SSE stream
 * (rate limit, invalid session, budget exceeded all reject before the
 * graph runs at all). */
export interface HttpErrorDetail {
  code: string;
  message: string;
}

export function startChatStream(
  sessionId: string,
  message: string,
  opts: { byok?: ByokCredentials; signal?: AbortSignal } = {}
): Promise<Response> {
  return fetch(apiUrl("/chat"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: sessionId,
      message,
      byok_provider: opts.byok?.provider || null,
      byok_api_key: opts.byok?.apiKey || null,
    }),
    signal: opts.signal,
  });
}

export function startConfirmStream(
  sessionId: string,
  confirmed: boolean,
  opts: { signal?: AbortSignal } = {}
): Promise<Response> {
  return fetch(apiUrl("/confirm"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, confirmed }),
    signal: opts.signal,
  });
}
