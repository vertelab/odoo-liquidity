# -*- coding: utf-8 -*-
from datetime import date, timedelta

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestForecastEngine(TransactionCase):
    """Engine behaviour against real invoices and bills."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.partner = cls.env["res.partner"].create({"name": "Forecast Test Partner"})
        cls.sale_journal = cls.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", cls.company.id)], limit=1
        )
        cls.purchase_journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)], limit=1
        )
        cls.income_account = cls.env["account.account"].search(
            [
                ("account_type", "=", "income"),
                ("company_ids", "in", cls.company.ids),
            ],
            limit=1,
        )
        cls.expense_account = cls.env["account.account"].search(
            [
                ("account_type", "=", "expense"),
                ("company_ids", "in", cls.company.ids),
            ],
            limit=1,
        )

    def _invoice(self, amount, maturity, move_type="out_invoice"):
        """Post a real customer invoice or vendor bill with a due date."""
        journal = self.sale_journal if move_type == "out_invoice" else self.purchase_journal
        account = self.income_account if move_type == "out_invoice" else self.expense_account
        move = self.env["account.move"].create(
            {
                "move_type": move_type,
                "partner_id": self.partner.id,
                "journal_id": journal.id,
                "invoice_date": maturity,
                "invoice_date_due": maturity,
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": account.id,
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

    def _forecast(self, days=30, opening=5000.0):
        return self.env["liquidity.forecast"].create(
            {
                "date_from": date(2026, 2, 1),
                "horizon_days": days,
                "opening_balance": opening,
                "opening_balance_manual": True,
            }
        )

    def test_receivable_becomes_incoming_and_payable_outgoing(self):
        self._invoice(1000.0, date(2026, 2, 10))
        self._invoice(400.0, date(2026, 2, 10), move_type="in_invoice")
        forecast = self._forecast()
        forecast.action_compute()

        moves = self.env["account.move.line"].search(
            [
                ("account_type", "in", ("asset_receivable", "liability_payable")),
                ("partner_id", "=", self.partner.id),
            ]
        )
        self.assertEqual(
            sorted(moves.mapped("date_maturity")),
            [date(2026, 2, 10), date(2026, 2, 10)],
            "Test data must actually fall on the maturity date under test.",
        )
        day = forecast.forecast_line_ids.filtered(lambda l: l.date == date(2026, 2, 10))
        self.assertEqual(
            len(day), 1,
            "Lines: %s | Entries: %s" % (
                [(l.date, l.incoming, l.outgoing) for l in forecast.forecast_line_ids
                 if l.incoming or l.outgoing],
                [(e.date, e.journal_id.id, forecast.journal_id.id,
                  [(l.account_id.code, l.date, l.debit, l.credit) for l in e.line_ids])
                 for e in self.env["account.move"].search([("liquidity_type", "=", "forecast")])],
            ),
        )
        self.assertAlmostEqual(day.incoming, 1000.0, places=2)
        self.assertAlmostEqual(day.outgoing, 400.0, places=2)
        self.assertAlmostEqual(day.closing_balance, 5600.0, places=2)

    def test_overdue_receivable_is_outside_the_horizon(self):
        """An overdue receivable keeps its maturity and never moves to today."""
        self._invoice(700.0, date(2025, 12, 1))
        forecast = self._forecast()
        forecast.action_compute()
        self.assertAlmostEqual(sum(forecast.forecast_line_ids.mapped("incoming")), 0.0, places=2)

    def test_lowest_balance_can_be_worked_out_by_hand(self):
        """A seeded book whose trough is known without running the engine.

        Opening 1000 on 1 Feb. Three documents, all inside a 10-day horizon:

          5 Feb  +500 receivable            1000 -> 1500
          6 Feb  -900 payable               1500 ->  600   <- trough
          8 Feb  +100 receivable             600 ->  700

        The trough is 600 on 6 Feb and the balance never goes negative, so
        days_to_crisis is 0. 600 is 60% of the opening 1000 -- above the
        thin-balance threshold -- so risk is 'good'. Anyone re-deriving this
        from the ledger must land on the same numbers.
        """
        self._invoice(500.0, date(2026, 2, 5))
        self._invoice(900.0, date(2026, 2, 6), move_type="in_invoice")
        self._invoice(100.0, date(2026, 2, 8))

        forecast = self._forecast(days=10, opening=1000.0)
        forecast.action_compute()

        self.assertAlmostEqual(forecast.lowest_balance, 600.0, places=2)
        self.assertEqual(forecast.lowest_balance_date, date(2026, 2, 6))
        self.assertEqual(forecast.days_to_crisis, 0)
        self.assertEqual(forecast.risk_level, "good")
        closing = {
            line.date: line.closing_balance for line in forecast.forecast_line_ids
        }
        self.assertAlmostEqual(closing[date(2026, 2, 5)], 1500.0, places=2)
        self.assertAlmostEqual(closing[date(2026, 2, 6)], 600.0, places=2)
        self.assertAlmostEqual(closing[date(2026, 2, 8)], 700.0, places=2)

    def test_real_bookkeeping_is_marked_actual(self):
        """A normal invoice must insert, and say it is real bookkeeping.

        The marker column is nullable on purpose -- Odoo's create pipeline does
        not carry it, so ``required=True`` would make the column NOT NULL with
        no default and every ordinary invoice would fail to insert. This test
        guards both halves of that trade-off: the insert works, and the row
        still carries an unambiguous ``actual``.
        """
        move = self._invoice(120.0, date(2026, 2, 5))
        self.assertEqual(move.liquidity_type, "actual")
        self.assertFalse(move.liquidity_origin)
        forecast_entries = self.env["account.move"].search(
            [("liquidity_type", "=", "forecast")]
        )
        self.assertNotIn(move, forecast_entries)

    def test_forecast_entries_carry_marker_and_origin(self):
        source = self._invoice(250.0, date(2026, 2, 5))
        forecast = self._forecast(days=10, opening=100.0)
        forecast.action_compute()
        entries = self.env["account.move"].search([("liquidity_type", "=", "forecast")])
        self.assertTrue(entries)
        self.assertTrue(all(e.journal_id.is_liquidity_forecast for e in entries))
        self.assertIn(
            source,
            entries.mapped("liquidity_origin"),
            "Every forecast entry must point back at its source document.",
        )

    def test_forecast_entries_never_touch_a_real_account(self):
        """Projected money must not enter the real ledger."""
        self._invoice(900.0, date(2026, 2, 5))
        forecast = self._forecast(days=10, opening=100.0)
        forecast.action_compute()
        entries = self.env["account.move"].search([("liquidity_type", "=", "forecast")])
        holding = forecast.journal_id.liquidity_forecast_account_id
        counterpart = self.env["liquidity.forecast"]._get_or_create_forecast_account(
            forecast.company_id, code="LQDFC01"
        )
        self.assertTrue(holding)
        for entry in entries:
            # An account.move that must balance cannot put both sides on one
            # account and still be read back unambiguously, so the projection is
            # carried by the holding account and its dedicated counterpart.
            # Notes are exempt on purpose: they carry no amount and no account,
            # and exist only to say in the UI that this is not bookkeeping.
            self.assertFalse(
                entry.line_ids.filtered(
                    lambda l: l.account_id
                    and l.account_id not in (holding | counterpart)
                ),
                "A forecast entry touched an account outside the forecast accounts: %s"
                % [(l.id, l.account_id.code) for l in entry.line_ids if l.account_id],
            )

    def test_recompute_does_not_duplicate_entries(self):
        self._invoice(300.0, date(2026, 2, 5))
        forecast = self._forecast(days=10, opening=100.0)
        forecast.action_compute()
        first = self.env["account.move"].search_count([("liquidity_type", "=", "forecast")])
        forecast.action_compute()
        second = self.env["account.move"].search_count([("liquidity_type", "=", "forecast")])
        self.assertEqual(first, second)

    def test_paid_invoice_leaves_the_forecast(self):
        move = self._invoice(500.0, date(2026, 2, 5))
        forecast = self._forecast(days=10)
        forecast.action_compute()
        self.assertAlmostEqual(sum(forecast.forecast_line_ids.mapped("incoming")), 500.0, places=2)

        payment = self.env["account.payment"].create(
            {
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": self.partner.id,
                "amount": 500.0,
                "journal_id": self.env["account.journal"]
                .search([("type", "=", "bank"), ("company_id", "=", self.company.id)], limit=1)
                .id,
            }
        )
        payment.action_post()
        (move + payment.move_id).line_ids.filtered(
            lambda l: l.account_type == "asset_receivable"
        ).reconcile()

        forecast.action_compute()
        self.assertAlmostEqual(
            sum(forecast.forecast_line_ids.mapped("incoming")),
            0.0,
            places=2,
            msg="A settled invoice must no longer be projected as incoming cash.",
        )
