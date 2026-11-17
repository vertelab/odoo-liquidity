# -*- coding: utf-8 -*-
"""The commission basis: what is owed, and why.

An agreement says what a rate is. The basis says what that rate produced for a
period, and — this is the part that makes it usable — *why*. A report that only
shows a number cannot be checked by the person who has to approve it, and an
unchecked commission is a commission that quietly grows.

Deliberately not an invoicing engine. It produces a table a human reads and an
accountant books; it does not create invoices or payments. Same principle as
`liquidity_work`: basis, not a billing motor.
"""
import logging
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class LiquidityAgencyCommissionBasis(models.Model):
    """A computed row in the basis: one agreement, one period, one figure."""

    _name = "liquidity.agency.commission.basis"
    _description = "Agency commission basis"
    _order = "period_start desc, agency_partner_id"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name")
    agency_partner_id = fields.Many2one("res.partner", string="Agency", readonly=True)
    tenant_id = fields.Many2one("liquidity.tenant", string="Customer", readonly=True)
    agreement_id = fields.Many2one(
        "liquidity.agency.agreement", string="Agreement", readonly=True
    )
    period_start = fields.Date(string="Period from", readonly=True)
    period_end = fields.Date(string="Period to", readonly=True)
    source = fields.Selection(
        selection=[("vertel", "Vertel's"), ("agency", "Agency")],
        string="Customer of",
        readonly=True,
    )
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("active", "Active"),
            ("ending", "Ending"),
            ("ended", "Ended"),
        ],
        string="Agreement status",
        readonly=True,
    )
    base_amount = fields.Monetary(
        string="Period amount",
        currency_field="currency_id",
        readonly=True,
        help="What the customer paid for the period. Entered by the caller: "
             "the commission module does not read the subscription ledger, "
             "because it does not own it.",
    )
    signup_amount = fields.Monetary(
        string="Signup commission", currency_field="currency_id", readonly=True
    )
    recurring_amount = fields.Monetary(
        string="Recurring commission", currency_field="currency_id", readonly=True
    )
    total_amount = fields.Monetary(
        string="Total", currency_field="currency_id", readonly=True
    )
    currency_id = fields.Many2one(
        "res.currency", readonly=True,
        default=lambda self: self.env.company.currency_id,
    )
    reason = fields.Char(
        string="Basis",
        readonly=True,
        help="Why the figure is what it is. '30 % of 12 000' is checkable; a "
             "bare 3 600 is not.",
    )

    @api.depends("agency_partner_id", "tenant_id", "period_start")
    def _compute_display_name(self):
        for row in self:
            row.display_name = "%s / %s / %s" % (
                row.agency_partner_id.name or "",
                row.tenant_id.name or "",
                row.period_start or "",
            )


class LiquidityAgencyAgreementBasisWizard(models.TransientModel):
    """Build the basis for a period, from the caller's own amounts.

    The caller supplies one amount per customer for the period. That is the
    honest split: this module knows the *rules*, and the caller knows what the
    customer actually paid. Reading the subscription ledger here would make
    this module own a second copy of that truth.
    """

    _name = "liquidity.agency.basis.wizard"
    _description = "Build agency commission basis"

    period_start = fields.Date(
        string="Period from", required=True,
        default=lambda self: date.today().replace(day=1) - relativedelta(months=1),
    )
    period_end = fields.Date(
        string="Period to", required=True,
        default=lambda self: date.today().replace(day=1) - relativedelta(days=1),
    )
    agency_partner_id = fields.Many2one(
        "res.partner", string="Agency",
        help="Leave empty for every agency.",
    )
    base_amount = fields.Monetary(
        string="Amount per customer",
        currency_field="currency_id",
        help="What each customer paid for the period. Used for every agreement "
             "in scope, so the report is a comparison rather than a ledger.",
    )
    currency_id = fields.Many2one(
        "res.currency",
        default=lambda self: self.env.company.currency_id,
    )

    def action_build(self):
        """Compute the basis and show it. Nothing is written."""
        self.ensure_one()
        domain = []
        if self.agency_partner_id:
            domain.append(("agency_partner_id", "=", self.agency_partner_id.id))
        agreements = self.env["liquidity.agency.agreement"].search(domain)

        rows = agreements._build_basis(
            self.period_start, self.period_end, self.base_amount or 0.0
        )
        # The rows are real records in a non-stored model, created so the view
        # can show them; they disappear with the cursor, which is correct for a
        # report.
        records = self.env["liquidity.agency.commission.basis"].create(rows)
        return {
            "type": "ir.actions.act_window",
            "name": _("Commission basis %(start)s – %(end)s", start=self.period_start,
                      end=self.period_end),
            "res_model": "liquidity.agency.commission.basis",
            "view_mode": "list",
            "domain": [("id", "in", records.ids)],
            "target": "current",
        }


class LiquidityAgencyAgreementBasis(models.Model):
    _inherit = "liquidity.agency.agreement"

    def _build_basis(self, period_start, period_end, base_amount):
        """One row per agreement, with the arithmetic and its reason.

        The reason string is the point. A reader who can see "30 % of 12 000"
        can disagree with it; a reader who sees 3 600 cannot.
        """
        rows = []
        for agreement in self:
            earning = agreement._is_earning()
            signup = agreement._compute_signup_commission(base_amount) \
                if earning else 0.0
            recurring = agreement._compute_recurring_commission(
                base_amount, on_date=period_end
            ) if earning else 0.0

            reason = agreement._basis_reason(
                base_amount, signup, recurring, period_end
            )
            rows.append({
                "agreement_id": agreement.id,
                "agency_partner_id": agreement.agency_partner_id.id,
                "tenant_id": agreement.tenant_id.id,
                "source": agreement.source,
                "state": agreement.state,
                "period_start": period_start,
                "period_end": period_end,
                "base_amount": base_amount,
                "signup_amount": signup,
                "recurring_amount": recurring,
                "total_amount": signup + recurring,
                "reason": reason,
            })
        return rows

    def _basis_reason(self, base_amount, signup, recurring, on_date):
        """Plain words for why the row says what it says."""
        self.ensure_one()
        if self.source != "agency":
            return _("Vertel's customer — no commission, by rule")
        if self.state not in ("active", "ending"):
            return _("Agreement is %(state)s — earns nothing", state=self.state)
        parts = []
        if signup:
            parts.append(_("signup %(rate)s %% of %(base)s",
                           rate=round((self.signup_rate or 0.0) * 100, 1),
                           base=base_amount))
        if recurring:
            parts.append(_("recurring %(rate)s %% of %(base)s",
                           rate=round((self.recurring_rate or 0.0) * 100, 1),
                           base=base_amount))
        if not parts:
            if self.recurring_until and on_date > self.recurring_until:
                return _("The ramp ended %(date)s — no recurring commission",
                         date=self.recurring_until)
            if self.recurring_from and on_date < self.recurring_from:
                return _("Signup already paid; recurring starts %(date)s",
                         date=self.recurring_from)
            return _("Nothing earned in this period")
        return "; ".join(parts)
