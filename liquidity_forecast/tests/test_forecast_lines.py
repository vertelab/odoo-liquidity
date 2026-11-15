# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestForecastLines(TransactionCase):
    def setUp(self):
        super().setUp()
        self.forecast = self.env["liquidity.forecast"].create(
            {
                "date_from": "2026-01-01",
                "horizon_days": 30,
                "opening_balance": 10000.0,
                "opening_balance_manual": True,
            }
        )

    def test_one_line_per_day_including_empty_days(self):
        """A day with no movement still gets a row — the curve has no holes."""
        result = self.forecast._compute_daily_lines()
        lines = self.forecast.forecast_line_ids
        self.assertEqual(len(lines), 30)
        self.assertEqual(
            lines.mapped("date"),
            [self.forecast.date_from + timedelta(days=i) for i in range(30)],
        )
        # No source lines at all: balance stays flat and nothing goes wrong.
        self.assertEqual(result["lowest_balance"], 10000.0)
        self.assertEqual(result["days_to_crisis"], 0)

    def test_unique_per_forecast_and_date(self):
        self.forecast._compute_daily_lines()
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.env["liquidity.forecast.line"].create(
                    {
                        "forecast_id": self.forecast.id,
                        "date": self.forecast.date_from,
                    }
                )

    def test_has_event_false_and_event_type_empty(self):
        """Phase 1 never claims an event: False means 'not computed yet'."""
        self.forecast._compute_daily_lines()
        self.assertFalse(any(self.forecast.forecast_line_ids.mapped("has_event")))
        self.assertFalse(any(self.forecast.forecast_line_ids.mapped("event_type")))

    def test_date_to_matches_horizon(self):
        self.assertEqual(
            self.forecast.date_to,
            self.forecast.date_from + timedelta(days=29),
        )
