# tests/test_eval_stats.py
"""Phase 5: evals/stats.py's bootstrap_ci and percentile -- the two
statistics primitives evals/run_evals.py's CI regression gate depends on."""
from __future__ import annotations

import statistics

import pytest

from evals.stats import bootstrap_ci, percentile


def test_bootstrap_ci_point_estimate_matches_plain_mean():
    samples = [1.0, 1.0, 0.0, 1.0, 0.0]
    ci = bootstrap_ci(samples)
    assert ci.point_estimate == statistics.mean(samples)


def test_bootstrap_ci_is_deterministic_across_runs():
    samples = [1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0]
    a = bootstrap_ci(samples, seed=0)
    b = bootstrap_ci(samples, seed=0)
    assert a.lower == b.lower
    assert a.upper == b.upper


def test_bootstrap_ci_all_passes_has_zero_width_interval():
    ci = bootstrap_ci([1.0, 1.0, 1.0, 1.0])
    assert ci.lower == ci.upper == 1.0


def test_bootstrap_ci_single_sample_is_a_point():
    ci = bootstrap_ci([1.0])
    assert ci.point_estimate == ci.lower == ci.upper == 1.0


def test_bootstrap_ci_rejects_empty_samples():
    with pytest.raises(ValueError):
        bootstrap_ci([])


def test_bootstrap_ci_interval_contains_the_point_estimate():
    samples = [1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0]
    ci = bootstrap_ci(samples)
    assert ci.lower <= ci.point_estimate <= ci.upper


def test_percentile_p50_of_sorted_values():
    assert percentile([1, 2, 3, 4, 5], 50) == 3


def test_percentile_p95_favors_the_high_end():
    values = list(range(1, 101))  # 1..100
    assert percentile(values, 95) >= 95


def test_percentile_rejects_empty_values():
    with pytest.raises(ValueError):
        percentile([], 50)
