import { NodeState } from "./types";

export interface NodeStateStyleDef {
  label: string;
  dot: string; // background color class for the state dot
  ring: string; // border color class for the node card
  text: string; // text color class for the state label
}

export const NODE_STATE_STYLE: Record<NodeState, NodeStateStyleDef> = {
  pending: { label: "Pending", dot: "bg-ink-2", ring: "border-border-0", text: "text-ink-2" },
  running: { label: "Running", dot: "bg-amber animate-qp-pulse", ring: "border-amber", text: "text-amber" },
  succeeded: { label: "Succeeded", dot: "bg-teal", ring: "border-teal", text: "text-teal" },
  degraded: { label: "Degraded", dot: "bg-amber", ring: "border-amber", text: "text-amber" },
  failed: { label: "Failed", dot: "bg-coral", ring: "border-coral", text: "text-coral" },
  not_reached: { label: "Not reached", dot: "bg-ink-2", ring: "border-border-0", text: "text-ink-2" },
  skipped_cached: { label: "Cached", dot: "bg-teal", ring: "border-teal-dim", text: "text-teal" },
};

// Node names in the order they're first seen aren't always this list
// (the analytics and manipulation paths branch), but this labels the ones
// the pipeline can show -- mirrors app/agent/graph.py's node names.
export const NODE_LABELS: Record<string, string> = {
  guardrail: "Guardrail",
  intent_classifier: "Intent classifier",
  polite_redirect: "Off-topic redirect",
  schema_question_node: "Schema question",
  schema_retriever: "Schema retriever",
  sql_generator: "SQL generator",
  validator: "Validator",
  ambiguity_check: "Ambiguity check",
  ask_clarifying_question: "Clarifying question",
  error_response: "Error response",
  auto_limit_injector: "Auto-LIMIT",
  analytics_executor: "Query executor",
  self_corrector: "Self-correction",
  insight_summarizer: "Insight summary",
  chart_detector: "Chart detection",
  followup_suggester: "Follow-ups",
  dry_run_preview: "Preview",
  confirmation_gate: "Confirmation",
  transactional_executor: "Commit",
  audit_logger: "Audit log",
};

export function nodeLabel(node: string): string {
  return NODE_LABELS[node] ?? node;
}

export const ERROR_CODE_UI: Record<
  string,
  { title: string; action: string }
> = {
  PROVIDER_TIMEOUT: { title: "The AI provider took too long to respond.", action: "Try again." },
  PROVIDER_RATE_LIMIT: { title: "The AI provider is rate-limiting us right now.", action: "Wait a moment and try again." },
  PROVIDER_AUTH_FAILED: { title: "The AI provider rejected our credentials.", action: "This is a configuration issue on our end." },
  PROVIDER_UNAVAILABLE: { title: "The AI provider is currently unavailable.", action: "Try again shortly." },
  ALL_PROVIDERS_FAILED: { title: "Every configured AI provider failed to respond.", action: "Please try again shortly, or add your own API key below." },
  GUARDRAIL_BLOCKED: { title: "Your message was blocked by the input safety filter.", action: "Rephrase your request." },
  VALIDATION_BLOCKED: { title: "The generated query didn't pass safety validation.", action: "Try rephrasing your request." },
  ROW_CEILING_EXCEEDED: { title: "This change would affect too many rows and was blocked.", action: "Narrow your request with more specific conditions." },
  FK_BLOCKED: { title: "This change is blocked by related rows in other tables.", action: "Remove or reassign those rows first." },
  SCHEMA_VALIDATION_FAILED: { title: "The generated query referenced an unknown table or column.", action: "Try rephrasing your request." },
  SESSION_EXPIRED: { title: "No pending confirmation for this session.", action: "It may have expired -- ask again if you still want this." },
  SESSION_INVALID: { title: "Your session id isn't valid.", action: "Reset your sandbox." },
  RATE_LIMITED: { title: "Too many requests.", action: "Slow down and try again shortly." },
  BUDGET_EXCEEDED: { title: "The daily usage budget has been reached.", action: "Add your own API key below to keep going, or try again tomorrow." },
  INTERNAL: { title: "Something went wrong on our end.", action: "Try again." },
};
