# -*- coding: utf-8 -*-
"""One consultant's account at one customer.

Step 1 had one login and one API key per customer, and a firm with eight
consultants shared them. That is the cheapest setup and therefore what a firm
actually does -- and it destroys the audit trail, because the customer can no
longer see *who* was in their books, only that someone was.

This model is the fix: one account row per consultant per customer, each with
its own credential. Two things follow, and they are the point:

* **The log names a person.** ``bridge.access.log`` points at this row, so the
  customer's answer to "who opened our ledger on Tuesday" is a name.
* **Revoking one consultant is one row.** Not a rotation of a shared key that
  nobody dares do because it breaks everyone.

An account is not a user at the customer -- that is the bridge's job (see
``liquidity_bridge``). This row only says which credential the portfolio uses.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class LiquidityTenantAccount(models.Model):
    """A consultant's credential against one source."""

    _name = "liquidity.tenant.account"
    _description = "Consultant account at a source"
    _order = "tenant_id, login"
    _rec_name = "display_name"

    tenant_id = fields.Many2one(
        "liquidity.tenant",
        string="Source",
        required=True,
        ondelete="cascade",
        index=True,
    )
    consultant_id = fields.Many2one(
        "res.users",
        string="Consultant",
        help="The consultant at the firm who uses this account. Empty means "
             "the account belongs to the firm as a whole -- which is what "
             "step 1 had, and what the log then cannot name a person for.",
    )
    consultant_ref = fields.Char(
        string="Consultant reference",
        help="The consultant's identifier at the firm, when it is not a local "
             "user. Written into the access log alongside the name.",
    )
    login = fields.Char(
        string="Login",
        required=True,
        help="The login at the source. For an Odoo source this is an Odoo "
             "user; for a driven source it is the OAuth subject, if any.",
    )
    keykeep_ref = fields.Char(
        string="Keykeep reference",
        required=True,
        help="Where this account's own credential lives. One credential per "
             "consultant is the whole point: revoking one person must not "
             "touch anyone else.",
    )
    is_agency_account = fields.Boolean(
        string="Agency account",
        default=True,
        help="True when this account exists only for the firm's access. The "
             "customer can count these separately from their own users.",
    )
    agency_partner_id = fields.Many2one(
        "res.partner",
        string="Agency",
        help="The firm this account belongs to. Set on the account rather than "
             "read from the source, because one customer may work with more "
             "than one firm over time.",
    )
    active = fields.Boolean(
        default=True,
        help="Untick to revoke. The row and its history are kept, so the "
             "access log still resolves to a name after the consultant left.",
    )
    last_used_at = fields.Datetime(
        string="Last used",
        readonly=True,
        help="Updated when the account is used to read. An account nobody has "
             "used in six months is a credential somebody should revoke.",
    )
    display_name = fields.Char(compute="_compute_display_name", store=True)

    _sql_constraints = [
        (
            "tenant_login_uniq",
            "unique(tenant_id, login)",
            "This login is already registered for this source.",
        ),
    ]

    @api.depends("tenant_id.name", "login", "consultant_id.name")
    def _compute_display_name(self):
        for account in self:
            who = account.consultant_id.name or account.login
            account.display_name = "%s / %s" % (
                account.tenant_id.name or "", who or "",
            )

    @api.constrains("tenant_id", "keykeep_ref")
    def _check_one_credential_per_account(self):
        """Two accounts for the same login would be two people sharing a key.

        That is the situation this model exists to remove, so it is refused
        rather than tolerated.
        """
        for account in self:
            duplicates = self.search([
                ("id", "!=", account.id),
                ("tenant_id", "=", account.tenant_id.id),
                ("keykeep_ref", "=", account.keykeep_ref),
            ])
            if duplicates:
                raise ValidationError(
                    _("Two accounts for %(tenant)s share the credential "
                      "%(ref)s. Each consultant needs their own.",
                      tenant=account.tenant_id.name, ref=account.keykeep_ref)
                )

    def action_revoke(self):
        """Retire the account. Kept, not deleted: the log must still resolve."""
        self.write({"active": False})
        return True
