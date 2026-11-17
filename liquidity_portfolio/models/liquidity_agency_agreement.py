# -*- coding: utf-8 -*-
"""The commission agreement: what a firm earns for a customer it brought in.

The model is Spiris' two-part split, with the ending written down. The market
research is in the proposal; what matters here is the arithmetic and the rules
that stop it from paying for something that did not happen.

Four rules, and each one closes a way to game the model:

1. **Only an active agreement on a paying customer earns anything.** A customer
   who stopped paying still exists in the portfolio; the commission must not.
   That is the silent leak nobody notices for a year.

2. **Vertel's own customers cannot be claimed.** No signup commission, no
   recurring bonus, no "but we introduced them in 2023". The rule has to have
   no back door, or it is not a rule.

3. **A returning customer is not a new customer.** ``brought_in_at`` stays, so
   a firm cannot earn 30 % twice by letting a customer churn and come back.

4. **Ending is a ramp, not a cliff.** Six months, then zero. A firm that built
   the relationship should not lose everything in a day -- and a firm that
   keeps customers it does not want is worse for everyone.

This module computes the basis. It does not invoice and does not pay out: that
is an accounting matter, and belongs where invoicing already lives.
"""
import logging
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

#: The signup commission, as a share of the first subscription period.
SIGNUP_RATE = 0.30

#: The recurring bonus, scaled on how many customers the firm has brought in.
#: Bokio's thresholds punish small firms -- 0 % until ten customers -- so the
#: first step here is deliberately low.
RECURRING_TIERS = (
    (20, 0.20),
    (10, 0.10),
)
RECURRING_RATE_BELOW_FIRST_TIER = 0.0

#: How long the recurring bonus runs after the firm leaves the customer.
ENDING_MONTHS = 6


class LiquidityAgencyAgreement(models.Model):
    """One firm's claim on one customer."""

    _name = "liquidity.agency.agreement"
    _description = "Agency commission agreement"
    _order = "brought_in_at desc, id desc"
    _rec_name = "display_name"

    agency_partner_id = fields.Many2one(
        "res.partner",
        string="Agency",
        required=True,
        ondelete="restrict",
        index=True,
    )
    tenant_id = fields.Many2one(
        "liquidity.tenant",
        string="Customer",
        required=True,
        ondelete="restrict",
        index=True,
    )
    display_name = fields.Char(compute="_compute_display_name", store=True)
    source = fields.Selection(
        related="tenant_id.source",
        string="Customer of",
        store=True,
        readonly=True,
        help="Read from the customer. An agreement on a Vertel customer "
             "earns nothing -- the rule is not enforced by hiding the field "
             "but by the computation returning zero.",
    )
    brought_in_at = fields.Date(
        string="Brought in",
        required=True,
        default=fields.Date.context_today,
        help="When the agency brought the customer in. This date is never "
             "moved: a returning customer keeps theirs, which is what stops "
             "the signup commission being earned twice.",
    )
    signup_rate = fields.Float(
        string="Signup rate",
        default=SIGNUP_RATE,
        digits=(5, 4),
        help="Share of the first subscription period. Paid once, at the start.",
    )
    recurring_rate = fields.Float(
        string="Recurring rate",
        digits=(5, 4),
        compute="_compute_recurring_rate",
        # NOT stored. The rate depends on how many agreements the agency has,
        # which changes whenever *any other* agreement is created or ended --
        # and Odoo's dependency system cannot express "any record of this
        # agency". A stored compute here would silently go stale: the first
        # agreement would keep rate 0 forever, because creating the tenth one
        # does not touch it. Non-stored is always right when read, and this
        # value is displayed and used in arithmetic, never searched on.
        store=False,
        help="Scaled on how many customers the agency has brought in. "
             "Computed, not typed: a rate that can be edited by hand is a rate "
             "that will be.",
    )
    recurring_from = fields.Date(
        string="Recurring from",
        help="When the recurring bonus starts. Empty means 'after the first "
             "period', which is the normal case.",
    )
    recurring_until = fields.Date(
        string="Recurring until",
        help="Empty means 'until further notice'. Set when the agreement "
             "enters its ending, so the ramp has a visible end date.",
    )
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("active", "Active"),
            ("ending", "Ending"),
            ("ended", "Ended"),
        ],
        string="Status",
        default="draft",
        required=True,
        index=True,
        tracking=False,
    )
    ended_at = fields.Date(string="Ended", readonly=True)
    signup_paid_at = fields.Date(
        string="Signup commission paid",
        readonly=True,
        help="When the signup commission was earned. Set once and never "
             "cleared: a reopened agreement is not a new one, and without this "
             "the signup could be earned again by letting a customer churn "
             "and come back.",
    )
    note = fields.Text(string="Note")

    _sql_constraints = [
        (
            "agency_tenant_active_uniq",
            "unique(agency_partner_id, tenant_id)",
            "This agency already has an agreement for this customer.",
        ),
    ]

    @api.depends("agency_partner_id.name", "tenant_id.name")
    def _compute_display_name(self):
        for agreement in self:
            agreement.display_name = "%s / %s" % (
                agreement.agency_partner_id.name or "",
                agreement.tenant_id.name or "",
            )

    @api.constrains("tenant_id", "source")
    def _check_source_matches(self):
        """An agreement on a Vertel customer is allowed but earns nothing.

        Refusing it outright would hide the real situation: a firm may well
        have introduced a customer Vertel already had. What must not happen is
        that the agreement pays out, so the computation returns zero and the
        form says why.
        """
        for agreement in self:
            if agreement.tenant_id.source != agreement.source:
                raise ValidationError(
                    _("The agreement's source does not match the customer's.")
                )

    # -- the arithmetic ----------------------------------------------------

    @api.depends("agency_partner_id", "state")
    def _compute_recurring_rate(self):
        """The bonus tier, from how many customers this agency brought in.

        Only customers that are actually the agency's count. A Vertel customer
        with an agreement attached must not inflate the tier -- that would be
        the back door Beslut 4 closes.
        """
        for agreement in self:
            if not agreement.agency_partner_id:
                agreement.recurring_rate = RECURRING_RATE_BELOW_FIRST_TIER
                continue
            # ``self`` may not be in the search result yet while creating, so
            # the agreement being computed is counted explicitly. Otherwise the
            # first agreement an agency creates always sees a count of zero.
            counted = self.search([
                ("agency_partner_id", "=", agreement.agency_partner_id.id),
                ("source", "=", "agency"),
                ("state", "in", ("active", "ending")),
            ])
            count = len(counted | agreement)
            rate = RECURRING_RATE_BELOW_FIRST_TIER
            for threshold, tier_rate in RECURRING_TIERS:
                if count >= threshold:
                    rate = tier_rate
                    break
            agreement.recurring_rate = rate

    def _is_earning(self):
        """Whether this agreement pays anything at all this period."""
        self.ensure_one()
        if self.state not in ("active", "ending"):
            return False
        # The rule that matters most: a Vertel customer earns nothing, ever.
        if self.source != "agency":
            return False
        return True

    def _compute_signup_commission(self, first_period_amount):
        """30 % of the first subscription period, once.

        Returns 0.0 when the agreement does not earn -- including for a
        returning customer, whose ``brought_in_at`` predates the current
        subscription and who therefore already had their signup commission.
        """
        self.ensure_one()
        if not self._is_earning():
            return 0.0
        if self.signup_paid_at:
            # Already paid, whatever the state is now. This is what makes
            # action_reopen safe.
            return 0.0
        if self.state != "active":
            return 0.0
        return round(first_period_amount * (self.signup_rate or 0.0), 2)

    def action_mark_signup_paid(self, on_date=None):
        """Record that the signup commission has been earned.

        Separate from ``_compute_signup_commission`` because the computation
        must stay a pure question ("what does this pay?") while the fact that
        it *was* paid is a record. Mixing them would make the answer depend on
        when it was asked.
        """
        for agreement in self:
            if not agreement.signup_paid_at and agreement._compute_signup_commission(1.0):
                agreement.signup_paid_at = on_date or fields.Date.context_today(self)
        return True

    def _compute_recurring_commission(self, period_amount, on_date=None, rate=None):
        """The recurring bonus for one period, on a given date.

        Zero when the agreement is not earning, when the ramp has run out, or
        when the customer is not paying.

        ``rate`` overrides the tier, for a caller that has already decided the
        rate (a report for a closed period, or a test of the ramp in
        isolation). When it is omitted the tier applies.
        """
        self.ensure_one()
        if not self._is_earning():
            return 0.0
        on_date = on_date or fields.Date.context_today(self)
        if self.recurring_from and on_date < self.recurring_from:
            return 0.0
        if self.recurring_until and on_date > self.recurring_until:
            return 0.0
        effective = self.recurring_rate if rate is None else rate
        return round(period_amount * (effective or 0.0), 2)

    # -- lifecycle ---------------------------------------------------------

    def action_activate(self):
        for agreement in self:
            if agreement.state != "draft":
                continue
            agreement.state = "active"
            if not agreement.recurring_from:
                # The normal case: the recurring bonus starts after the first
                # subscription period, which is one year.
                agreement.recurring_from = agreement.brought_in_at + relativedelta(
                    years=1
                )
        return True

    def action_end(self, on_date=None):
        """Start the ramp. Six months, then zero -- never a cliff."""
        on_date = on_date or fields.Date.context_today(self)
        for agreement in self:
            if agreement.state not in ("active", "ending"):
                continue
            agreement.write({
                "state": "ending",
                "recurring_until": on_date + relativedelta(months=ENDING_MONTHS),
            })
        return True

    @api.model
    def _cron_finish_ending(self):
        """Move agreements whose ramp has run out to ``ended``.

        The commission already returns zero past ``recurring_until``, so this
        is housekeeping rather than the thing that stops the money: the
        computation is the guard, the state is the record.
        """
        today = fields.Date.context_today(self)
        due = self.search([
            ("state", "=", "ending"),
            ("recurring_until", "!=", False),
            ("recurring_until", "<", today),
        ])
        for agreement in due:
            agreement.write({"state": "ended", "ended_at": today})
        _logger.info("Ended %s agency agreements", len(due))
        return True

    def action_reopen(self):
        """A returning customer keeps their ``brought_in_at``.

        Reopening restores the recurring bonus at the tier that applies now --
        but never the signup commission, because ``brought_in_at`` is unchanged
        and the signup is tied to the start, not to the state.
        """
        for agreement in self:
            if agreement.state != "ended":
                continue
            agreement.write({
                "state": "active",
                "ended_at": False,
                "recurring_until": False,
            })
        return True
