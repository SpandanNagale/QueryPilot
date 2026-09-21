// Mirrors app/core/node_state.py::NodeState and the SSE event payload shapes
// app/main.py::_run_graph_stream actually emits (NODE_EVENT_MAP, the
// node_state/llm_attempt/cache_hit/guardrail/validator_verdict additions
// from Phase 4, and the "done" run-summary payload).

export type NodeState =
  | "pending"
  | "running"
  | "succeeded"
  | "degraded"
  | "failed"
  | "not_reached"
  | "skipped_cached";

// app/core/errors.py::ErrorCode
export type ErrorCode =
  | "PROVIDER_TIMEOUT"
  | "PROVIDER_RATE_LIMIT"
  | "PROVIDER_AUTH_FAILED"
  | "PROVIDER_UNAVAILABLE"
  | "ALL_PROVIDERS_FAILED"
  | "GUARDRAIL_BLOCKED"
  | "VALIDATION_BLOCKED"
  | "ROW_CEILING_EXCEEDED"
  | "FK_BLOCKED"
  | "SCHEMA_VALIDATION_FAILED"
  | "SESSION_EXPIRED"
  | "SESSION_INVALID"
  | "RATE_LIMITED"
  | "BUDGET_EXCEEDED"
  | "INTERNAL";

export interface ErrorEventPayload {
  code: ErrorCode;
  message: string;
  node: string;
}

export interface NodeStatePayload {
  node: string;
  state: NodeState;
}

export interface GuardrailVerdict {
  blocked: boolean;
  reason?: string | null;
}

export interface ValidatorCheck {
  name: string;
  passed: boolean;
  injected?: boolean;
}

export interface ValidationResult {
  passed: boolean;
  statement_type: string | null;
  reasons: string[];
  checks: ValidatorCheck[];
}

export interface CascadeInfo {
  table: string;
  column: string;
  rows: Record<string, unknown>[];
  row_count: number;
}

export interface PreviewInfo {
  operation: "DELETE" | "UPDATE" | "INSERT";
  table: string;
  rows: Record<string, unknown>[];
  row_count: number;
  insert_values?: Record<string, unknown>;
  cascades?: CascadeInfo[];
}

export interface ExecutionResult {
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
}

export interface ChartSpec {
  type: "line" | "bar" | "pie";
  x: string;
  y: string;
  title: string;
}

export interface LlmAttemptPayload {
  alias: string;
  provider: string;
  model: string;
  latency_ms: number | null;
  success: boolean;
  error?: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  cost_usd: number | null;
}

export interface CacheHitPayload {
  node: string;
  tokens_saved: number | null;
  cost_saved: number | null;
  seconds_saved: number | null;
}

export interface RunSummary {
  run_id: string;
  total_latency_ms: number;
  node_latencies_ms: Record<string, number>;
  llm_attempts: LlmAttemptPayload[];
  cache_hits: number;
  cache_seconds_saved: number;
  trace_url: string | null;
}

// GET /health/providers -- {alias: [{provider, model, status, detail, latency_ms?}, ...]}
// Each call pings every configured deployment for real (app/core/llm_router.py::check_providers),
// so the frontend polls this sparingly -- once on mount plus a manual refresh, never on an interval.
export interface ProviderHealthEntry {
  provider: string | null;
  model: string | null;
  status: "ok" | "error";
  detail?: string;
  latency_ms?: number;
}
export type ProviderHealth = Record<string, ProviderHealthEntry[]>;

export interface BudgetHealth {
  spent: number;
  cap: number;
  exceeded: boolean;
}

export interface HealthResponse {
  healthy: boolean;
  database: { available: boolean; detail: string };
  read_only_mode: boolean;
}

export interface TableColumn {
  name: string;
  type: string;
}

export interface TableInfo {
  name: string;
  purpose: string;
  columns: TableColumn[];
  row_count: number;
}

export interface TablePreview {
  table: string;
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  limit: number;
  offset: number;
}

export interface AuditEvent {
  timestamp: string;
  session_id: string;
  event: string;
  [key: string]: unknown;
}
