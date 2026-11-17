# -*- coding: utf-8 -*-
"""What the customer sent, received at the firm.

The bridge's queue carries events out of the customer's Odoo. This is the other
end: a small endpoint at the firm that stores them, matches them to a
bookkeeping row, and — where it can — turns them into a work order.

Two rules, and both come from the design's "what must never happen":

**An unmatched event is not silenced.** An event from a customer the firm has
not imported, or from a company that no longer exists, goes into a visible
"unmatched" list. Dropping it would look identical to the customer never having
sent anything, which is the worst possible failure for a feature whose purpose
is to replace a phone call.

**Delivery is idempotent by UUID.** The customer retries when it is unsure a
delivery landed. A retry must not create a second work order.

The endpoint is not part of `liquidity_bridge` — that module is installed at the
customer. This one runs at the firm, and the direction of trust is the opposite:
the customer's event arrives here with the bridge key the firm issued.
"""
import json
import logging

from odoo import _, api, fields, models
from odoo.http import request

_logger = logging.getLogger(__name__)


class LiquidityBridgeMessage(models.Model):
    """The firm's side of the mirrored channel.

    Same shape as ``bridge.message`` at the customer, deliberately duplicated
    rather than shared: the two installations are independent, and a shared
    model would make one module depend on the other. The UUID is what makes
    them the same message.
    """

    _name = "liquidity.bridge.message"
    _description = "Mirrored message with a customer"
    _order = "sent_at asc, id asc"
    _rec_name = "summary"

    uuid = fields.Char(string="Message id", required=True, readonly=True, index=True)
    thread_key = fields.Char(string="Thread", required=True, readonly=True, index=True)
    summary = fields.Char(string="Summary", readonly=True)
    body = fields.Text(string="Message", required=True, readonly=True)
    author_kind = fields.Selection(
        selection=[("customer", "Customer"), ("agency", "Accounting firm")],
        string="From",
        required=True,
        readonly=True,
    )
    author_name = fields.Char(string="Written by", readonly=True)
    sent_at = fields.Datetime(string="Sent", required=True, readonly=True)
    tenant_id = fields.Many2one("liquidity.tenant", string="Source", readonly=True)
    res_model = fields.Char(readonly=True)
    res_id = fields.Integer(readonly=True)
    res_reference = fields.Char(string="Related record", readonly=True)

    _sql_constraints = [
        ("liquidity_bridge_message_uuid_uniq", "unique(uuid)",
         "This message has already been received."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("summary"):
                text = (vals.get("body") or "").strip().splitlines()
                vals["summary"] = (text[0] if text else "")[:120]
        return super().create(vals_list)

    def write(self, vals):
        """Immutable, for the same reason as at the customer: an edit brings
        back the question of which version is true."""
        from odoo.exceptions import UserError
        protected = {"body", "uuid", "author_kind", "author_name", "sent_at",
                     "thread_key"}
        if protected & set(vals):
            raise UserError(
                _("A message cannot be changed once it is sent. Write a new "
                  "one instead — both parties must keep the same history.")
            )
        return super().write(vals)


class LiquidityIncomingEvent(models.Model):
    """One event received from a customer's bridge."""

    _name = "liquidity.incoming.event"
    _description = "Event received from a customer's bridge"
    _order = "received_at desc, id desc"
    _rec_name = "summary"

    uuid = fields.Char(
        string="Event id",
        required=True,
        readonly=True,
        index=True,
        help="The customer's id for the event. A retry with the same id is "
             "acknowledged, not stored twice.",
    )
    event_type = fields.Char(string="Type", required=True, readonly=True)
    summary = fields.Char(string="Summary", required=True, readonly=True)
    body = fields.Text(string="Message", readonly=True)
    tenant_id = fields.Many2one(
        "liquidity.tenant",
        string="Source",
        readonly=True,
        index=True,
        help="Matched from the bridge key that delivered the event. Empty "
             "means the key is not registered -- the event is then unmatched "
             "rather than discarded.",
    )
    tenant_company_id = fields.Many2one(
        "liquidity.tenant.company",
        string="Bookkeeping",
        readonly=True,
        index=True,
        help="Matched on the customer's company reference. Empty means the "
             "firm has not imported that company yet.",
    )
    company_ref = fields.Integer(string="Company reference", readonly=True)
    res_reference = fields.Char(string="Related record", readonly=True)
    sent_by = fields.Char(string="Sent by", readonly=True)
    sent_at = fields.Datetime(string="Sent", readonly=True)
    received_at = fields.Datetime(
        string="Received", default=fields.Datetime.now, required=True,
        readonly=True,
    )
    state = fields.Selection(
        selection=[
            ("matched", "Matched"),
            ("unmatched", "Unmatched"),
            ("ignored", "Ignored"),
        ],
        string="Status",
        default="unmatched",
        required=True,
        index=True,
        help="Unmatched is a visible state, not a failure. An event the firm "
             "cannot place is one a human has to look at -- silencing it would "
             "look exactly like the customer never writing.",
    )
    match_note = fields.Char(
        string="Why unmatched",
        readonly=True,
        help="What was missing: an unknown source, or a company the firm has "
             "not imported.",
    )
    # Not a required outcome. A work order belongs to a plan the firm has sold
    # to that customer, and an incoming question does not imply one exists.
    # The reference is filled when a plan is there, and left empty otherwise --
    # an event is not made to wait for a commercial relationship.
    work_order_id = fields.Many2one(
        "liquidity.work.order",
        string="Work order",
        readonly=True,
        ondelete="set null",
        help="Set when the bookkeeping has a plan the event could become work "
             "on. Empty is normal: a question is not a sold service.",
    )

    _sql_constraints = [
        (
            "incoming_event_uuid_uniq",
            "unique(uuid)",
            "This event has already been received.",
        ),
    ]

    @api.model
    def receive(self, payload, tenant=None):
        """Store one event. Returns (record, created).

        ``created`` is False when the uuid was already stored, which the
        endpoint answers with 200 so the customer stops retrying.
        """
        event_uuid = payload.get("uuid")
        existing = self.search([("uuid", "=", event_uuid)], limit=1)
        if existing:
            return existing, False

        company_ref = payload.get("company_ref") or 0
        company = self.env["liquidity.tenant.company"]
        note = False
        if tenant:
            company = tenant.company_ids.filtered(
                lambda c: c.company_ref == company_ref
            )[:1]
            if not company:
                note = _("The firm has not imported company %s from this "
                         "source.", company_ref)
        else:
            note = _("The bridge key is not registered at any source.")

        event = self.create({
            "uuid": event_uuid,
            "event_type": payload.get("event_type") or "message",
            "summary": payload.get("summary") or "",
            "body": payload.get("body") or "",
            "tenant_id": tenant.id if tenant else False,
            "tenant_company_id": company.id if company else False,
            "company_ref": company_ref,
            "res_reference": payload.get("res_reference") or False,
            "sent_by": payload.get("sent_by") or False,
            "sent_at": payload.get("sent_at") or False,
            "state": "matched" if company else "unmatched",
            "match_note": note,
        })
        return event, True

    def action_ignore(self):
        self.write({"state": "ignored"})
        return True

    def action_rematch(self):
        """Try again after the firm has imported the missing company."""
        for event in self:
            if event.state != "unmatched" or not event.tenant_id:
                continue
            company = event.tenant_id.company_ids.filtered(
                lambda c: c.company_ref == event.company_ref
            )[:1]
            if company:
                event.write({
                    "tenant_company_id": company.id,
                    "state": "matched",
                    "match_note": False,
                })
        return True
