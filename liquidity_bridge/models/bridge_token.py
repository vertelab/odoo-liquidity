# -*- coding: utf-8 -*-
"""Impersonation tokens and the access log.

The token is the only thing in this module that can turn a request into a
logged-in session, so it is built to be useless as a credential:

* **Single use.** ``used_at`` is set before the session is created, inside the
  same transaction. Redeeming twice finds ``used_at`` set and refuses.
* **Short lived.** Sixty seconds by default. The browser redeems it within
  seconds of the redirect; anything longer is a window somebody can steal.
* **Bound.** One user, one company, one issuing agency. A token for company A
  cannot open company B.
* **No data in itself.** It carries no permissions, only a reference. The
  rights come from the user it points at, which is the point: the firm opens
  *its own* account, never someone else's.

The access log is written **before** the session exists, in the same
transaction. If the log write fails, the session is not created -- "no session
without a log line" is a rule, not an intention.
"""
import logging
import secrets

from odoo import api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class BridgeToken(models.Model):
    """A single-use, short-lived ticket to open a session."""

    _name = "bridge.token"
    _description = "Impersonation token"
    _order = "create_date desc, id desc"

    token = fields.Char(
        string="Token",
        required=True,
        readonly=True,
        index=True,
        help="Random, single-use. Never derived from a user id or a password.",
    )
    user_id = fields.Many2one(
        "res.users",
        string="Open as",
        required=True,
        ondelete="cascade",
        help="The agency's own account at this customer. Not a customer user: "
             "opening someone else's account is a different function with a "
             "different security profile.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        ondelete="cascade",
        help="The company the session is scoped to. A token for one company "
             "cannot open another.",
    )
    issued_by = fields.Char(
        string="Issued by",
        required=True,
        help="The agency name from the configuration at issue time. Copied, "
             "not read live, so the log still says who it was if the "
             "configuration changes later.",
    )
    expires_at = fields.Datetime(string="Expires", required=True, index=True)
    used_at = fields.Datetime(string="Used", readonly=True)
    menu_xmlid = fields.Char(
        string="Menu",
        help="Deep link target as an xmlid, resolved at the customer. Empty "
             "opens the home screen.",
    )

    @api.model
    def issue(self, user, company, menu_xmlid=None):
        """Create a token. Returns the record; the caller redirects with it."""
        config = self.env["liquidity.bridge.config"]
        if not config.allows_impersonation():
            raise AccessError(
                "Impersonation is switched off at this customer."
            )
        ttl = config.token_ttl()
        from datetime import timedelta
        return self.sudo().create({
            "token": secrets.token_urlsafe(32),
            "user_id": user.id,
            "company_id": company.id,
            "issued_by": config.agency_name() or "unknown agency",
            "expires_at": fields.Datetime.now() + timedelta(seconds=ttl),
            "menu_xmlid": menu_xmlid or False,
        })

    def _is_valid(self):
        """Usable: not used, not expired."""
        self.ensure_one()
        if self.used_at:
            return False
        return self.expires_at > fields.Datetime.now()

    @api.model
    def _cron_purge(self):
        """Delete expired tokens. A token that cannot be used is only a liability."""
        expired = self.sudo().search([
            ("expires_at", "<", fields.Datetime.now()),
        ])
        expired.unlink()
        return True


class BridgeAccessLog(models.Model):
    """Who from the agency opened what, when, and how it went.

    The device log Odoo already keeps shows an IP address and a browser. It
    does not answer the question a customer actually asks: *who from the firm
    was in our books on Tuesday?* This does.
    """

    _name = "bridge.access.log"
    _description = "Access from the accounting firm"
    _order = "opened_at desc, id desc"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    agency = fields.Char(string="Agency", required=True, index=True)
    user_id = fields.Many2one(
        "res.users",
        string="Opened as",
        required=True,
        ondelete="restrict",
        index=True,
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        ondelete="restrict",
    )
    opened_at = fields.Datetime(
        string="Opened",
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    outcome = fields.Selection(
        selection=[
            ("granted", "Session opened"),
            ("refused", "Refused"),
        ],
        string="Outcome",
        required=True,
        default="granted",
    )
    reason = fields.Char(
        string="Reason",
        help="Why it was refused, when it was. Empty for a granted session.",
    )
    menu_xmlid = fields.Char(string="Menu")
    ip_address = fields.Char(string="IP address")

    @api.depends("agency", "user_id", "opened_at", "outcome")
    def _compute_display_name(self):
        for row in self:
            row.display_name = "%s / %s / %s" % (
                row.agency or "",
                row.user_id.name or "",
                fields.Datetime.to_string(row.opened_at) if row.opened_at else "",
            )

    @api.model
    def log(self, agency, user, company, outcome, reason=None, menu_xmlid=None,
            ip_address=None):
        """Write one row. The caller must treat a failure here as fatal."""
        return self.sudo().create({
            "agency": agency or "unknown agency",
            "user_id": user.id,
            "company_id": company.id,
            "outcome": outcome,
            "reason": reason,
            "menu_xmlid": menu_xmlid,
            "ip_address": ip_address,
        })
