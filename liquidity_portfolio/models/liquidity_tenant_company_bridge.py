# -*- coding: utf-8 -*-
"""Opening the customer's system as the consultant who clicked.

Step 1 offered a login link: a plain bookmark, because a browser cannot log in
with an API key. That is honest but it still means typing a password, and the
customer's access log then shows "someone from the firm".

This adds the other half. The firm asks the customer's bridge for a token, and
the browser redeems it. Three properties matter, and all three are the bridge's
doing, not ours:

* the token is single-use and lives for seconds,
* it is bound to one consultant's account and one company,
* it is written into the customer's own access log **before** the session
  exists.

What this module must not do is pretend. If the customer has no bridge, the
button is not shown and the login link from step 1 remains -- a button that
fails is worse than no button.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class LiquidityTenantCompany(models.Model):
    _inherit = "liquidity.tenant.company"

    bridge_available = fields.Boolean(
        string="Bridge at the customer",
        compute="_compute_bridge_available",
        store=False,
        help="True when the customer's Odoo answers as a bridge and allows "
             "the firm to open a session. False keeps the plain login link "
             "from step 1 -- a button that cannot work is worse than none.",
    )
    bridge_state = fields.Char(
        string="Bridge status",
        compute="_compute_bridge_available",
        store=False,
        help="Why the bridge is or is not usable: missing, switched off, "
             "impersonation refused, or key refused. The reader needs the "
             "reason, not just the absence of a button.",
    )

    def _compute_bridge_available(self):
        for row in self:
            if row.tenant_id.source_type != "odoo":
                # A driven source has no Odoo to open.
                row.bridge_available = False
                row.bridge_state = "driven source — nothing to open"
                continue
            status = row.tenant_id._bridge_status()
            row.bridge_available = bool(status.get("available"))
            row.bridge_state = status.get("reason") or ""

    def action_open_at_customer(self):
        """Ask the customer's bridge for a token and send the browser there."""
        self.ensure_one()
        if self.tenant_id.source_type != "odoo":
            raise UserError(
                _("This source is not an Odoo instance, so there is nothing "
                  "to open. The figures are derived here.")
            )

        status = self.tenant_id._bridge_status()
        if not status.get("available"):
            raise UserError(
                _("The customer's bridge cannot open a session: %(reason)s. "
                  "Use the login link instead.",
                  reason=status.get("reason") or _("unknown reason"))
            )

        result = self.tenant_id._request_bridge_session(
            company_ref=self.company_ref,
        )
        if not result.get("url"):
            raise UserError(
                _("The bridge refused the session: %(reason)s",
                  reason=result.get("reason") or _("no reason given"))
            )
        # A redirect, not a new window: the token is in the URL and a token in
        # a second tab is a token in two histories.
        return {
            "type": "ir.actions.act_url",
            "url": result["url"],
            "target": "self",
        }
