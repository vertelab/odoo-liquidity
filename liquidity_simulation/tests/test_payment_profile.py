# -*- coding: utf-8 -*-
from datetime import date, timedelta

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestPaymentProfile(TransactionCase):
    """Profiles built from real reconciled payments."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.partner = cls.env["res.partner"].create({"name": "Profile Test Partner"})
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", cls.company.id)], limit=1
        )
        cls.income_account = cls.env["account.account"].search(
            [("account_type", "=", "income"), ("company_ids", "in", cls.company.ids)],
            limit=1,
        )
        # No chart of accounts is installed in the test database, so there is
        # no sale journal and no income account to post against. The tests
        # that need real reconciled moves then skip -- they are about the
        # profile arithmetic, not about the chart.
        cls.can_invoice = bool(cls.journal and cls.income_account)

    def _paid_invoice(self, amount, due, paid_on):
        """Post an invoice and reconcile it on a given day."""
        if not self.can_invoice:
            self.skipTest("No chart of accounts in the test database")
        move = self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner.id,
                "journal_id": self.journal.id,
                "invoice_date": due,
                "invoice_date_due": due,
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
        receivable = move.line_ids.filtered(
            lambda l: l.account_type == "asset_receivable"
        )
        # A payment entry against the receivable, dated when it was paid.
        payment = self.env["account.move"].create(
            {
                "move_type": "entry",
                "journal_id": self.journal.id,
                "date": paid_on,
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": receivable.account_id.id,
                            "partner_id": self.partner.id,
                            "name": "payment",
                            "debit": 0.0,
                            "credit": amount,
                            "date_maturity": paid_on,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "account_id": self.income_account.id,
                            "name": "payment",
                            "debit": amount,
                            "credit": 0.0,
                        },
                    ),
                ],
            }
        )
        payment.action_post()
        payment_line = payment.line_ids.filtered(
            lambda l: l.account_type == "asset_receivable"
        )
        (receivable + payment_line).reconcile()
        return move

    def _profile(self):
        return self.env["liquidity.payment.profile"].create(
            {
                "partner_id": self.partner.id,
                "company_id": self.company.id,
                "direction": "receivable",
            }
        )

    def test_thirty_payments_twelve_days_late_gives_high_confidence(self):
        """A customer who always pays 12 days late must not be treated as on time."""
        base = date(2026, 1, 1)
        for index in range(30):
            due = base + timedelta(days=index * 3)
            self._paid_invoice(100.0, due, due + timedelta(days=12))
        profile = self._profile()
        profile.action_refresh()
        self.assertEqual(profile.sample_size, 30)
        self.assertAlmostEqual(profile.mean_delay_days, 12.0, places=2)
        self.assertEqual(profile.confidence, "high")
        self.assertEqual(profile.distribution, "empirical")

    def test_two_payments_gives_low_confidence_and_fallback(self):
        base = date(2026, 1, 1)
        for index in range(2):
            due = base + timedelta(days=index * 5)
            self._paid_invoice(100.0, due, due + timedelta(days=4))
        profile = self._profile()
        profile.action_refresh()
        self.assertEqual(profile.sample_size, 2)
        self.assertEqual(profile.confidence, "low")
        self.assertEqual(profile.distribution, "fallback")

    def test_confidence_thresholds(self):
        classify = self.env["liquidity.payment.profile"]._classify_confidence
        self.assertEqual(classify(20), ("empirical", "high"))
        self.assertEqual(classify(19), ("empirical", "medium"))
        self.assertEqual(classify(5), ("empirical", "medium"))
        self.assertEqual(classify(4), ("fallback", "low"))
        self.assertEqual(classify(0), ("fallback", "low"))

    def test_observations_are_kept_for_audit(self):
        due = date(2026, 1, 10)
        self._paid_invoice(100.0, due, due + timedelta(days=7))
        profile = self._profile()
        profile.action_refresh()
        self.assertTrue(profile.observations)
        self.assertIn("7", profile.observations.split(","))

    def test_get_or_create_never_creates_two_profiles(self):
        Profile = self.env["liquidity.payment.profile"]
        first = Profile._get_or_create_for_partner(
            self.partner, self.company, "receivable"
        )
        second = Profile._get_or_create_for_partner(
            self.partner, self.company, "receivable"
        )
        self.assertEqual(first, second)

    def test_customer_and_vendor_profiles_are_separate(self):
        Profile = self.env["liquidity.payment.profile"]
        customer = Profile._get_or_create_for_partner(
            self.partner, self.company, "receivable"
        )
        vendor = Profile._get_or_create_for_partner(
            self.partner, self.company, "payable"
        )
        self.assertNotEqual(customer, vendor)
