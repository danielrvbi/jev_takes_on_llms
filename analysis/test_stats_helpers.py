"""Offline checks for helper settings and input handling."""

import unittest

import numpy as np
from scipy import stats
from statsmodels.stats.power import TTestIndPower, TTestPower

from analysis.stats_helpers import levene, mean_power, welch


class StatsHelpersTests(unittest.TestCase):
    def test_welch_uses_unequal_variances_and_exposes_interval(self):
        a, b = [1, 2, 3, 4], [0, 10, 20, 30, 40, 50]
        result = welch(a, b)
        expected = stats.ttest_ind(a, b, equal_var=False)
        self.assertAlmostEqual(result.statistic, expected.statistic)
        self.assertAlmostEqual(result.pvalue, expected.pvalue)
        self.assertEqual(result.confidence_interval(), expected.confidence_interval())
        self.assertNotAlmostEqual(result.pvalue, stats.ttest_ind(a, b).pvalue)

    def test_levene_uses_medians_for_skewed_samples(self):
        a, b = [0, 1, 2, 3, 100], [0, 1, 2, 3, 4, 5]
        self.assertEqual(levene(a, b), stats.levene(a, b, center="median"))
        self.assertNotAlmostEqual(levene(a, b).pvalue, stats.levene(a, b, center="mean").pvalue)

    def test_samples_require_finite_vectors_and_two_observations(self):
        for helper in (welch, levene):
            for bad in ([], [1], [[1, 2]], [1, np.nan], [1, np.inf]):
                with self.subTest(helper=helper.__name__, sample=bad):
                    with self.assertRaises(ValueError):
                        helper(bad, [1, 2, 3])
                    with self.assertRaises(ValueError):
                        helper([1, 2, 3], bad)

    def test_power_uses_correct_sample_unit_and_increases_with_n(self):
        self.assertAlmostEqual(mean_power(0.5), TTestIndPower().power(0.5, 40, 0.05, ratio=1))
        self.assertAlmostEqual(mean_power(0.5, n=10, paired=True), TTestPower().power(0.5, 10, 0.05))
        self.assertGreater(mean_power(0.5, n=40), mean_power(0.5, n=10))
        self.assertAlmostEqual(mean_power(0), 0.05)

    def test_invalid_power_parameters(self):
        for kwargs in ({"effect_size": -1}, {"effect_size": np.nan},
                       {"effect_size": np.inf}, {"n": 1}, {"n": 2.5},
                       {"n": np.inf}, {"alpha": 0}, {"alpha": 1}, {"alpha": np.nan}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                mean_power(**({"effect_size": 0.5} | kwargs))


if __name__ == "__main__":
    unittest.main()
