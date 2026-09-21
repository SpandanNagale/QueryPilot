"""Execution-accuracy eval harness for QueryPilot, running on the Phase 1
cloud LLM gateway and Phase 2a per-session sandbox databases.

Runs each case in eval_set.json through the real agent graph (same code
path as production - no mocking). Each case gets its own fresh,
freshly-provisioned session sandbox (a throwaway copy of data/template.db,
same mechanism a real visitor's browser session gets), so a manipulation
case's actual DELETE/UPDATE/INSERT can never contaminate the shared
template or any other case's run - before Phase 2a existed, this harness
ran every case against the one shared template database directly.

Measures:
- execution accuracy: does the agent's SQL return the same result set as a
  hand-written reference query, not whether the SQL text matches it (all
  non-guardrail categories)
- guardrail correctness: does app/core/guardrail.py's heuristic block the
  inputs it should and let through the inputs it shouldn't -- including the
  "destructive SQL must reach the validator, not the guardrail" rule
- for a manipulation case (not blocked), that the preview's row-impact
  matches the reference query's expected count, AND that confirming the
  change actually commits (queried back from the case's own sandbox)
- p50/p95 latency, and a 95% bootstrap CI on each category's pass rate

Usage:
    python -m evals.run_evals
    python -m evals.run_evals --filter manipulation
    python -m evals.run_evals --out evals/eval_results.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import text

from app.agent.graph import build_graph
from app.core.db import get_engines
from app.core.sessions import get_session_db_path
from evals.stats import bootstrap_ci, percentile

EVAL_SET_PATH = Path(__file__).parent / "eval_set.json"
RESULTS_PATH = Path(__file__).parent / "eval_results.json"
SUMMARY_PATH = Path(__file__).parent / "SUMMARY.md"

# Categories folded into "execution accuracy" for the CI regression check;
# "guardrail" is reported and gated separately (a heuristic pass/fail, not
# an execution-accuracy comparison).
_EXECUTION_ACCURACY_CATEGORIES = {
    "simple_select", "join", "aggregation", "time_based", "followup", "manipulation", "ambiguous",
}

_GUARDRAIL_REGRESSION_FLOOR_PCT = 100.0
_EXECUTION_ACCURACY_REGRESSION_POINTS = 5.0


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


def _scalar(rows: list[dict]) -> int:
    if not rows:
        return 0
    return list(rows[0].values())[0]


def _new_case_config(case_id: str) -> tuple[dict, object]:
    """A fresh per-case session sandbox (a throwaway copy of the template,
    never reused across cases) plus its own MemorySaver checkpointer -- a
    process-lifetime, in-memory graph.compile(checkpointer=...) would leak
    state across every case sharing one graph object otherwise."""
    session_id = str(uuid.uuid4())
    db_path = get_session_db_path(session_id)
    thread_id = f"eval-{case_id}-{session_id[:8]}"
    config = {"configurable": {"thread_id": thread_id, "db_path": str(db_path)}}
    return config, db_path


def run_reference(sql: str, db_path) -> list[dict]:
    read_only_engine, _ = get_engines(str(db_path))
    with read_only_engine.connect() as conn:
        result = conn.execute(text(sql))
        return [dict(row._mapping) for row in result.fetchall()]


def run_guardrail_case(case: dict) -> dict:
    """Calls guardrail_node directly rather than running the full graph.
    guardrail runs first, before any LLM call -- "guardrail correctness"
    means "did this heuristic decide correctly," not "did the whole
    downstream pipeline also succeed." Live-verified during Phase 5
    development: routing an expect_blocked=False case through the full
    graph made it vulnerable to an unrelated downstream provider crash (the
    free-tier-model TypeError documented in Phases 3/4's notes) that has
    nothing to do with the guardrail's own decision, producing a false
    guardrail-correctness regression. Calling the node directly is also
    immediate (no network calls at all) and needs no session sandbox."""
    from app.agent.nodes.guardrail import guardrail_node

    result = {"id": case["id"], "category": "guardrail", "passed": False, "detail": ""}
    started = time.time()
    verdict = guardrail_node({"user_input": case["turns"][0]})
    result["latency"] = round(time.time() - started, 2)
    blocked = bool(verdict.get("guardrail_verdict", {}).get("blocked"))
    expected = bool(case["expect_blocked"])
    result["passed"] = blocked == expected
    result["detail"] = f"blocked={blocked}, expected={expected}"
    return result


def run_manipulation_case(graph, case: dict, config: dict, db_path) -> dict:
    result = {"id": case["id"], "category": "manipulation", "passed": False, "detail": ""}
    started = time.time()
    try:
        state = None
        for turn in case["turns"]:
            state = graph.invoke(
                {"messages": [], "query_history": (state or {}).get("query_history", []), "user_input": turn}, config
            )
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"agent raised: {exc}"
        result["latency"] = round(time.time() - started, 2)
        return result

    result["generated_sql"] = state.get("generated_sql")
    interrupted = bool(state.get("__interrupt__"))

    if case.get("expect_blocked"):
        blocked = bool(state.get("error")) and not interrupted
        result["passed"] = blocked
        result["detail"] = f"error={state.get('error')!r}, interrupted={interrupted}"
        result["latency"] = round(time.time() - started, 2)
        return result

    preview = state.get("preview") or {}
    preview_ok = interrupted and preview.get("row_count") is not None
    detail = f"interrupted={interrupted}, preview_row_count={preview.get('row_count')}"
    if not interrupted and not preview_ok:
        # Diagnostic-only: neither an error (validator/dry_run_preview
        # blocked it) nor a preview -- most likely ambiguity_check
        # classified this request as ambiguous this run (LLM classification
        # is non-deterministic case to case; a request that reaches
        # ask_clarifying_question sets final_response, not error).
        detail += f", error={state.get('error')!r}, is_ambiguous={state.get('ambiguity', {}).get('is_ambiguous')}, final_response={state.get('final_response')!r}"

    expected_count = None
    if preview_ok and case.get("reference_sql"):
        expected_count = 1 if preview.get("operation") == "INSERT" else _scalar(run_reference(case["reference_sql"], db_path))
        preview_ok = preview.get("row_count") == expected_count
        detail += f", expected_count={expected_count}"

    if not preview_ok:
        result["passed"] = False
        result["detail"] = detail
        result["latency"] = round(time.time() - started, 2)
        return result

    # Actually confirm: the whole point of a per-case sandbox is that this
    # write is safe to really commit -- it only ever touches this case's own
    # throwaway copy. Confirms the full path (transactional_executor,
    # audit_logger) runs cleanly, not just that a preview was offered.
    try:
        state = graph.invoke(Command(resume="yes"), config)
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"{detail}; confirm raised: {exc}"
        result["latency"] = round(time.time() - started, 2)
        return result

    committed = not state.get("error") and bool(state.get("final_response"))
    detail += f", committed={committed}, final_response={state.get('final_response')!r}"

    # Only a DELETE's reference_sql (an existence count keyed on the
    # deleted row's id) is expected to hit 0 after a successful commit. An
    # UPDATE's reference_sql (e.g. "COUNT(*) WHERE id=X") counts the SAME
    # row before and after -- the row still exists, only a field changed --
    # so re-running it post-commit would wrongly fail every correctly-
    # committed UPDATE case. UPDATE/INSERT cases rely on the `committed`
    # check above alone (no per-case "expected new value" data exists in
    # eval_set.json to check further).
    if committed and preview.get("operation") == "DELETE" and case.get("reference_sql") and expected_count is not None:
        post_state = _scalar(run_reference(case["reference_sql"], db_path))
        committed = post_state == 0
        detail += f", post_commit_reference_count={post_state}"

    result["passed"] = committed
    result["detail"] = detail
    result["latency"] = round(time.time() - started, 2)
    return result


def run_case(graph, case: dict) -> dict:
    if case["category"] == "guardrail":
        return run_guardrail_case(case)

    config, db_path = _new_case_config(case["id"])
    if case["category"] == "manipulation":
        return run_manipulation_case(graph, case, config, db_path)

    started = time.time()
    result = {"id": case["id"], "category": case["category"], "passed": False, "detail": ""}
    try:
        state = None
        for turn in case["turns"]:
            state = graph.invoke(
                {"messages": [], "query_history": (state or {}).get("query_history", []), "user_input": turn}, config
            )
    except Exception as exc:  # noqa: BLE001
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
        ref_rows = run_reference(case["reference_sql"], db_path)
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"reference_sql failed: {exc}"
        return result

    match = rows_match(exec_result["rows"], ref_rows)
    result["passed"] = match
    result["detail"] = f"agent_rows={len(exec_result['rows'])}, ref_rows={len(ref_rows)}, match={match}"
    return result


def _build_report(results: list[dict]) -> dict:
    by_category: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_category[r["category"]].append(r)

    scorecard = {}
    for cat, rows in sorted(by_category.items()):
        outcomes = [1.0 if r["passed"] else 0.0 for r in rows]
        passed = int(sum(outcomes))
        ci = bootstrap_ci(outcomes, statistic=statistics.mean)
        scorecard[cat] = {
            "passed": passed,
            "total": len(rows),
            "pct": round(100 * passed / len(rows), 1),
            "ci_95": ci.to_dict(),
        }

    exec_rows = [r for r in results if r["category"] in _EXECUTION_ACCURACY_CATEGORIES]
    guardrail_rows = [r for r in results if r["category"] == "guardrail"]

    exec_pass = sum(1 for r in exec_rows if r["passed"])
    exec_pct = round(100 * exec_pass / len(exec_rows), 1) if exec_rows else None
    guardrail_pass = sum(1 for r in guardrail_rows if r["passed"])
    guardrail_pct = round(100 * guardrail_pass / len(guardrail_rows), 1) if guardrail_rows else None

    latencies = [r["latency"] for r in results]
    latency_stats = {
        "mean": round(statistics.mean(latencies), 2),
        "p50": round(percentile(latencies, 50), 2),
        "p95": round(percentile(latencies, 95), 2),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "scorecard": scorecard,
        "execution_accuracy_pct": exec_pct,
        "guardrail_correctness_pct": guardrail_pct,
        "latency": latency_stats,
    }


def _check_regression(report: dict, baseline: dict | None) -> list[str]:
    """Returns a list of human-readable regression failures; empty means
    the run is clean. Compares against `baseline` (the previously committed
    evals/eval_results.json, read before this run overwrites it) -- absent
    on the very first run, which can't regress against nothing."""
    failures = []

    guardrail_pct = report.get("guardrail_correctness_pct")
    if guardrail_pct is not None and guardrail_pct < _GUARDRAIL_REGRESSION_FLOOR_PCT:
        failures.append(f"guardrail correctness {guardrail_pct}% is below the required {_GUARDRAIL_REGRESSION_FLOOR_PCT}%")

    if baseline is None:
        return failures

    baseline_exec_pct = baseline.get("execution_accuracy_pct")
    current_exec_pct = report.get("execution_accuracy_pct")
    if baseline_exec_pct is not None and current_exec_pct is not None:
        drop = baseline_exec_pct - current_exec_pct
        if drop > _EXECUTION_ACCURACY_REGRESSION_POINTS:
            failures.append(
                f"execution accuracy dropped {drop:.1f} points ({baseline_exec_pct}% -> {current_exec_pct}%), "
                f"more than the allowed {_EXECUTION_ACCURACY_REGRESSION_POINTS} points"
            )
    return failures


def _write_summary(report: dict, baseline: dict | None, regressions: list[str], path: Path) -> None:
    lines = [
        "# Eval summary",
        "",
        f"Generated: {report['generated_at']}",
        "",
        f"- Execution accuracy: **{report['execution_accuracy_pct']}%**"
        + (
            f" (baseline: {baseline['execution_accuracy_pct']}%)"
            if baseline and baseline.get("execution_accuracy_pct") is not None
            else ""
        ),
        f"- Guardrail correctness: **{report['guardrail_correctness_pct']}%**",
        f"- Latency: mean {report['latency']['mean']}s, p50 {report['latency']['p50']}s, p95 {report['latency']['p95']}s",
        "",
        "## Scorecard",
        "",
        "| Category | Passed | Total | % | 95% CI |",
        "|---|---|---|---|---|",
    ]
    for cat, s in sorted(report["scorecard"].items()):
        ci = s["ci_95"]
        lines.append(f"| {cat} | {s['passed']} | {s['total']} | {s['pct']}% | [{ci['lower']*100:.1f}%, {ci['upper']*100:.1f}%] |")

    lines += ["", "## CI gate", ""]
    if regressions:
        lines.append("**FAILED** — this run would fail the eval.yml CI gate:")
        lines += [f"- {r}" for r in regressions]
    else:
        lines.append("Passed — no regression against the last committed baseline.")

    # encoding="utf-8" explicitly: Windows' default text encoding is the
    # system locale codepage (often cp1252), which can't represent the em
    # dash above -- write_text() without it silently mangled "Passed —" to
    # "Passed ?" observed live on a Windows dev machine.
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", default=None, help="only run cases whose category contains this substring")
    parser.add_argument("--out", default=str(RESULTS_PATH))
    parser.add_argument("--summary-out", default=str(SUMMARY_PATH))
    args = parser.parse_args()

    out_path = Path(args.out)
    baseline = json.loads(out_path.read_text()) if out_path.exists() else None

    cases = json.loads(EVAL_SET_PATH.read_text())
    if args.filter:
        cases = [c for c in cases if args.filter in c["category"]]

    # A fresh checkpointer per harness run (not per case -- each case
    # already gets its own thread_id, so one MemorySaver is fine and avoids
    # rebuilding the graph object 68 times).
    graph = build_graph(checkpointer=MemorySaver())

    results = []
    for i, case in enumerate(cases, 1):
        label = case["turns"][-1][:60]
        print(f"[{i}/{len(cases)}] {case['id']} ({case['category']}): {label!r}", flush=True)
        r = run_case(graph, case)
        results.append(r)
        status = "PASS" if r["passed"] else "FAIL"
        print(f"    -> {status} ({r['latency']}s) {r['detail'][:150]}", flush=True)

    report = _build_report(results)

    print("\n" + "=" * 60)
    print("SCORECARD")
    print("=" * 60)
    for cat, s in sorted(report["scorecard"].items()):
        print(f"  {cat:20s} {s['passed']:3d}/{s['total']:<3d}  {s['pct']:5.1f}%")
    print("-" * 60)
    print(f"  Execution accuracy: {report['execution_accuracy_pct']}%")
    print(f"  Guardrail correctness: {report['guardrail_correctness_pct']}%")
    print(f"  Latency: mean {report['latency']['mean']}s p50 {report['latency']['p50']}s p95 {report['latency']['p95']}s")

    regressions = _check_regression(report, baseline)
    if regressions:
        print("\nREGRESSION DETECTED:")
        for r in regressions:
            print(f"  - {r}")

    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nFull results written to {out_path}")

    summary_path = Path(args.summary_out)
    _write_summary(report, baseline, regressions, summary_path)
    print(f"Summary written to {summary_path}")

    if regressions:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
