# -*- coding: utf-8 -*-
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCronRegistration(TransactionCase):
    """The schedule lives in cron.task, and the module owns none of it.

    These assertions guard the boundary rather than the schedule itself: the
    task must exist, must point at this module's model and method, and the
    forecast model must not grow its own run-duration field. Actual execution,
    spread and run history are cron_tick's job and are verified in operation,
    not here.
    """

    def test_task_is_registered_on_the_forecast_model(self):
        task = self.env["cron.task"].search(
            [("technical_name", "=", "liquidity_forecast_recompute")]
        )
        self.assertEqual(len(task), 1, "Exactly one recompute task must exist.")
        self.assertEqual(task.module, "liquidity_forecast")
        self.assertEqual(task.model_name, "liquidity.forecast")
        self.assertEqual(task.method_name, "_cron_recompute_all")
        self.assertEqual(task.cadence, "daily")

    def test_cadence_and_interval_are_not_both_set(self):
        """cron.task refuses a task that declares both; make sure ours does not."""
        task = self.env["cron.task"].search(
            [("technical_name", "=", "liquidity_forecast_recompute")]
        )
        self.assertTrue(task.cadence)
        self.assertFalse(
            task.interval_number,
            "A task uses cadence OR interval, never both.",
        )

    def test_forecast_model_owns_no_run_duration(self):
        """Run duration belongs to cron.task.log, not to the forecast model."""
        fields = self.env["liquidity.forecast"]._fields
        self.assertNotIn("duration_ms", fields)
