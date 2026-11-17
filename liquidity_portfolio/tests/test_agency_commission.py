# -*- coding: utf-8 -*-
"""The commission model, asserted against the four rules.

Every test here corresponds to a way the model could pay for something that did
not happen. They are written that way on purpose: the arithmetic is easy, and
the rules are what the money depends on.
"""
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestAgencyCommission(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agency = cls.env["res.partner"].create({"name": "Byrån AB"})
        cls.other_agency = cls.env["res.partner"].create({"name": "Annan Byrå AB"})

    def _tenant(self, name, source="agency", agency=None):
        return self.env["liquidity.tenant"].create({
            "name": name,
            "base_url": "https://%s.test" % name.lower().replace(" ", "-"),
            "db_name": name.lower().replace(" ", "_"),
            "login": "byra@%s.test" % name.lower().replace(" ", "-"),
            "source": source,
            "owning_agency_id": (agency or self.agency).id if source == "agency" else False,
        })

    def _agreement(self, tenant, agency=None, state="active", brought_in=None):
        agreement = self.env["liquidity.agency.agreement"].create({
            "agency_partner_id": (agency or self.agency).id,
            "tenant_id": tenant.id,
            "brought_in_at": brought_in or date(2026, 1, 1),
        })
        if state == "active":
            agreement.action_activate()
        return agreement

    # -- rule 1: only an active agreement on a paying customer ------------

    def test_a_draft_agreement_earns_nothing(self):
        tenant = self._tenant("Kund A")
        agreement = self._agreement(tenant, state="draft")
        self.assertEqual(agreement._compute_signup_commission(10000.0), 0.0)

    def test_an_ended_agreement_earns_nothing(self):
        tenant = self._tenant("Kund B")
        agreement = self._agreement(tenant)
        agreement.write({"state": "ended"})
        self.assertEqual(agreement._compute_recurring_commission(10000.0), 0.0)

    # -- rule 2: Vertel's customers cannot be claimed ---------------------

    def test_a_vertel_customer_earns_nothing(self):
        """The rule that has no back door."""
        tenant = self._tenant("Vertel Kund", source="vertel")
        agreement = self._agreement(tenant)
        self.assertEqual(
            agreement._compute_signup_commission(10000.0), 0.0,
            "An agency attached to a Vertel customer must never create a claim.",
        )
        self.assertEqual(
            agreement._compute_recurring_commission(10000.0), 0.0,
            "And the recurring bonus must not apply either -- otherwise the "
            "agency has an incentive to convert Vertel customers.",
        )

    def test_a_vertel_customer_does_not_count_towards_the_tier(self):
        """3.5: the back door in the tier calculation."""
        for index in range(10):
            tenant = self._tenant("Vertel %s" % index, source="vertel")
            self._agreement(tenant)
        agency_tenant = self._tenant("Byrå Kund")
        agreement = self._agreement(agency_tenant)
        self.assertEqual(
            agreement.recurring_rate, 0.0,
            "Ten Vertel customers must not push the agency into a tier. "
            "Only customers the agency actually brought in count.",
        )

    # -- rule 3: a returning customer is not a new customer ---------------

    def test_a_returning_customer_gets_no_new_signup_commission(self):
        tenant = self._tenant("Återkommande Kund")
        agreement = self._agreement(tenant, brought_in=date(2025, 1, 1))
        self.assertEqual(agreement._compute_signup_commission(10000.0), 3000.0)
        # A real run records that it was earned; the computation alone does not.
        agreement.action_mark_signup_paid(on_date=date(2025, 1, 1))

        agreement.action_end(on_date=date(2026, 6, 1))
        agreement.write({"state": "ended"})
        agreement.action_reopen()

        self.assertEqual(
            agreement.brought_in_at, date(2025, 1, 1),
            "brought_in_at must survive the round trip, or the signup "
            "commission can be earned twice by letting a customer churn.",
        )
        self.assertEqual(
            agreement._compute_signup_commission(10000.0), 0.0,
            "A reopened agreement is not a new one: the signup commission was "
            "already paid.",
        )

    # -- rule 4: ending is a ramp -----------------------------------------

    def test_ending_sets_a_six_month_ramp(self):
        tenant = self._tenant("Kund C")
        agreement = self._agreement(tenant)
        agreement.action_end(on_date=date(2026, 6, 1))
        self.assertEqual(agreement.state, "ending")
        self.assertEqual(agreement.recurring_until, date(2026, 12, 1))

    def test_the_ramp_pays_until_the_end_date_and_then_stops(self):
        tenant = self._tenant("Kund D")
        # Brought in a year ago, so the recurring bonus has started -- that is
        # the situation the ramp applies to.
        agreement = self._agreement(tenant, brought_in=date(2025, 1, 1))
        agreement.write({"recurring_from": date(2026, 1, 1)})
        agreement.action_end(on_date=date(2026, 6, 1))

        # The rate is passed explicitly: one customer is below the first tier,
        # so the tier would mask what this test is measuring -- the ramp, which
        # is about dates and nothing else.
        self.assertGreater(
            agreement._compute_recurring_commission(
                10000.0, on_date=date(2026, 11, 1), rate=0.10),
            0.0, "Inside the ramp the bonus still pays.",
        )
        self.assertEqual(
            agreement._compute_recurring_commission(
                10000.0, on_date=date(2027, 1, 1), rate=0.10),
            0.0, "After the ramp it stops -- with no cliff, but with an end.",
        )

    def test_the_cron_finishes_an_expired_ramp(self):
        tenant = self._tenant("Kund E")
        agreement = self._agreement(tenant)
        agreement.action_end(on_date=date.today() - relativedelta(months=7))
        self.env["liquidity.agency.agreement"]._cron_finish_ending()
        agreement.invalidate_recordset()
        self.assertEqual(agreement.state, "ended")
        self.assertTrue(agreement.ended_at)

    # -- the arithmetic itself --------------------------------------------

    def test_signup_is_thirty_percent_of_the_first_period(self):
        tenant = self._tenant("Kund F")
        agreement = self._agreement(tenant)
        self.assertAlmostEqual(
            agreement._compute_signup_commission(10000.0), 3000.0
        )

    def test_the_recurring_tier_needs_ten_customers(self):
        tenant = self._tenant("Kund G")
        agreement = self._agreement(tenant)
        self.assertEqual(agreement.recurring_rate, 0.0, "One customer is not ten.")

        for index in range(9):
            extra = self._tenant("Extra %s" % index)
            self._agreement(extra)

        agreement.invalidate_recordset()
        counted = self.env["liquidity.agency.agreement"].search_count([
            ("agency_partner_id", "=", self.agency.id),
            ("source", "=", "agency"),
            ("state", "in", ("active", "ending")),
        ])
        self.assertEqual(counted, 10, "the fixture must create ten active agreements")
        self.assertAlmostEqual(agreement.recurring_rate, 0.10)

    def test_the_top_tier_needs_twenty_customers(self):
        tenant = self._tenant("Kund H")
        agreement = self._agreement(tenant)
        for index in range(19):
            extra = self._tenant("Topp %s" % index)
            self._agreement(extra)
        agreement.invalidate_recordset()
        self.assertAlmostEqual(agreement.recurring_rate, 0.20)

    def test_recurring_does_not_start_before_recurring_from(self):
        tenant = self._tenant("Kund I")
        agreement = self._agreement(tenant)
        agreement.write({"recurring_from": date(2027, 1, 1)})
        self.assertEqual(
            agreement._compute_recurring_commission(10000.0, on_date=date(2026, 6, 1)),
            0.0, "The first period is the signup commission's, not the bonus's.",
        )


@tagged("post_install", "-at_install")
class TestCustomerSourceIsFixed(TransactionCase):
    """2.2: who the customer belongs to is set at onboarding and never changed."""

    def test_source_cannot_be_changed(self):
        tenant = self.env["liquidity.tenant"].create({
            "name": "Fast Källa",
            "base_url": "https://fast.test",
            "db_name": "fast",
            "login": "byra@fast.test",
            "source": "vertel",
        })
        with self.assertRaises(ValidationError):
            tenant.write({"source": "agency"})

    def test_writing_the_same_source_is_allowed(self):
        """An idempotent write must not raise: forms send the whole record."""
        tenant = self.env["liquidity.tenant"].create({
            "name": "Samma Källa",
            "base_url": "https://samma.test",
            "db_name": "samma",
            "login": "byra@samma.test",
            "source": "vertel",
        })
        tenant.write({"source": "vertel", "name": "Samma Källa AB"})
        self.assertEqual(tenant.source, "vertel")
