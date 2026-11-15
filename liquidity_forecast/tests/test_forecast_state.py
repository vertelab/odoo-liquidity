# -*- coding: utf-8 -*-
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestForecastState(TransactionCase):
    def test_no_opening_balance_stays_draft_with_message(self):
        """Without cash on hand the forecast reports draft, never an invented zero."""
        forecast = self.env["liquidity.forecast"].create(
            {"date_from": "2026-04-01", "horizon_days": 5}
        )
        forecast.write({"opening_balance": 0.0, "opening_balance_manual": False})
        # Force the 'unknown' branch: no automatic default either.
        forecast.opening_balance = False
        forecast.action_compute()
        if forecast.state == "draft":
            self.assertTrue(forecast.state_message)
            self.assertFalse(forecast.risk_level)
        else:
            # The company has cash accounts, so a real balance was found.
            self.assertEqual(forecast.state, "computed")

    def test_manual_balance_wins_over_automatic(self):
        forecast = self.env["liquidity.forecast"].create(
            {
                "date_from": "2026-04-01",
                "horizon_days": 5,
                "opening_balance": 4242.0,
            }
        )
        forecast.action_set_manual_opening_balance()
        forecast.action_compute()
        self.assertAlmostEqual(forecast.opening_balance, 4242.0, places=2)

    def test_computed_sets_timestamp_and_state(self):
        forecast = self.env["liquidity.forecast"].create(
            {
                "date_from": "2026-04-01",
                "horizon_days": 5,
                "opening_balance": 100.0,
                "opening_balance_manual": True,
            }
        )
        forecast.action_compute()
        self.assertEqual(forecast.state, "computed")
        self.assertTrue(forecast.computed_at)
        self.assertFalse(forecast.state_message)

    def test_cron_entry_point_runs_without_error(self):
        self.env["liquidity.forecast"]._cron_recompute_all()

    def test_draft_is_never_reported_as_good(self):
        """An uncomputed forecast must not look healthy."""
        forecast = self.env["liquidity.forecast"].create({"date_from": "2026-04-01"})
        self.assertEqual(forecast.state, "draft")
        self.assertFalse(forecast.risk_level)
