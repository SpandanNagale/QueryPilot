import { Page, Route } from "@playwright/test";

/** Builds one SSE frame in app/main.py::_sse()'s exact wire format. */
export function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function nodeState(node: string, state: string) {
  return frame("node_state", { node, state });
}

const DONE = frame("done", {
  run_id: "test-run",
  total_latency_ms: 1200,
  node_latencies_ms: {},
  llm_attempts: [{ alias: "chat", provider: "openrouter", model: "test-model", latency_ms: 400, success: true, prompt_tokens: 100, completion_tokens: 20, cost_usd: 0.0001 }],
  cache_hits: 0,
  cache_seconds_saved: 0,
  trace_url: null,
});

/** A full, successful analytics turn: guardrail -> ... -> followups -> done. */
export const ANALYTICS_SUCCESS_SSE =
  nodeState("guardrail", "running") +
  frame("guardrail", { guardrail_verdict: { blocked: false, reason: null } }) +
  nodeState("guardrail", "succeeded") +
  nodeState("intent_classifier", "running") +
  nodeState("intent_classifier", "succeeded") +
  nodeState("schema_retriever", "running") +
  nodeState("schema_retriever", "succeeded") +
  nodeState("sql_generator", "running") +
  frame("sql_generated", { generated_sql: "SELECT name FROM products WHERE category = 'cola'" }) +
  nodeState("sql_generator", "succeeded") +
  nodeState("validator", "running") +
  frame("validator_verdict", {
    validation_result: {
      passed: true,
      statement_type: "SELECT",
      reasons: [],
      checks: [
        { name: "sql_parses", passed: true },
        { name: "statement_type_allowed", passed: true },
        { name: "tables_exist", passed: true },
        { name: "columns_exist", passed: true },
      ],
    },
  }) +
  nodeState("validator", "succeeded") +
  nodeState("ambiguity_check", "running") +
  nodeState("ambiguity_check", "succeeded") +
  nodeState("auto_limit_injector", "running") +
  frame("validator_verdict", { generated_sql: "SELECT name FROM products WHERE category = 'cola' LIMIT 200", retry_count: 0, validator_verdict: { checks: [{ name: "auto_limit_injected", passed: true, injected: true }] } }) +
  nodeState("auto_limit_injector", "succeeded") +
  nodeState("analytics_executor", "running") +
  frame("result", { execution_result: { columns: ["name"], rows: [{ name: "Ratna Cola Classic" }, { name: "Konkan Cola" }], row_count: 2, truncated: false } }) +
  nodeState("analytics_executor", "succeeded") +
  nodeState("insight_summarizer", "running") +
  frame("token", { token: "There are two cola products." }) +
  nodeState("insight_summarizer", "succeeded") +
  nodeState("chart_detector", "running") +
  frame("chart", { chart_spec: null }) +
  nodeState("chart_detector", "succeeded") +
  nodeState("followup_suggester", "running") +
  frame("followups", { followups: ["What is the average price?"] }) +
  nodeState("followup_suggester", "succeeded") +
  DONE;

/** A guardrail-blocked turn: only the guardrail node runs. */
export const GUARDRAIL_BLOCKED_SSE =
  nodeState("guardrail", "running") +
  frame("guardrail", { guardrail_verdict: { blocked: true, reason: "Your message looks like a prompt-injection attempt." } }) +
  frame("error", { code: "GUARDRAIL_BLOCKED", message: "Guardrail blocked: Your message looks like a prompt-injection attempt.", node: "guardrail" }) +
  nodeState("guardrail", "failed") +
  nodeState("intent_classifier", "not_reached") +
  DONE;

const PREVIEW = {
  operation: "UPDATE",
  table: "orders",
  rows: [{ id: 42, old_status: "pending", new_status: "cancelled" }],
  row_count: 1,
};

/** A manipulation turn that reaches confirmation and pauses (the interrupt). */
export const MANIPULATION_AWAITING_CONFIRMATION_SSE =
  nodeState("guardrail", "running") +
  frame("guardrail", { guardrail_verdict: { blocked: false, reason: null } }) +
  nodeState("guardrail", "succeeded") +
  nodeState("intent_classifier", "running") +
  nodeState("intent_classifier", "succeeded") +
  nodeState("schema_retriever", "running") +
  nodeState("schema_retriever", "succeeded") +
  nodeState("sql_generator", "running") +
  frame("sql_generated", { generated_sql: "UPDATE orders SET status = 'cancelled' WHERE id = 42;" }) +
  nodeState("sql_generator", "succeeded") +
  nodeState("validator", "running") +
  frame("validator_verdict", { validation_result: { passed: true, statement_type: "UPDATE", reasons: [], checks: [] } }) +
  nodeState("validator", "succeeded") +
  nodeState("ambiguity_check", "running") +
  nodeState("ambiguity_check", "succeeded") +
  nodeState("dry_run_preview", "running") +
  frame("preview", { preview: PREVIEW, validator_verdict: { passed: true, statement_type: null, reasons: [], checks: [{ name: "row_impact_within_ceiling", passed: true }] }, awaiting_confirmation: true }) +
  nodeState("dry_run_preview", "succeeded") +
  nodeState("confirmation_gate", "running") +
  frame("awaiting_confirmation", {
    type: "confirmation_request",
    preview: PREVIEW,
    generated_sql: "UPDATE orders SET status = 'cancelled' WHERE id = 42;",
    message: "Please confirm to proceed (yes/no). Anything other than yes cancels.",
    confirmation_timeout_seconds: 60,
  }) +
  DONE;

/** /confirm resuming that same turn, committed successfully. */
export const CONFIRM_COMMITTED_SSE =
  nodeState("confirmation_gate", "running") +
  frame("message", { awaiting_confirmation: false, confirmed: true }) +
  nodeState("confirmation_gate", "succeeded") +
  nodeState("transactional_executor", "running") +
  frame("result", { execution_result: { columns: [], rows: [], row_count: 1, truncated: false }, final_response: "Done - 1 row(s) affected in orders." }) +
  nodeState("transactional_executor", "succeeded") +
  nodeState("audit_logger", "running") +
  nodeState("audit_logger", "succeeded") +
  DONE;

/** /confirm resuming that same turn, cancelled by the user. */
export const CONFIRM_CANCELLED_SSE =
  nodeState("confirmation_gate", "running") +
  frame("message", { awaiting_confirmation: false, confirmed: false, final_response: "Cancelled - no changes were made." }) +
  nodeState("confirmation_gate", "succeeded") +
  DONE;

export async function mockSse(page: Page, urlSuffix: string, body: string) {
  await page.route(`**${urlSuffix}`, async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body,
    });
  });
}
