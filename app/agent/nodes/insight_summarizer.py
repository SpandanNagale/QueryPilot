from __future__ import annotations

from langgraph.config import get_stream_writer

from app.agent.nodes.result_formatting import rows_to_markdown
from app.agent.prompts import build_insight_summary_messages
from app.agent.state import AgentState
from app.config import settings
from app.core.llm import stream_chat


def insight_summarizer_node(state: AgentState) -> dict:
    exec_result = state["execution_result"]
    markdown = rows_to_markdown(exec_result["columns"], exec_result["rows"])

    writer = get_stream_writer()
    chunks: list[str] = []
    for token in stream_chat(
        model=settings.chat_model,
        messages=build_insight_summary_messages(state["user_input"], state["generated_sql"], markdown),
        temperature=0.2,
    ):
        chunks.append(token)
        writer({"token": token})
    content = "".join(chunks)

    result_summary = f"{exec_result['row_count']} row(s): {content}"
    history_entry = {
        "nl_request": state["user_input"],
        "sql": state["generated_sql"],
        "result_summary": result_summary,
    }

    return {
        "final_response": content,
        "query_history": state.get("query_history", []) + [history_entry],
    }
