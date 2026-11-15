# -*- coding: utf-8 -*-
from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestSimulationStore(TransactionCase):
    """liquidity.simulation and its daily lines, against a real forecast."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.partner = cls.env["res.partner"].create({"name": "Simulation Test Partner"})
        cls.sale_journal = cls.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", cls.company.id)], limit=1
        )
        cls.income_account = cls.env["account.account"].search(
            [("account_type", "=", "income"), ("company_ids", "in", cls.company.ids)],
            limit=1,
        )
        # No chart of accounts is installed in the test database, so the
        # sale journal and the income account may be missing. The invoice
        # helper then has nothing to post against; the tests that need one
        # skip instead of failing for a reason that is not the code's.
        cls.can_invoice = bool(cls.sale_journal and cls.income_account)

    def _invoice(self, amount, maturity):
        if not self.can_invoice:
            self.skipTest("No chart of accounts in the test database")
        move = self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner.id,
                "journal_id": self.sale_journal.id,
                "invoice_date": maturity,
                "invoice_date_due": maturity,
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": self.income_account.id,
                            "quantity": 1.0,
                            "price_unit": amount,
                            "tax_ids": [(6, 0, [])],
                        },
                    )
                ],
            }
        )
        move.action_post()
        return move

    def _forecast(self, days=20, opening=10000.0):
        return self.env["liquidity.forecast"].create(
            {
                "date_from": date(2026, 5, 1),
                "horizon_days": days,
                "opening_balance": opening,
                "opening_balance_manual": True,
            }
        )

    def _simulation(self, forecast, iterations=200):
        return self.env["liquidity.simulation"].create(
            {
                "forecast_id": forecast.id,
                "horizon_days": forecast.horizon_days,
                "iterations": iterations,
            }
        )

    def test_compute_creates_exactly_horizon_lines(self):
        forecast = self._forecast(days=20)
        forecast.action_compute()
        simulation = self._simulation(forecast)
        simulation.action_compute()
        self.assertEqual(simulation.state, "computed")
        self.assertEqual(len(simulation.line_ids), 20)
        self.assertTrue(simulation.computed_at)
        self.assertGreaterEqual(simulation.duration_ms, 0)

    def test_no_opening_balance_raises_and_writes_nothing(self):
        """A band that starts from zero is worse than no band.

        ``Monetary`` coerces ``False`` to 0.0, so "no balance" reaches the
        code as 0.0 -- the guard must be on the value, not on ``is False``.
        """
        forecast = self._forecast(days=10, opening=0.0)
        simulation = self._simulation(forecast)
        with self.assertRaises(UserError):
            simulation.action_compute()
        self.assertFalse(simulation.line_ids)
        self.assertEqual(simulation.state, "draft")

    def test_band_covers_baseline(self):
        """p10 <= baseline <= p90 — a breach is a distribution error, shown."""
        self._invoice(2000.0, date(2026, 5, 5))
        forecast = self._forecast(days=20)
        forecast.action_compute()
        simulation = self._simulation(forecast)
        simulation.action_compute()
        for line in simulation.line_ids:
            if line.baseline_balance is None:
                continue
            self.assertLessEqual(line.p10, line.baseline_balance + 0.01)
            self.assertGreaterEqual(line.p90, line.baseline_balance - 0.01)

    def test_error_state_is_set_on_failure(self):
        """A failure is recorded on the record, never swallowed.

        The failure is provoked through the engine, not by patching the model
        class: assigning an attribute on a model class is caught by Odoo's
        test harness (``metamodel_setattr``) and reported as a leaked
        attribute, which would fail an unrelated assertion later.
        """
        forecast = self._forecast(days=10)
        forecast.action_compute()
        simulation = self._simulation(forecast)

        from ..services import simulation_engine

        original = simulation_engine.run

        def boom(*args, **kwargs):
            raise ValueError("engine exploded")

        simulation_engine.run = boom
        try:
            simulation.action_compute()
        finally:
            simulation_engine.run = original
        self.assertEqual(simulation.state, "error")
        self.assertTrue(simulation.state_message)

    def test_latest_simulation_and_staleness_on_forecast(self):
        forecast = self._forecast(days=10)
        forecast.action_compute()
        simulation = self._simulation(forecast)
        simulation.action_compute()
        simulation.invalidate_recordset()
        forecast.invalidate_recordset()
        self.assertEqual(simulation.state, "computed")
        self.assertEqual(forecast.latest_simulation_id, simulation)
        self.assertFalse(forecast.simulation_is_stale)
        # Recompute the forecast: the band now describes an older baseline.
        forecast.write({"computed_at": simulation.computed_at + timedelta(hours=1)})
        forecast.invalidate_recordset()
        self.assertTrue(forecast.simulation_is_stale)

    def test_no_simulation_means_none_not_zero(self):
        forecast = self._forecast(days=10)
        forecast.action_compute()
        forecast.invalidate_recordset()
        self.assertFalse(forecast.latest_simulation_id)
        self.assertEqual(
            forecast.simulation_p_crisis_display, "—",
            "No simulation must read as a dash, never '0 %' — 'no data' and "
            "'zero risk' are different statements.",
        )

    def test_effective_risk_takes_the_higher_level(self):
        forecast = self._forecast(days=10)
        forecast.action_compute()
        simulation = self._simulation(forecast)
        simulation.action_compute()
        forecast.invalidate_recordset()
        ranking = {"good": 0, "warning": 1, "danger": 2}
        self.assertGreaterEqual(
            ranking[forecast.effective_risk_level],
            ranking[forecast.risk_level or "good"],
        )

    def test_uninstall_safe_fields_are_additive_only(self):
        """The simulation adds fields; it must not touch the line contract."""
        line_fields = self.env["liquidity.forecast.line"]._fields
        for name in ("p10", "p50", "p90", "p_negative"):
            self.assertNotIn(
                name, line_fields,
                "liquidity.forecast.line must gain no probability fields — "
                "the baseline contract is untouched.",
            )

    def test_variance_contributions_are_stored(self):
        self._invoice(1500.0, date(2026, 5, 4))
        forecast = self._forecast(days=15)
        forecast.action_compute()
        simulation = self._simulation(forecast)
        simulation.action_compute()
        self.assertTrue(simulation.variance_contribution_ids)
        total = sum(simulation.variance_contribution_ids.mapped("variance_share"))
        self.assertAlmostEqual(total, 1.0, places=4)
