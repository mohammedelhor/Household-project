"""Tests for ml/power.py and ml/compare.py, with numbers worked out by hand."""
import math
from datetime import datetime, timezone

import pytest

from data_engineering.scopes import Scope
from ml.baselines import FixedDays, MedianInterval
from ml.compare import cluster_bootstrap_ci, compare, paired_differences
from ml.power import design_effect, power_for, required_clusters, required_samples
from ml.target import Sample

T = datetime(2026, 9, 1, tzinfo=timezone.utc)


def sample(scope_id, history, target):
    return Sample(Scope("household", scope_id), 1, T, T.date(), tuple(history), target)


# ------------------------------------ power ---------------------------------------

def test_required_samples_matches_the_hand_formula():
    # n = 7.85 * (sigma/delta)^2 ; sigma/delta = 2 -> 31.4 -> 32
    assert required_samples(2.0, 1.0) == 32
    assert required_samples(3.0, 1.0) == 71
    assert required_samples(4.0, 1.0) == 126


def test_a_fixed_64_samples_is_about_a_coin_flip_when_sigma_is_four_deltas():
    assert power_for(64, 4.0, 1.0) == pytest.approx(0.52, abs=0.02)


def test_power_reaches_80_percent_at_the_required_size():
    n = required_samples(3.0, 1.0)
    assert power_for(n, 3.0, 1.0) >= 0.80
    assert power_for(n - 5, 3.0, 1.0) < 0.80


def test_design_effect_and_clusters():
    assert design_effect(1, 0.5) == 1.0
    assert design_effect(30, 0.2) == pytest.approx(6.8)
    # 70.6 independent samples * 6.8 = 480.4 -> 481 samples = 17 clusters of 30
    assert required_samples(3.0, 1.0, cluster_size=30, icc=0.2) == 481
    assert required_clusters(3.0, 1.0, cluster_size=30, icc=0.2) == 17


@pytest.mark.parametrize("args", [(0, 1), (1, 0), (-1, 1)])
def test_power_rejects_nonsense(args):
    with pytest.raises(ValueError):
        required_samples(*args)


def test_design_effect_validates_inputs():
    with pytest.raises(ValueError):
        design_effect(0, 0.1)
    with pytest.raises(ValueError):
        design_effect(5, 1.5)


# ------------------------------------ compare -------------------------------------

def scopes_where_b_is_better(n_scopes=8, per_scope=5):
    """A (always 14) is off by 7 on a 7-day product; B (median) is exact."""
    return [sample(s, (7, 7), 7) for s in range(1, n_scopes + 1) for _ in range(per_scope)]


def test_paired_differences_are_per_sample():
    rows = paired_differences([sample(1, (7, 7), 7)], FixedDays(14), MedianInterval())
    assert rows == [("household:1", 7)]            # |14-7| - |7-7|


def test_a_clear_improvement_is_adopted():
    result = compare(scopes_where_b_is_better(), FixedDays(14), MedianInterval(), delta=1.0)
    assert result.decision == "adopt_b"
    assert result.mean_difference == 7 and result.n_scopes == 8


def test_a_worse_model_is_not_adopted():
    result = compare(scopes_where_b_is_better(), MedianInterval(), FixedDays(14), delta=1.0)
    assert result.decision == "keep_a"


def test_a_noisy_difference_is_inconclusive():
    samples = [sample(s, (7, 7), 7 + (3 if s % 2 else -3)) for s in range(1, 7) for _ in range(3)]
    result = compare(samples, MedianInterval(), FixedDays(8), delta=0.5)
    assert result.decision == "inconclusive"


def test_a_single_scope_is_always_inconclusive():
    samples = [sample(1, (7, 7), 7)] * 20
    result = compare(samples, FixedDays(14), MedianInterval(), delta=1.0)
    assert result.decision == "inconclusive" and "single scope" in result.reason


def test_no_common_samples_is_inconclusive():
    result = compare([sample(1, (), 7)], FixedDays(14), MedianInterval(), delta=1.0)
    assert result.decision == "inconclusive"


def test_delta_must_be_positive():
    with pytest.raises(ValueError):
        compare([], FixedDays(14), MedianInterval(), delta=0)


def test_the_bootstrap_resamples_scopes_not_rows():
    """One scope with 100 rows and one with 1: a row bootstrap would hardly move,
    a scope bootstrap must show the interval spanning both scopes' values."""
    rows = [("a", 5.0)] * 100 + [("b", -5.0)]
    low, high = cluster_bootstrap_ci(rows, resamples=500, seed=1)
    assert low < -4 and high > 4.9


def test_the_bootstrap_is_reproducible():
    rows = [("a", 1.0), ("b", 2.0), ("c", 4.0)]
    assert cluster_bootstrap_ci(rows, seed=3) == cluster_bootstrap_ci(rows, seed=3)
