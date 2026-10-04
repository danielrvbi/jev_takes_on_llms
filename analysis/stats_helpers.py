"""Small statistical helpers; choose comparable, independent samples yourself."""

import numpy as np
from scipy import stats
from statsmodels.stats.power import TTestIndPower, TTestPower


def _sample(values):
    values = np.asarray(values, dtype=float)
    if values.ndim != 1 or values.size < 2 or not np.isfinite(values).all():
        raise ValueError("Use a finite 1-D sample with at least two observations")
    return values


def welch(a, b):
    """Two-sided independent mean comparison; returns a SciPy TtestResult."""
    return stats.ttest_ind(_sample(a), _sample(b), equal_var=False)


def levene(a, b):
    """Two-sided spread comparison, median-centered; returns a SciPy result."""
    return stats.levene(_sample(a), _sample(b), center="median")


def mean_power(effect_size, n=40, paired=False, alpha=0.05):
    """Assumed mean-effect sensitivity, not observed power or exact Welch power.

    Independent: n per group; effect = mean difference / common population SD.
    Paired: n pairs; effect = mean paired difference / SD of paired differences.
    """
    if not np.isfinite(effect_size) or effect_size < 0:
        raise ValueError("effect_size must be finite and nonnegative")
    if not np.isfinite(n) or n < 2 or int(n) != n:
        raise ValueError("n must be an integer of at least two")
    if not np.isfinite(alpha) or not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")
    if paired:
        return float(TTestPower().power(effect_size, n, alpha, alternative="two-sided"))
    return float(TTestIndPower().power(effect_size, n, alpha, ratio=1, alternative="two-sided"))
