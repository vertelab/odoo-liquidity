# -*- coding: utf-8 -*-
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestForecastRisk(TransactionCase):
    def setUp(self):
        super().setUp()
        self.forecast = self.env["liquidity.forecast"].create(
            {"date_from": "2026-03-01", "opening_balance": 1000.0}
        )

    def _risk(self, days, lowest):
        return self.forecast._compute_risk_level(days, lowest)

    def test_crisis_inside_fourteen_days_is_danger(self):
        self.assertEqual(self._risk(1, -500.0), "danger")
        self.assertEqual(self._risk(14, -1.0), "danger")

    def test_crisis_after_fourteen_days_does_not_trigger_danger(self):
        """15 days is outside the danger window — the amount decides instead."""
        self.assertNotEqual(self._risk(15, -500.0), "danger")

    def test_crisis_day_beats_thin_amount(self):
        """A tiny shortfall soon is worse than a large one far away."""
        self.assertEqual(self._risk(3, -1.0), "danger")
        self.assertNotEqual(self._risk(80, -900.0), "danger")

    def test_thin_balance_below_ten_percent_is_warning(self):
        self.assertEqual(self._risk(0, 50.0), "warning")

    def test_comfortable_balance_is_good(self):
        self.assertEqual(self._risk(0, 500.0), "good")

    def test_days_to_crisis_zero_means_no_crisis(self):
        """0 is the 'no crisis' sentinel; day one gives 1, never 0."""
        self.assertEqual(self._risk(0, 500.0), "good")
        self.assertEqual(self._risk(1, -1.0), "danger")
