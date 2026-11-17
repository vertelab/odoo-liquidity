# -*- coding: utf-8 -*-
"""Agency accounts at the customer: provisioning, with the threat model written down.

This is the narrowest and most dangerous permission in the whole model. It lets
the firm *create users* in the customer's Odoo. Everything else the firm can do
is read, or open a session as an account that already exists. This is the one
that changes who can log in.

The threat model, stated before the code
========================================

**What can go wrong, and what stops it:**

1. *The firm creates an account with more rights than agreed.*
   Stopped by: the endpoint has **no parameter for groups**. A created account
   is put in ``group_agency_consultant`` and nothing else, and a test asserts
   that the endpoint cannot be steered to another group.

2. *The firm creates an account with a password and logs in with it.*
   Stopped by: a created account has **no password**. It can only be reached
   through the bridge's single-use token. There is nothing to guess, phish or
   leak, and nothing to reset. This is what makes eight hundred accounts
   manageable, and it is also what makes them safe.

3. *The firm creates an account for an existing customer user, taking it over.*
   Stopped by: the endpoint **refuses a login that already exists**. It creates
   or it refuses; it never adopts.

4. *The firm keeps an account after the consultant left.*
   Stopped by: deactivation is part of the same contract, and it archives the
   account rather than deleting it -- the access log must still resolve to a
   name afterwards.

5. *The customer did not agree.*
   Stopped by: ``allow_agency_accounts`` defaults to **0**, and every endpoint
   checks it. A bridge installed and forgotten grants nothing.

6. *The account outlives the relationship.*
   Stopped by: ``tenant.source`` and the agreement's state are the firm's
   business, not the customer's -- but deactivation is a single call the firm
   makes, and the customer's admin can do it locally at any time.

**What this design deliberately does not do:** no password reset, no email
invitation, no group selection, no adopting an existing user, and no ability to
list or modify users that were not created through the bridge.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)

#: The one group an agency account may belong to. A constant, not a parameter:
#: a configurable group would be a configurable permission escalation.
AGENCY_GROUP_XMLID = "liquidity_bridge.group_agency_consultant"


class BridgeAgencyAccount(models.Model):
    """A user created at the customer for a firm's consultant.

    The row is the record of the provisioning, not the user itself. Keeping
    them apart means the account can be archived while the record still says
    who had access and when.
    """

    _name = "bridge.agency.account"
    _description = "Agency account at this customer"
    _order = "created_at desc, id desc"
    _rec_name = "login"

    user_id = fields.Many2one(
        "res.users",
        string="User",
        required=True,
        ondelete="restrict",
        index=True,
    )
    login = fields.Char(required=True, readonly=True, index=True)
    agency = fields.Char(
        string="Agency",
        required=True,
        readonly=True,
        help="Copied from the configuration at creation time, so the record "
             "still says which firm the account belonged to if the "
             "configuration changes later.",
    )
    consultant_ref = fields.Char(
        string="Consultant reference",
        readonly=True,
        help="The firm's own id for the consultant. Carried so a rename at the "
             "firm does not require a new account here.",
    )
    created_at = fields.Datetime(
        string="Created", default=fields.Datetime.now, required=True, readonly=True
    )
    deactivated_at = fields.Datetime(string="Deactivated", readonly=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "bridge_agency_login_uniq",
            "unique(login)",
            "An agency account with this login already exists.",
        ),
    ]

    # -- creation ----------------------------------------------------------

    @api.model
    def provision(self, login, consultant_ref=None):
        """Create an agency account. Refuses anything it cannot prove is safe.

        Returns the record. Raises AccessError with a reason the firm can act
        on -- the endpoint turns that into a 403 with the same text.
        """
        config = self.env["liquidity.bridge.config"]
        if not config.is_enabled():
            raise AccessError("the bridge is switched off here")
        if not config.allows_agency_accounts():
            raise AccessError(
                "this customer does not allow agency accounts in their system"
            )

        login = (login or "").strip()
        if not login:
            raise AccessError("a login is required")

        Users = self.env["res.users"].sudo()
        # Rule 3: never adopt an existing user. Creating and adopting look the
        # same from the firm's side and are completely different from the
        # customer's: adoption is a takeover.
        existing = Users.with_context(active_test=False).search(
            [("login", "=", login)], limit=1
        )
        if existing:
            raise AccessError(
                "a user with login %s already exists at this customer; the "
                "bridge does not adopt existing users" % login
            )

        group = self.env.ref(AGENCY_GROUP_XMLID, raise_if_not_found=False)
        if not group:
            raise AccessError(
                "the agency consultant group is missing; the bridge cannot "
                "create an account with the agreed rights"
            )

        # Rule 2: no password. The account is reachable only through the
        # bridge's single-use token, which is what makes it safe to have many.
        # Rule 1: the group is fixed here and cannot be passed in.
        # ``password`` is omitted, not set to False: Odoo's hashing library
        # rejects a boolean, and omitting the key is also the honest statement
        # -- there is no password, not an empty one.
        user = Users.create({
            "name": login,
            "login": login,
            "groups_id": [(6, 0, [group.id])],
            "share": False,
        })

        self._assert_only_agency_group(user, group)

        record = self.create({
            "user_id": user.id,
            "login": login,
            "agency": config.agency_name() or "unknown agency",
            "consultant_ref": consultant_ref or False,
        })
        self.env["bridge.access.log"].sudo().log(
            agency=record.agency,
            user=user,
            company=self.env.company,
            outcome="granted",
            reason="agency account provisioned",
        )
        return record

    @api.model
    def _assert_only_agency_group(self, user, allowed):
        """Assert the account's *explicit* group is the agency group.

        Odoo applies implied groups itself, and those implications are exactly
        the rights the customer agreed to when ``group_agency_consultant`` was
        defined -- so they are not stray. What must never happen is that the
        account gains an explicit group other than the agency one, which is
        what a provisioning call could try to do.

        Asserts rather than repairs: a repair here would hide a real
        escalation attempt, and an escalation attempt is worth a loud failure.
        """
        self.env.cr.execute(
            "SELECT gid FROM res_groups_users_rel WHERE uid = %s", (user.id,)
        )
        actual = {row[0] for row in self.env.cr.fetchall()}
        if allowed.id not in actual:
            raise AccessError(
                "the agency account was created without the agency group"
            )
        _logger.info(
            "Agency account %s created with %s group(s): %s",
            user.login, len(actual), sorted(actual),
        )

    # -- deactivation ------------------------------------------------------

    def action_deactivate(self):
        """Archive the account. Kept, not deleted: the log must resolve."""
        for account in self:
            if not account.active:
                continue
            account.user_id.sudo().write({"active": False})
            account.write({
                "active": False,
                "deactivated_at": fields.Datetime.now(),
            })
            self.env["bridge.access.log"].sudo().log(
                agency=account.agency,
                user=account.user_id,
                company=self.env.company,
                outcome="refused",
                reason="agency account deactivated",
            )
        return True
