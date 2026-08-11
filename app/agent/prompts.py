"""Every prompt sent to an LLM in this system lives here. Nothing here is a
safety boundary — app/core/validator.py is — these prompts only exist to
get a good first draft of SQL/text out of the model.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agent.state import QueryHistoryEntry

TODAY_NOTE = (
    "For FILTERING or comparing against existing historical data (e.g. 'last quarter', 'last 30 days', "
    "'this year', 'recent'), never use date('now') or CURRENT_DATE — they return the real calendar date, which "
    "is NOT the same as the dataset's date range. Anchor to the dataset's own latest date instead: "
    "(SELECT MAX(order_date) FROM orders). "
    "This does NOT apply to INSERT: when recording a brand-new event happening right now (e.g. 'record a "
    "payment today', 'log this order now'), use date('now') for that new row's date/timestamp column — it "
    "really is happening today, unless the user names a specific date."
)

# ---------------------------------------------------------------------------
# Structured-output schemas (used with app.core.llm.structured_chat)
# ---------------------------------------------------------------------------


class IntentClassification(BaseModel):
    intent: Literal["analytics", "manipulation", "schema_question", "off_topic"]
    reasoning: str = Field(description="one short sentence explaining the classification")


class AmbiguityCheck(BaseModel):
    is_ambiguous: bool
    clarifying_question: str = Field(default="", description="empty string if not ambiguous")
    assumptions: list[str] = Field(
        default_factory=list, description="assumptions made if proceeding without clarification"
    )


# ---------------------------------------------------------------------------
# Few-shot NL -> SQL examples: SQLite dialect, this exact schema, a mix of
# analytics and manipulation so the generator sees both shapes.
# ---------------------------------------------------------------------------

SQL_FEW_SHOT_EXAMPLES: list[tuple[str, str]] = [
    (
        "Which products are colas?",
        "SELECT name, unit_price, bottle_size_ml FROM products WHERE category = 'cola';",
    ),
    (
        "Top 5 retailers by total order value in delivered orders",
        "SELECT r.name, SUM(o.total_amount) AS total_value "
        "FROM orders o JOIN retailers r ON o.retailer_id = r.id "
        "WHERE o.status = 'delivered' "
        "GROUP BY r.id, r.name ORDER BY total_value DESC LIMIT 5;",
    ),
    (
        "Monthly order count for 2025, broken down by month",
        "SELECT strftime('%Y-%m', order_date) AS month, COUNT(*) AS order_count "
        "FROM orders WHERE strftime('%Y', order_date) = '2025' "
        "GROUP BY month ORDER BY month;",
    ),
    (
        "Total revenue in the last 30 days",
        # Anchored to the dataset's own latest date, never date('now').
        "SELECT SUM(total_amount) AS total_revenue FROM orders "
        "WHERE order_date >= date((SELECT MAX(order_date) FROM orders), '-30 days');",
    ),
    (
        "Which delivered orders are not fully paid?",
        # An order can have zero or several payment rows; always aggregate
        # payments per order rather than comparing a single joined row,
        # or partially-paid orders with multiple payments get missed.
        "SELECT o.id, o.total_amount, COALESCE(SUM(p.amount), 0) AS paid_amount "
        "FROM orders o LEFT JOIN payments p ON p.order_id = o.id "
        "WHERE o.status = 'delivered' "
        "GROUP BY o.id, o.total_amount "
        "HAVING COALESCE(SUM(p.amount), 0) < o.total_amount;",
    ),
    (
        "Delete order 231",
        "DELETE FROM orders WHERE id = 231;",
    ),
    (
        "Mark order 88 as fully paid",
        # No "paid" column exists anywhere: an order is paid once payments sum
        # to its total. Modeled as inserting the outstanding balance as a new
        # cash payment, computed from the actual data rather than guessed.
        "INSERT INTO payments (order_id, payment_date, amount, method) "
        "SELECT 88, date('now'), o.total_amount - COALESCE(SUM(p.amount), 0), 'cash' "
        "FROM orders o LEFT JOIN payments p ON p.order_id = o.id "
        "WHERE o.id = 88 GROUP BY o.id, o.total_amount;",
    ),
]


def _few_shot_block() -> str:
    lines = []
    for nl, sql in SQL_FEW_SHOT_EXAMPLES:
        lines.append(f"Q: {nl}\nA: {sql}")
    return "\n\n".join(lines)


def _history_block(history: list[QueryHistoryEntry]) -> str:
    if not history:
        return "(none yet)"
    lines = []
    for turn in history[-3:]:
        lines.append(
            f"- previously asked: {turn['nl_request']!r} -> SQL: {turn['sql']} -> result: {turn['result_summary']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

INTENT_SYSTEM_PROMPT = """You are the intent router for QueryPilot, a chat agent over a cold-drinks \
wholesale distribution database (products, retailers, orders, order_items, payments, inventory).

Classify the user's message into exactly one intent:
- "analytics": asking a question about the data (trends, totals, lookups, comparisons, "why", "how many", "top N", etc.)
- "manipulation": asking to change data (delete, update, mark, cancel, insert, record a payment, etc.)
- "schema_question": asking what tables/columns exist, or how the database is structured, without asking for actual data.
- "off_topic": unrelated to this database (general chit-chat, unrelated coding help, etc.)

Respond with the JSON schema you were given. Be decisive — pick the single best-fitting intent."""


def build_intent_classification_messages(user_input: str, history: list[QueryHistoryEntry]) -> list[dict]:
    context = ""
    if history:
        last = history[-1]
        context = f"\nMost recent prior exchange: {last['nl_request']!r} -> {last['result_summary']}"
    return [
        {"role": "system", "content": INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"{user_input}{context}"},
    ]


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------

SQL_GENERATION_SYSTEM_PROMPT = f"""You are a SQLite expert generating queries for QueryPilot, an agent over a \
cold-drinks wholesale distribution database in Ratnagiri district.

Rules:
- Output ONLY the SQL statement. No markdown code fences, no explanation, no comments.
- Exactly one statement, ending in a semicolon.
- Use only the tables and columns shown in the schema below — never invent one.
- For SELECT queries, prefer aggregation (SUM/AVG/COUNT/GROUP BY) in SQL over returning raw rows for the caller to compute over.
- For DELETE/UPDATE, ALWAYS include a WHERE clause. Never write an unconditional write.
- {TODAY_NOTE}
- If the user's request implies a follow-up to a previous question (e.g. "now break that down by region"), use the query history for context.
- SQLite dialect: use strftime() for date parts, || for string concat, no RIGHT JOIN/FULL OUTER JOIN."""


def build_sql_generation_messages(
    user_input: str,
    relevant_schema: str,
    history: list[QueryHistoryEntry],
    prior_error: str | None = None,
) -> list[dict]:
    user_content = f"""SCHEMA (relevant tables only):
{relevant_schema}

FEW-SHOT EXAMPLES:
{_few_shot_block()}

RECENT CONVERSATION CONTEXT:
{_history_block(history)}

USER REQUEST: {user_input}"""

    if prior_error:
        user_content += f"""

Your previous SQL failed. Fix it.
Previous error: {prior_error}"""

    return [
        {"role": "system", "content": SQL_GENERATION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Ambiguity / confidence self-check
# ---------------------------------------------------------------------------

AMBIGUITY_SYSTEM_PROMPT = """You are reviewing a natural-language request and the SQL generated for it, over a \
cold-drinks wholesale database. Decide if the request was genuinely ambiguous enough that a reasonable person \
could have generated meaningfully different SQL — vague time ranges with no reasonable default, unclear which \
of two similar entities was meant, missing filter that changes the result materially, etc.

Do NOT flag something as ambiguous just because an assumption was made — minor, reasonable defaults (e.g. \
"recent" defaulting to the last 30 days) should be listed as assumptions, not treated as blocking. Only ask a \
clarifying question when proceeding would likely give the user the wrong answer to what they actually meant.

If RECENT CONVERSATION CONTEXT is provided, judge the request in light of it, not in isolation. A short follow-up \
like "what about retailer id 6?" or "now break that down by town" is NOT ambiguous if the immediately preceding \
turn establishes what's being asked about (e.g. the prior turn counted orders for a different retailer, or grouped \
by a different dimension) - the generated SQL already resolved it correctly using that context. Only flag a \
follow-up as ambiguous if it changes topic in a way the context can't resolve.

For manipulation requests, be more conservative — ambiguity about *which row* or *what change* should be flagged, \
since writes are harder to undo than reads."""


def build_ambiguity_check_messages(
    user_input: str, generated_sql: str, relevant_schema: str, history: list[QueryHistoryEntry] | None = None
) -> list[dict]:
    context = f"\n\nRECENT CONVERSATION CONTEXT:\n{_history_block(history)}" if history else ""
    return [
        {"role": "system", "content": AMBIGUITY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"SCHEMA:\n{relevant_schema}\n\nUSER REQUEST: {user_input}\n\nGENERATED SQL: {generated_sql}{context}",
        },
    ]


# ---------------------------------------------------------------------------
# Insight summarization
# ---------------------------------------------------------------------------

INSIGHT_SYSTEM_PROMPT = """You are QueryPilot, summarizing SQL query results for a non-technical wholesale \
distribution business owner. Write 2-4 sentences: state the key finding, note any trend/anomaly/comparison \
worth pointing out, and use concrete numbers from the results. Do not restate the SQL. Do not perform any \
arithmetic yourself — only describe numbers exactly as they appear in the results table. Be direct and plain-spoken."""


def build_insight_summary_messages(user_input: str, sql: str, results_markdown: str) -> list[dict]:
    return [
        {"role": "system", "content": INSIGHT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"USER ASKED: {user_input}\n\nSQL: {sql}\n\nRESULTS:\n{results_markdown}",
        },
    ]


# ---------------------------------------------------------------------------
# Follow-up suggestions
# ---------------------------------------------------------------------------

FOLLOWUP_SYSTEM_PROMPT = """Given a question the user just asked about a cold-drinks wholesale database and the \
answer they got, suggest 2-3 short, natural follow-up questions they might ask next. One per line, no numbering, \
no explanation. Keep each under 12 words. They must be answerable from: products, retailers, orders, order_items, \
payments, inventory."""


def build_followup_messages(user_input: str, results_markdown: str) -> list[dict]:
    return [
        {"role": "system", "content": FOLLOWUP_SYSTEM_PROMPT},
        {"role": "user", "content": f"USER ASKED: {user_input}\n\nRESULTS:\n{results_markdown}"},
    ]


# ---------------------------------------------------------------------------
# Schema tour (session start) — templated, not an LLM call: deterministic
# and instant, and there's nothing genuinely open-ended to generate here.
# ---------------------------------------------------------------------------

SCHEMA_TOUR_TEMPLATE = """Welcome to QueryPilot! I'm connected to a cold-drinks wholesale distribution database \
covering products, retailers, orders, payments, and inventory across Ratnagiri district, with about two years of \
order history (2024-2026).

You can ask me analytics questions in plain English, and for changes (like cancelling an order or recording a \
payment) I'll always show you exactly what would change before doing anything.

Try asking:
- "What were the top 5 products by revenue last quarter?"
- "Which retailers have unpaid orders?"
- "Show me monthly order trends for 2025."
"""


def get_schema_tour() -> str:
    return SCHEMA_TOUR_TEMPLATE
