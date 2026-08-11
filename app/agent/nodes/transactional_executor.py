"""Executes the confirmed write inside a real transaction: BEGIN -> execute
-> COMMIT, ROLLBACK on any error. Re-validates and re-checks the row-impact
ceiling one last time right before writing, since the underlying data (or
even the row count matched) could have changed between the preview shown
and the user's confirmation moments later.
"""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from app.agent.state import AgentState
from app.core import executor as sql_executor
from app.core.validator import check_row_impact, validate_sql


def transactional_executor_node(state: AgentState) -> dict:
    sql = state["generated_sql"]

    validation = validate_sql(sql, intent="manipulation")
    if not validation.passed:
        reason = "; ".join(validation.reasons)
        return {"error": reason, "final_response": f"I couldn't complete this: {reason}"}

    impact = check_row_impact(sql)
    if not impact.passed:
        reason = "; ".join(impact.reasons)
        return {"error": reason, "final_response": f"I couldn't complete this: {reason}"}

    try:
        with sql_executor.transaction() as conn:
            result = sql_executor.execute_write(sql, conn)
    except IntegrityError as exc:
        # Defense in depth: check_row_impact() above already checks the live
        # FK graph, so this should be unreachable in practice - but if data
        # changed in the instant between that check and this write, surface
        # something readable instead of a raw driver exception.
        reason = "a foreign key constraint failed - other rows still reference the ones being changed"
        return {"error": reason, "final_response": f"The change failed and was rolled back: {reason}."}
    except Exception as exc:  # noqa: BLE001 - must ROLLBACK and report, whatever the cause
        return {"error": str(exc), "final_response": f"The change failed and was rolled back: {exc}"}

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
