import { SseFrame } from "./sse";
import {
  CacheHitPayload,
  ChartSpec,
  ErrorEventPayload,
  ExecutionResult,
  GuardrailVerdict,
  LlmAttemptPayload,
  NodeState,
  PreviewInfo,
  RunSummary,
  ValidationResult,
} from "./types";

export interface NodeTraceEntry {
  node: string;
  state: NodeState;
  startedAtMs?: number;
  finishedAtMs?: number;
  attempts: LlmAttemptPayload[];
  cacheHits: CacheHitPayload[];
  guardrailVerdict?: GuardrailVerdict;
  validatorChecks?: { name: string; passed: boolean; injected?: boolean }[];
}

export type ChatStatus = "idle" | "running" | "awaiting_confirmation" | "done" | "error" | "cancelled";

export interface ChatState {
  status: ChatStatus;
  userMessage: string | null;
  nodeOrder: string[];
  nodes: Record<string, NodeTraceEntry>;
  currentNode: string | null;
  generatedSql: string | null;
  validationResult: ValidationResult | null;
  preview: PreviewInfo | null;
  confirmationMessage: string | null;
  confirmationTimeoutSeconds: number | null;
  executionResult: ExecutionResult | null;
  chartSpec: ChartSpec | null;
  insightText: string;
  followups: string[];
  finalResponse: string | null;
  errors: ErrorEventPayload[];
  summary: RunSummary | null;
  lastHeartbeatAtMs: number | null;
}

export function initialChatState(): ChatState {
  return {
    status: "idle",
    userMessage: null,
    nodeOrder: [],
    nodes: {},
    currentNode: null,
    generatedSql: null,
    validationResult: null,
    preview: null,
    confirmationMessage: null,
    confirmationTimeoutSeconds: null,
    executionResult: null,
    chartSpec: null,
    insightText: "",
    followups: [],
    finalResponse: null,
    errors: [],
    summary: null,
    lastHeartbeatAtMs: null,
  };
}

export type ChatAction =
  | { type: "user_message"; message: string }
  | { type: "confirm_resume" }
  | { type: "sse"; frame: SseFrame }
  | { type: "stream_error"; message: string }
  | { type: "http_error"; code: string; message: string };

function ensureNode(state: ChatState, node: string): NodeTraceEntry {
  return state.nodes[node] ?? { node, state: "pending", attempts: [], cacheHits: [] };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "user_message": {
      // A brand-new turn: reset everything except the running node trace's
      // shell (a fresh turn gets its own trace from node_state events).
      const fresh = initialChatState();
      fresh.status = "running";
      fresh.userMessage = action.message;
      return fresh;
    }

    case "confirm_resume": {
      // Resuming the SAME turn after /confirm -- keep the existing trace
      // (guardrail..dry_run_preview already ran) and append to it, don't reset.
      return { ...state, status: "running", confirmationMessage: null };
    }

    case "sse":
      return applySseFrame(state, action.frame);

    case "stream_error":
      return {
        ...state,
        status: "error",
        errors: [...state.errors, { code: "INTERNAL", message: action.message, node: "client" }],
      };

    case "http_error":
      return {
        ...state,
        status: "error",
        errors: [...state.errors, { code: action.code as ErrorEventPayload["code"], message: action.message, node: "server" }],
      };

    default:
      return state;
  }
}

function applySseFrame(state: ChatState, frame: SseFrame): ChatState {
  const data = frame.data as Record<string, unknown>;

  switch (frame.eventType) {
    case "node_state": {
      const node = data.node as string;
      const nodeState = data.state as NodeState;
      const entry = { ...ensureNode(state, node) };
      entry.state = nodeState;
      if (nodeState === "running") entry.startedAtMs = Date.now();
      else entry.finishedAtMs = Date.now();

      const nodeOrder = state.nodeOrder.includes(node) ? state.nodeOrder : [...state.nodeOrder, node];
      return {
        ...state,
        nodeOrder,
        nodes: { ...state.nodes, [node]: entry },
        currentNode: nodeState === "running" ? node : state.currentNode,
      };
    }

    case "llm_attempt": {
      const node = state.currentNode;
      if (!node) return state;
      const entry = { ...ensureNode(state, node) };
      entry.attempts = [...entry.attempts, data as unknown as LlmAttemptPayload];
      return { ...state, nodes: { ...state.nodes, [node]: entry } };
    }

    case "cache_hit": {
      const payload = data as unknown as CacheHitPayload;
      const node = payload.node || state.currentNode;
      if (!node) return state;
      const entry = { ...ensureNode(state, node) };
      entry.cacheHits = [...entry.cacheHits, payload];
      return { ...state, nodes: { ...state.nodes, [node]: entry } };
    }

    case "guardrail": {
      const node = state.currentNode ?? "guardrail";
      const entry = { ...ensureNode(state, node) };
      entry.guardrailVerdict = data.guardrail_verdict as GuardrailVerdict;
      return { ...state, nodes: { ...state.nodes, [node]: entry } };
    }

    case "validator_verdict": {
      let next = state;
      if (data.validation_result) {
        next = { ...next, validationResult: data.validation_result as ValidationResult };
        const node = state.currentNode ?? "validator";
        const entry = { ...ensureNode(next, node) };
        entry.validatorChecks = (data.validation_result as ValidationResult).checks;
        next = { ...next, nodes: { ...next.nodes, [node]: entry } };
      }
      if (data.generated_sql) next = { ...next, generatedSql: data.generated_sql as string };
      if (data.validator_verdict) {
        const node = state.currentNode ?? "auto_limit_injector";
        const entry = { ...ensureNode(next, node) };
        const verdict = data.validator_verdict as { checks: NodeTraceEntry["validatorChecks"] };
        entry.validatorChecks = [...(entry.validatorChecks ?? []), ...(verdict.checks ?? [])];
        next = { ...next, nodes: { ...next.nodes, [node]: entry } };
      }
      return next;
    }

    case "sql_generated":
      return { ...state, generatedSql: data.generated_sql as string };

    case "preview": {
      let next = state;
      if (data.preview) next = { ...next, preview: data.preview as PreviewInfo };
      if (data.validator_verdict) {
        const node = "dry_run_preview";
        const entry = { ...ensureNode(next, node) };
        entry.validatorChecks = (data.validator_verdict as ValidationResult).checks;
        next = { ...next, nodes: { ...next.nodes, [node]: entry } };
      }
      return next;
    }

    case "awaiting_confirmation":
      return {
        ...state,
        status: "awaiting_confirmation",
        preview: (data.preview as PreviewInfo) ?? state.preview,
        generatedSql: (data.generated_sql as string) ?? state.generatedSql,
        confirmationMessage: data.message as string,
        confirmationTimeoutSeconds: (data.confirmation_timeout_seconds as number) ?? state.confirmationTimeoutSeconds,
      };

    case "message": {
      let next = state;
      if (data.final_response) next = { ...next, finalResponse: data.final_response as string };
      if (data.confirmed === false) next = { ...next, status: "cancelled" };
      return next;
    }

    case "result": {
      let next = state;
      if (data.execution_result) next = { ...next, executionResult: data.execution_result as ExecutionResult };
      if (data.final_response) next = { ...next, finalResponse: data.final_response as string };
      return next;
    }

    case "chart":
      return { ...state, chartSpec: (data.chart_spec as ChartSpec) ?? null };

    case "followups":
      return { ...state, followups: (data.followups as string[]) ?? [] };

    case "token":
      return { ...state, insightText: state.insightText + ((data.token as string) ?? "") };

    case "error":
      return { ...state, errors: [...state.errors, data as unknown as ErrorEventPayload] };

    case "heartbeat":
      return { ...state, lastHeartbeatAtMs: Date.now() };

    case "done": {
      const summary = data as unknown as RunSummary;
      const status: ChatStatus = state.status === "awaiting_confirmation" ? "awaiting_confirmation" : "done";
      return { ...state, summary, status };
    }

    default:
      return state;
  }
}
