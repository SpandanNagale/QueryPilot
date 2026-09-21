"""Statistics primitives for evals/run_evals.py: bootstrap confidence
intervals and latency percentiles.

Ported from the sibling Crewai-Research-pipeline repo's eval/stats.py
(itself a port of github.com/SpandanNagale/Assay's lib/stats/bootstrap.ts),
trimmed to what this harness actually uses -- bootstrap_ci and percentile.
That repo's paired_bootstrap_ci/spearman_correlation/weighted_cohens_kappa
exist there for judge-vs-human calibration, which QueryPilot's harness has
no equivalent of (it compares agent SQL results against a hand-written
reference query, not a judge's rating), so they're not ported. Stdlib only
(random/statistics), no new dependency.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass
class ConfidenceInterval:
    point_estimate: float
    lower: float
    upper: float
    confidence: float

    def to_dict(self) -> dict:
        return {
            "point_estimate": round(self.point_estimate, 4),
            "lower": round(self.lower, 4),
            "upper": round(self.upper, 4),
            "confidence": self.confidence,
        }


def bootstrap_ci(
    samples: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = statistics.mean,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> ConfidenceInterval:
    """Percentile bootstrap confidence interval for `statistic` over
    `samples`. Deterministic across runs (fixed seed) so two harness runs on
    identical data produce an identical interval -- important for CI
    regression checks."""
    if not samples:
        raise ValueError("bootstrap_ci requires at least one sample")

    point_estimate = statistic(samples)
    if len(samples) == 1:
        return ConfidenceInterval(point_estimate, point_estimate, point_estimate, confidence)

    rng = random.Random(seed)
    n = len(samples)
    resampled_stats = []
    for _ in range(n_resamples):
        resample = [samples[rng.randrange(n)] for _ in range(n)]
        resampled_stats.append(statistic(resample))
    resampled_stats.sort()

    alpha = (1 - confidence) / 2
    lower = resampled_stats[int(alpha * n_resamples)]
    upper = resampled_stats[min(n_resamples - 1, int((1 - alpha) * n_resamples))]
    return ConfidenceInterval(point_estimate, lower, upper, confidence)


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (p in [0, 100])."""
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((p / 100) * (len(ordered) - 1))))
    return ordered[index]
