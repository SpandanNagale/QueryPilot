from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig
from sqlalchemy.exc import IntegrityError

from app.agent.state import AgentState
from app.core import executor as sql_executor
from app.core.db import get_engines
from app.core.validator import check_row_impact, validate_sql

logger = logging.getLogger(__name__)


def transactional_executor_node(state: AgentState, config: RunnableConfig) -> dict:
    sql = state["generated_sql"]
    db_path = config["configurable"]["db_path"]
    read_only_engine, read_write_engine = get_engines(db_path)

    validation = validate_sql(sql, intent="manipulation", engine=read_only_engine)
    if not validation.passed:
        reason = "; ".join(validation.reasons)
        return {"error": reason, "final_response": f"I couldn't complete this: {reason}"}

    impact = check_row_impact(sql, engine=read_only_engine)
    if not impact.passed:
        reason = "; ".join(impact.reasons)
        return {"error": reason, "final_response": f"I couldn't complete this: {reason}"}

    try:
        with sql_executor.transaction(engine=read_write_engine) as conn:
            result = sql_executor.execute_write(sql, conn)
    except IntegrityError:
        reason = "a foreign key constraint failed - other rows still reference the ones being changed"
        return {"error": reason, "final_response": f"The change failed and was rolled back: {reason}."}
    except Exception as exc:  # noqa: BLE001 - must ROLLBACK and report, whatever the cause
        logger.warning("transactional_executor DB error (sql=%r): %s", sql, exc)
        reason = "an unexpected database error occurred"
        return {
            "error": f"execution failed: {exc}",
            "client_error": reason,
            "final_response": f"The change failed and was rolled back: {reason}.",
        }

    return {
        "execution_result": {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": False,
        },
        "final_response": f"Done - {result.row_count} row(s) affected in {state['preview']['table']}.",
        "error": None,
    }
