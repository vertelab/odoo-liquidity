# -*- coding: utf-8 -*-
"""days_to_crisis = 0 must never read as 'crisis today'.

This is the single most likely wrong reading in the whole portfolio: a healthy
customer whose forecast finds no crisis reports 0, and a view that treats 0 as
"now" would paint every healthy customer red.
"""
from odoo.tests.common import TransactionCase

from ..services import kpi_reader


class TestDaysToCrisis(TransactionCase):

    def test_zero_means_no_crisis_in_horizon(self):
        result = kpi_reader.interpret_days_to_crisis(0)
        self.assertEqual(result["kind"], "none_in_horizon")
        self.assertIsNone(result["days"])

    def test_one_means_crisis_now(self):
        """A negative opening balance gives 1 — that is a real crisis."""
        result = kpi_reader.interpret_days_to_crisis(1)
        self.assertEqual(result["kind"], "crisis")
        self.assertEqual(result["days"], 1)

    def test_positive_value_is_crisis(self):
        result = kpi_reader.interpret_days_to_crisis(47)
        self.assertEqual(result["kind"], "crisis")
        self.assertEqual(result["days"], 47)

    def test_none_is_unknown(self):
        result = kpi_reader.interpret_days_to_crisis(None)
        self.assertEqual(result["kind"], "unknown")
        self.assertIsNone(result["days"])
