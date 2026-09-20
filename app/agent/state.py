"""The LangGraph agent state. Every node reads/writes a subset of this."""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

Intent = Literal["analytics", "manipulation", "clarification_needed", "schema_question", "off_topic"]


class QueryHistoryEntry(TypedDict):
    nl_request: str
    sql: str
    result_summary: str


class GuardrailVerdictDict(TypedDict):
    blocked: bool
    reason: NotRequired[str | None]


class AmbiguityInfo(TypedDict):
    is_ambiguous: bool
    clarifying_question: NotRequired[str]
    assumptions: NotRequired[list[str]]


class CascadeInfo(TypedDict):
    table: str
    column: str
    rows: list[dict]
    row_count: int


class PreviewInfo(TypedDict, total=False):
    operation: str                  # "DELETE" | "UPDATE" | "INSERT"
    table: str
    rows: list[dict]                # affected/changed rows (old/new for UPDATE)
    row_count: int
    insert_values: dict
    cascades: list[CascadeInfo]     # DELETE only: rows in other tables that would cascade-delete too


class ExecutionResult(TypedDict, total=False):
    columns: list[str]
    rows: list[dict]
    row_count: int
    truncated: bool


class ChartSpec(TypedDict, total=False):
    type: Literal["line", "bar", "pie"]
    x: str
    y: str
    title: str


class AgentState(TypedDict):
    messages: list[dict]                 # [{"role": "user"|"assistant", "content": str}, ...]
    query_history: list[QueryHistoryEntry]
    user_input: str
    guardrail_verdict: NotRequired[GuardrailVerdictDict]
    intent: NotRequired[Intent]
    relevant_schema: NotRequired[str]
    generated_sql: NotRequired[str]
    validation_result: NotRequired[dict]
    ambiguity: NotRequired[AmbiguityInfo]
    preview: NotRequired[PreviewInfo]
    awaiting_confirmation: NotRequired[bool]
    confirmed: NotRequired[bool]
    manipulation_started_at: NotRequired[float]
    execution_result: NotRequired[ExecutionResult]
    error: NotRequired[str]
    retry_count: NotRequired[int]
    final_response: NotRequired[str]
    chart_spec: NotRequired[ChartSpec | None]
    followups: NotRequired[list[str]]
