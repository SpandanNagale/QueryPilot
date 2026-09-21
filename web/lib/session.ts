// A per-browser sandbox session id, persisted in localStorage so a reload
// keeps the same sandbox instead of silently starting a new (empty) one.
// crypto.randomUUID() produces a canonical-lowercase UUIDv4 string, exactly
// what app/core/sessions.py::is_valid_session_id() requires -- it's used
// directly as a file path component server-side.
const STORAGE_KEY = "querypilot.session_id";

export function getOrCreateSessionId(): string {
  if (typeof window === "undefined") return "";
  try {
    const existing = window.localStorage.getItem(STORAGE_KEY);
    if (existing) return existing;
  } catch {
    // localStorage unavailable (private mode, blocked site data, etc.) --
    // fall through to an in-memory-only id for this page load.
  }
  const id = crypto.randomUUID();
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // best-effort persistence only
  }
  return id;
}

export function replaceSessionId(): string {
  const id = crypto.randomUUID();
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // best-effort persistence only
  }
  return id;
}
