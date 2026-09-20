"""Execution-accuracy eval harness for QueryPilot.

Runs each case in eval_set.json through the real agent graph (same code
path as production - no mocking), then measures *execution accuracy*: does
the agent's SQL return the same result set as a hand-written reference
query, not whether the SQL text matches it. Also checks guardrail
correctness (ambiguous cases must trigger clarification; manipulation
cases must trigger a preview, or be blocked when they should be) and
records per-case latency.

Usage:
    python -m evals.run_evals
    python -m evals.run_evals --filter manipulation
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

from app.agent.graph import build_graph
from app.config import settings
from app.core.db import read_only_engine

EVAL_SET_PATH = Path(__file__).parent / "eval_set.json"
RESULTS_PATH = Path(__file__).parent / "eval_results.json"


def _row_values(row: dict) -> frozenset:
    """A row as a set of its (float-rounded) scalar values, ignoring column
    names/order entirely."""
    return frozenset(round(v, 2) if isinstance(v, float) else v for v in row.values())


def rows_match(agent_rows: list[dict], ref_rows: list[dict]) -> bool:
    """Execution-accuracy comparison that tolerates the agent selecting
    extra, reasonably-helpful columns beyond exactly what the reference
    query asked for (e.g. the agent adds unit_price when asked "which
    products are colas" - still correct, just more informative). A
    reference row counts as matched if some distinct agent row's value set
    is a SUPERSET of it; row counts must still match exactly so missing or
    extra rows are still caught. Greedy bipartite matching, largest
    reference rows first - fine at eval-scale row counts."""
    if len(agent_rows) != len(ref_rows):
        return False
    agent_sets = [_row_values(r) for r in agent_rows]
    ref_sets = sorted((_row_values(r) for r in ref_rows), key=len, reverse=True)
    used = [False] * len(agent_sets)
    for ref_set in ref_sets:
        matched_index = next(
            (i for i, a in enumerate(agent_sets) if not used[i] and ref_set <= a), None
        )
        if matched_index is None:
            return False
        used[matched_index] = True
    return True


def run_reference(sql: str) -> list[dict]:
    with read_only_engine.connect() as conn:
        result = conn.execute(text(sql))
        return [dict(row._mapping) for row in result.fetchall()]


def _scalar(rows: list[dict]) -> int:
    if not rows:
        return 0
    return list(rows[0].values())[0]


def run_case(graph, case: dict) -> dict:
    thread_id = f"eval-{case['id']}"
    # validator_node and other Part-A nodes subscript config["configurable"]["db_path"]
    # directly - evals run against the template database, same as before session
    # sandboxing existed.
    config = {"configurable": {"thread_id": thread_id, "db_path": str(settings.template_db_path)}}
    started = time.time()
    result = {"id": case["id"], "category": case["category"], "passed": False, "detail": ""}

    try:
        state = None
        for turn in case["turns"]:
            state = graph.invoke(
                {"messages": [], "query_history": (state or {}).get("query_history", []), "user_input": turn},
                config,
            )
    except Exception as exc:  # noqa: BLE001 - a raised exception is a failed case, not a harness crash
        result["detail"] = f"agent raised: {exc}"
        result["latency"] = round(time.time() - started, 2)
        return result

    result["latency"] = round(time.time() - started, 2)
    result["generated_sql"] = state.get("generated_sql")

    if case["category"] == "ambiguous":
        is_ambiguous = state.get("ambiguity", {}).get("is_ambiguous", False)
        result["passed"] = bool(is_ambiguous)
        result["detail"] = f"is_ambiguous={is_ambiguous}"
        return result

    if case["category"] == "manipulation":
        interrupted = bool(state.get("__interrupt__"))

        if case.get("expect_blocked"):
            blocked = bool(state.get("error")) and not interrupted
            result["passed"] = blocked
            result["detail"] = f"error={state.get('error')!r}, interrupted={interrupted}"
            return result

        preview = state.get("preview") or {}
        passed = interrupted and preview.get("row_count") is not None
        detail = f"interrupted={interrupted}, preview_row_count={preview.get('row_count')}"

        if passed and case.get("reference_sql"):
            expected_count = 1 if preview.get("operation") == "INSERT" else _scalar(run_reference(case["reference_sql"]))
            passed = preview.get("row_count") == expected_count
            detail += f", expected_count={expected_count}"

        result["passed"] = passed
        result["detail"] = detail
        return result

    # analytics categories: simple_select, join, aggregation, time_based, followup
    exec_result = state.get("execution_result")
    if not exec_result:
        result["detail"] = f"no execution_result; error={state.get('error')!r}"
        return result

    if not case.get("reference_sql"):
        result["passed"] = True
        result["detail"] = "no reference_sql provided; execution success only"
        return result

    try:
        ref_rows = run_reference(case["reference_sql"])
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"reference_sql failed: {exc}"
        return result

    match = rows_match(exec_result["rows"], ref_rows)
    result["passed"] = match
    result["detail"] = f"agent_rows={len(exec_result['rows'])}, ref_rows={len(ref_rows)}, match={match}"
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", default=None, help="only run cases whose category contains this substring")
    parser.add_argument("--out", default=str(RESULTS_PATH))
    args = parser.parse_args()

    cases = json.loads(EVAL_SET_PATH.read_text())
    if args.filter:
        cases = [c for c in cases if args.filter in c["category"]]

    graph = build_graph()

    results = []
    for i, case in enumerate(cases, 1):
        label = case["turns"][-1][:60]
        print(f"[{i}/{len(cases)}] {case['id']} ({case['category']}): {label!r}", flush=True)
        r = run_case(graph, case)
        results.append(r)
        status = "PASS" if r["passed"] else "FAIL"
        print(f"    -> {status} ({r['latency']}s) {r['detail'][:150]}", flush=True)

    by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in results:
        by_category[r["category"]][1] += 1
        if r["passed"]:
            by_category[r["category"]][0] += 1

    print("\n" + "=" * 60)
    print("SCORECARD")
    print("=" * 60)
    total_pass, total_n = 0, 0
    for cat, (p, n) in sorted(by_category.items()):
        pct = 100 * p / n if n else 0
        print(f"  {cat:20s} {p:3d}/{n:<3d}  {pct:5.1f}%")
        total_pass += p
        total_n += n
    overall_pct = 100 * total_pass / total_n if total_n else 0
    print("-" * 60)
    print(f"  {'OVERALL':20s} {total_pass:3d}/{total_n:<3d}  {overall_pct:5.1f}%")

    avg_latency = sum(r["latency"] for r in results) / len(results) if results else 0
    print(f"\nAverage latency per case: {avg_latency:.1f}s")

    scorecard = {
        cat: {"passed": p, "total": n, "pct": round(100 * p / n, 1) if n else 0} for cat, (p, n) in by_category.items()
    }
    Path(args.out).write_text(
        json.dumps({"results": results, "scorecard": scorecard, "overall_pct": round(overall_pct, 1)}, indent=2, default=str)
    )
    print(f"\nFull results written to {args.out}")


if __name__ == "__main__":
    main()
