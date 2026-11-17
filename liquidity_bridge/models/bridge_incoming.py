# -*- coding: utf-8 -*-
"""Messages coming *from* the firm.

The outbox carries the customer's questions out. This carries the firm's
answers back in, so the customer sees the reply where they asked -- in their
own Odoo, next to the entry -- instead of in an email nobody can find later.

Idempotent by UUID for the same reason as the outbox: the firm retries when it
is not sure a delivery landed, and a retry must not duplicate the message.
"""
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class BridgeIncoming(models.Model):
    """One message from the accounting firm."""

    _name = "bridge.incoming"
    _description = "Message from the accounting firm"
    _order = "received_at desc, id desc"
    _rec_name = "summary"

    uuid = fields.Char(
        string="Message id",
        required=True,
        readonly=True,
        index=True,
        help="The firm's id for this message. A retry with the same id is "
             "acknowledged, not stored twice.",
    )
    event_type = fields.Char(string="Type", required=True)
    summary = fields.Char(string="Summary", required=True)
    body = fields.Text(string="Message")
    agency = fields.Char(
        string="From",
        required=True,
        help="Copied from the configuration at receipt time, so the record "
             "still says who sent it if the configuration changes.",
    )
    res_model = fields.Char(string="Related model")
    res_id = fields.Integer(string="Related record")
    received_at = fields.Datetime(
        string="Received",
        default=fields.Datetime.now,
        required=True,
        readonly=True,
    )
    is_read = fields.Boolean(string="Read", default=False)

    _sql_constraints = [
        (
            "bridge_incoming_uuid_uniq",
            "unique(uuid)",
            "A message with this id has already been received.",
        ),
    ]

    def action_mark_read(self):
        self.write({"is_read": True})
        return True
