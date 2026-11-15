# -*- coding: utf-8 -*-
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestCronRegistration(TransactionCase):
    """The tasks are registered through cron_tick, in the right order."""

    def _task(self, technical_name):
        return self.env["cron.task"].search(
            [("technical_name", "=", technical_name)], limit=1
        )

    def test_simulation_task_exists_and_is_daily(self):
        task = self._task("liquidity_simulation_compute")
        self.assertTrue(task)
        self.assertEqual(task.module, "liquidity_simulation")
        self.assertEqual(task.model_name, "liquidity.simulation")
        self.assertEqual(task.method_name, "_cron_simulate_all")
        self.assertEqual(task.cadence, "daily")

    def test_profile_task_exists_and_is_weekly(self):
        task = self._task("liquidity_payment_profiles_refresh")
        self.assertTrue(task)
        self.assertEqual(task.cadence, "week")

    def test_simulation_runs_after_forecast(self):
        """The simulation reads the forecast's daily lines — order matters."""
        forecast_task = self._task("liquidity_forecast_recompute")
        simulation_task = self._task("liquidity_simulation_compute")
        self.assertTrue(forecast_task)
        self.assertTrue(simulation_task)
        self.assertGreater(
            simulation_task.sequence,
            forecast_task.sequence,
            "The simulation must run after the forecast cron, or it reads "
            "yesterday's baseline.",
        )

    def test_cron_entry_points_run_without_error(self):
        self.env["liquidity.simulation"]._cron_simulate_all()
        self.env["liquidity.payment.profile"]._cron_refresh_payment_profiles()
