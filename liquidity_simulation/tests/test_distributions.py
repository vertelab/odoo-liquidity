# -*- coding: utf-8 -*-
import random

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..services import distributions


@tagged("post_install", "-at_install")
class TestDistributions(TransactionCase):
    """The distribution classes, without a database."""

    def test_empirical_returns_observed_values_only(self):
        """Bootstrap never invents a delay that was not observed."""
        dist = distributions.EmpiricalDist([12, 12, 12])
        rng = random.Random(1)
        for _ in range(200):
            day, factor = dist.sample(rng)
            self.assertEqual(day, 12)
            self.assertEqual(factor, 1.0)

    def test_empirical_never_produces_negative_delay(self):
        """A customer who only pays late must not pay early in the model."""
        dist = distributions.EmpiricalDist([5, 10, 20])
        rng = random.Random(2)
        for _ in range(500):
            day, _factor = dist.sample(rng)
            self.assertIn(day, (5, 10, 20))
            self.assertGreaterEqual(day, 0)

    def test_outlier_cap_clips_at_sampling_not_in_history(self):
        """A 400-day payment is capped when drawn, but still counted.

        The sample size must keep telling the truth about how much history
        exists — capping the observation list would silently shrink it.
        """
        dist = distributions.EmpiricalDist([1, 2, 400], cap_days=90)
        self.assertEqual(dist.sample_size, 3)
        rng = random.Random(3)
        seen = set()
        for _ in range(200):
            day, _factor = dist.sample(rng)
            seen.add(day)
        self.assertIn(90, seen, "The 400-day observation must be capped to 90.")
        self.assertNotIn(400, seen)
        self.assertEqual(dist.sample_size, 3, "sample_size must not change")

    def test_bernoulli_never_pays_when_unpaid(self):
        """paid=False means the amount never arrives — not that it arrives late."""
        dist = distributions.BernoulliDist(0.0, fixed_delay=5)
        rng = random.Random(4)
        for _ in range(50):
            day, factor = dist.sample(rng)
            self.assertIsNone(day, "An unpaid item must have no day at all.")
            self.assertEqual(factor, 0.0)

    def test_bernoulli_pays_when_paid(self):
        dist = distributions.BernoulliDist(1.0, fixed_delay=7)
        rng = random.Random(5)
        for _ in range(50):
            day, factor = dist.sample(rng)
            self.assertEqual(day, 7)
            self.assertEqual(factor, 1.0)

    def test_bernoulli_rate_is_roughly_right(self):
        dist = distributions.BernoulliDist(0.2, fixed_delay=0)
        rng = random.Random(6)
        paid = sum(1 for _ in range(2000) if dist.sample(rng)[0] is not None)
        self.assertGreater(paid / 2000.0, 0.15)
        self.assertLess(paid / 2000.0, 0.25)

    def test_fixed_dist_is_deterministic(self):
        dist = distributions.FixedDist(3)
        rng = random.Random(7)
        samples = {dist.sample(rng) for _ in range(100)}
        self.assertEqual(samples, {(3, 1.0)})
        self.assertTrue(dist.is_deterministic)

    def test_normal_dist_clips_negative_delays(self):
        """A normal fallback must not invent early payments either."""
        dist = distributions.NormalDist(2.0, 10.0)
        rng = random.Random(8)
        for _ in range(500):
            day, _factor = dist.sample(rng)
            self.assertGreaterEqual(day, 0)

    def test_outlier_cap_formula(self):
        self.assertEqual(distributions.outlier_cap_days(2.0), 90)
        self.assertEqual(distributions.outlier_cap_days(40.0), 120)
        self.assertEqual(distributions.outlier_cap_days(40.0, cap=50), 50)

    def test_fallback_is_wider_than_empirics(self):
        """The fallback must err wide, never narrow."""
        dist = distributions.build_delay_dist(
            "fallback", mean_delay_days=5.0, stdev_delay_days=1.0
        )
        self.assertIsInstance(dist, distributions.NormalDist)
        self.assertGreaterEqual(
            dist.stdev, distributions.FALLBACK_PROFILE["stdev_delay_days"]
        )

    def test_no_numpy_scipy_pandas_import(self):
        """The module must stay dependency-free (design decision 4)."""
        import os

        module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        banned = ("import numpy", "import scipy", "import pandas",
                  "from numpy", "from scipy", "from pandas")
        # The test file itself names the banned modules in order to check for
        # them -- scanning it would always fail. Only production code counts.
        tests_dir = os.path.abspath(os.path.join(module_root, "tests"))
        offenders = []
        for dirpath, _dirnames, filenames in os.walk(module_root):
            if "__pycache__" in dirpath:
                continue
            if os.path.abspath(dirpath) == tests_dir:
                continue
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, "r", encoding="utf-8") as handle:
                    text = handle.read()
                for needle in banned:
                    if needle in text:
                        offenders.append((path, needle))
        self.assertFalse(offenders, "Banned imports found: %s" % offenders)
