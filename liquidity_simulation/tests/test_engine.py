# -*- coding: utf-8 -*-
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..services import distributions, simulation_engine


def _fixed_item(key, amount, due_offset, event_type=False):
    return simulation_engine.SimItem(
        key=key,
        label=key,
        amount=amount,
        due_offset=due_offset,
        dist=distributions.FixedDist(0),
        event_type=event_type,
    )


@tagged("post_install", "-at_install")
class TestSimulationEngine(TransactionCase):
    """The engine, exercised as plain data — no ORM in the arithmetic."""

    def test_same_seed_gives_identical_percentiles(self):
        """Reproducibility is the foundation; everything else rests on it."""
        item = simulation_engine.SimItem(
            key="a",
            label="a",
            amount=1000.0,
            due_offset=3,
            dist=distributions.EmpiricalDist([0, 5, 12, 30]),
        )
        first = simulation_engine.run([item], 5000.0, horizon_days=30,
                                      iterations=500, seed=42)
        second = simulation_engine.run([item], 5000.0, horizon_days=30,
                                       iterations=500, seed=42)
        self.assertEqual(
            [l["p50"] for l in first["lines"]],
            [l["p50"] for l in second["lines"]],
        )
        self.assertEqual(first["p_crisis_90"], second["p_crisis_90"])

    def test_different_seed_gives_different_result(self):
        item = simulation_engine.SimItem(
            key="a",
            label="a",
            amount=1000.0,
            due_offset=3,
            dist=distributions.EmpiricalDist([0, 5, 12, 30]),
        )
        first = simulation_engine.run([item], 5000.0, horizon_days=30,
                                      iterations=500, seed=1)
        second = simulation_engine.run([item], 5000.0, horizon_days=30,
                                       iterations=500, seed=2)
        self.assertNotEqual(
            [l["p50"] for l in first["lines"]],
            [l["p50"] for l in second["lines"]],
        )

    def test_fixed_items_collapse_the_band(self):
        """Only deterministic items -> the band is a line."""
        items = [
            _fixed_item("in", 1000.0, 5),
            _fixed_item("out", -400.0, 10),
        ]
        result = simulation_engine.run(items, 2000.0, horizon_days=20,
                                       iterations=200, seed=7)
        for line in result["lines"]:
            self.assertAlmostEqual(line["p10"], line["p50"], places=2)
            self.assertAlmostEqual(line["p50"], line["p90"], places=2)
        self.assertEqual(result["p_crisis_90"], 0.0)

    def test_percentiles_are_monotonic_every_day(self):
        item = simulation_engine.SimItem(
            key="a",
            label="a",
            amount=1000.0,
            due_offset=2,
            dist=distributions.EmpiricalDist([0, 10, 40, 80]),
        )
        result = simulation_engine.run([item], 500.0, horizon_days=90,
                                       iterations=400, seed=11)
        for line in result["lines"]:
            self.assertLessEqual(line["p10"], line["p50"])
            self.assertLessEqual(line["p50"], line["p90"])
            self.assertLessEqual(line["p90"], line["p95"])
            self.assertLessEqual(line["p95"], line["p99"])

    def test_p_crisis_is_share_of_iterations_not_sum_of_days(self):
        """Design decision 7 — the bug that looks plausible.

        One payable that is paid with 20 % probability and whose payment
        empties the till. The crisis probability must be about 0.2, not the
        sum of the daily probabilities (which would be far higher, because
        the balance stays negative for the rest of the horizon).
        """
        # Opening balance 1000, an outgoing of 2000 due on day 1 that is paid
        # in 20 % of the iterations (p_pay = 0.2). When it IS paid the till
        # goes negative; when it is not, the balance stays at 1000.
        #
        # The item must be *outgoing* (negative amount). An incoming of 2000
        # can never take a balance of 1000 below zero, so that fixture would
        # assert a crisis probability the arithmetic cannot produce.
        item = simulation_engine.SimItem(
            key="a",
            label="a",
            amount=-2000.0,
            due_offset=1,
            source_type="payable",
            dist=distributions.BernoulliDist(0.2, fixed_delay=0),
        )
        result = simulation_engine.run([item], 1000.0, horizon_days=30,
                                       iterations=4000, seed=13)
        self.assertGreater(result["p_crisis_90"], 0.15)
        self.assertLess(result["p_crisis_90"], 0.25)

        daily_sum = sum(line["p_negative"] for line in result["lines"])
        self.assertGreater(
            daily_sum, result["p_crisis_90"] * 2,
            "The daily probabilities must sum well above the iteration share "
            "— otherwise this test is not exercising the difference.",
        )

    def test_unpaid_item_adds_nothing_on_any_day(self):
        """A defaulted invoice is not deferred to the last day."""
        item = simulation_engine.SimItem(
            key="a",
            label="a",
            amount=1000.0,
            due_offset=2,
            dist=distributions.BernoulliDist(0.0, fixed_delay=0),
        )
        result = simulation_engine.run([item], 500.0, horizon_days=10,
                                       iterations=50, seed=17)
        for line in result["lines"]:
            self.assertAlmostEqual(line["p50"], 500.0, places=2)

    def test_line_count_equals_horizon(self):
        result = simulation_engine.run([], 100.0, horizon_days=45,
                                       iterations=10, seed=1)
        self.assertEqual(len(result["lines"]), 45)

    def test_worst_case_p95_is_below_the_median(self):
        item = simulation_engine.SimItem(
            key="a",
            label="a",
            amount=1000.0,
            due_offset=3,
            dist=distributions.EmpiricalDist([0, 20, 60]),
        )
        result = simulation_engine.run([item], 800.0, horizon_days=40,
                                       iterations=1000, seed=19)
        self.assertLessEqual(result["worst_case_p95"], result["lowest_p50"])

    def test_variance_share_sums_to_about_one(self):
        items = [
            simulation_engine.SimItem(
                key="a", label="a", amount=1000.0, due_offset=2,
                dist=distributions.EmpiricalDist([0, 10, 30]),
            ),
            simulation_engine.SimItem(
                key="b", label="b", amount=800.0, due_offset=5,
                dist=distributions.EmpiricalDist([0, 40, 80]),
            ),
        ]
        result = simulation_engine.run(items, 3000.0, horizon_days=60,
                                       iterations=500, seed=23)
        total = sum(result["variance_share"].values())
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_deterministic_item_has_zero_variance_share(self):
        """A fixed item explains no uncertainty, regardless of its size."""
        items = [
            _fixed_item("big", -100000.0, 5),
            simulation_engine.SimItem(
                key="small", label="small", amount=100.0, due_offset=2,
                dist=distributions.EmpiricalDist([0, 30, 60]),
            ),
        ]
        result = simulation_engine.run(items, 200000.0, horizon_days=40,
                                       iterations=500, seed=29)
        self.assertEqual(result["variance_share"]["big"], 0.0)
        self.assertAlmostEqual(result["variance_share"]["small"], 1.0, places=6)

    def test_event_markers_are_reported(self):
        items = [_fixed_item("tax", -500.0, 4, event_type="tax")]
        result = simulation_engine.run(items, 5000.0, horizon_days=10,
                                       iterations=10, seed=31)
        self.assertEqual(result["lines"][4]["expected_events"], ["tax"])
        self.assertEqual(result["lines"][3]["expected_events"], [])
