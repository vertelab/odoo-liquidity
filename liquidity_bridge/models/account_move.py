# -*- coding: utf-8 -*-
"""The customer's own button: "send this to the firm".

Deliberately on the invoice and on the journal entry, because those are the two
things a customer actually has a question about. The button puts one line and
an optional message in the outbox; it never calls the firm directly, so a
customer on a bad connection still gets their question queued.
"""
import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    def action_send_to_agency(self):
        """Queue this document for the firm. Returns a notification, not an error."""
        self.ensure_one()
        config = self.env["liquidity.bridge.config"]
        if not config.is_enabled():
            raise UserError(
                _("The bridge is switched off here, so nothing can be sent to "
                  "the accounting firm. Ask your administrator.")
            )

        event = self.env["bridge.outbox"].enqueue(
            event_type="entry_flagged" if self.is_move_sent is not None
                       and self.move_type == "entry" else "invoice_question",
            summary=_("%(name)s — question from the customer") % {
                "name": self.name or self.display_name,
            },
            body=_("The customer flagged %(name)s for review.") % {
                "name": self.name or self.display_name,
            },
            res_model=self._name,
            res_id=self.id,
            res_reference=self.name or self.display_name,
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sent to the accounting firm"),
                "message": _(
                    "Your question about %(name)s is queued and will be "
                    "delivered to the firm. Reference: %(ref)s",
                    name=self.name or self.display_name,
                    ref=event.uuid[:8],
                ),
                "type": "success",
                "sticky": False,
            },
        }
