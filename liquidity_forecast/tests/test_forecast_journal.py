# -*- coding: utf-8 -*-
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestForecastJournal(TransactionCase):
    def test_two_calls_create_one_journal(self):
        """Repeated calls must reuse the marked journal, never create a second."""
        Forecast = self.env["liquidity.forecast"]
        first = Forecast._get_or_create_forecast_journal(self.env.company)
        second = Forecast._get_or_create_forecast_journal(self.env.company)
        self.assertEqual(first, second)

        journals = self.env["account.journal"].search(
            [
                ("company_id", "=", self.env.company.id),
                ("is_liquidity_forecast", "=", True),
            ]
        )
        self.assertEqual(len(journals), 1, "Exactly one journal may be marked.")

    def test_journal_is_marked_and_general(self):
        journal = self.env["liquidity.forecast"]._get_or_create_forecast_journal(
            self.env.company
        )
        self.assertTrue(journal.is_liquidity_forecast)
        self.assertEqual(journal.type, "general")
