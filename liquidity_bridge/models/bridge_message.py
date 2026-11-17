# -*- coding: utf-8 -*-
"""The mirrored channel: a conversation that exists at both parties.

The design chose ALT B — every side is the original for its own messages — and
that choice removes a whole class of problems. There is no "authoritative
party" to pick, no conflict to resolve, and a party that is temporarily down
becomes a *delivery* problem instead of a *data loss* problem.

Two rules follow from it, and both are enforced here rather than documented:

**A message is an addition, never an update.** Once written it cannot be
edited or deleted. The moment editing is allowed, authority comes back: which
version is the true one, and who decides? Immutability is what makes the model
robust, so the fields are readonly and ``write()`` refuses.

**Identity is a UUID, not a row id.** The same message exists as two rows in
two databases, and the UUID is what makes delivering it twice harmless.

Deliberately kept apart from step 1's local chatter thread. Step 1's thread is
the firm's own notes, invisible to the customer; this is a channel both parties
read. Putting them in the same field would leak the notes to the customer on
the first sync, which the design names as the one thing that must never happen.
"""
import logging
import uuid as uuid_module

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BridgeMessage(models.Model):
    """One immutable message in the mirrored channel."""

    _name = "bridge.message"
    _description = "Mirrored message between customer and firm"
    _order = "sent_at asc, id asc"
    _rec_name = "summary"

    uuid = fields.Char(
        string="Message id",
        required=True,
        readonly=True,
        index=True,
        default=lambda self: str(uuid_module.uuid4()),
        help="Stable across both databases. Delivering the same message twice "
             "is then harmless, which is what makes a retry safe.",
    )
    thread_key = fields.Char(
        string="Thread",
        required=True,
        readonly=True,
        index=True,
        help="Groups messages into one conversation. Derived from the record "
             "the conversation is about (an invoice, an entry) rather than "
             "from a separate thread table: a thread is a view of messages, "
             "not an object with its own lifecycle.",
    )
    body = fields.Text(string="Message", required=True, readonly=True)
    summary = fields.Char(
        string="Summary",
        readonly=True,
        help="First line, for the list view. Set in create() rather than by a "
             "compute: it is derived once from an immutable body, and a "
             "compute over a field that can never change is machinery with no "
             "purpose.",
    )
    author_kind = fields.Selection(
        selection=[
            ("customer", "Customer"),
            ("agency", "Accounting firm"),
        ],
        string="From",
        required=True,
        readonly=True,
        default="customer",
        help="Which side wrote it. Each side is the original for its own "
             "messages, so this field is the only thing that decides whose "
             "message it is.",
    )
    author_name = fields.Char(
        string="Written by",
        readonly=True,
        help="Copied at write time. A message must still say who wrote it "
             "after that person's account is gone.",
    )
    sent_at = fields.Datetime(
        string="Sent",
        required=True,
        readonly=True,
        default=fields.Datetime.now,
        help="When the author sent it. Ordering uses this, not the row id: "
             "the two databases will not agree on ids.",
    )
    res_model = fields.Char(string="Related model", readonly=True)
    res_id = fields.Integer(string="Related record", readonly=True)
    res_reference = fields.Char(
        string="Related record", readonly=True,
        help="Readable reference so the firm can find the record without a "
             "database id.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        readonly=True,
        default=lambda self: self.env.company,
        index=True,
    )

    _sql_constraints = [
        (
            "bridge_message_uuid_uniq",
            "unique(uuid)",
            "This message has already been received.",
        ),
    ]

    @staticmethod
    def thread_key_for(res_model, res_id):
        """The thread a record's conversation belongs to.

        A function rather than a stored field on the record: a thread is a
        grouping, not an object, and giving it a table would mean maintaining
        a lifecycle for something that has none.
        """
        return "%s:%s" % (res_model or "", res_id or 0)

    @api.model_create_multi
    def create(self, vals_list):
        """Fill the summary from the body, once.

        The body is immutable, so the summary never needs recomputing -- which
        is why this is a create-time derivation and not a compute.
        """
        for vals in vals_list:
            if not vals.get("summary"):
                text = (vals.get("body") or "").strip().splitlines()
                vals["summary"] = (text[0] if text else "")[:120]
        return super().create(vals_list)

    def _compute_summary(self):
        """Kept for callers that read the field on an unsaved record."""
        for message in self:
            text = (message.body or "").strip().splitlines()
            message.summary = (text[0] if text else "")[:120]

    def write(self, vals):
        """Refuse edits. A message is an addition, and that is the whole model.

        Allowing an edit brings back the question ALT B exists to avoid: which
        version is the true one, and at which side? If editing is ever wanted,
        it has to be built as a new message that supersedes the old one, so
        both parties still hold the same history.
        """
        protected = {
            "body", "uuid", "author_kind", "author_name", "sent_at",
            "thread_key", "res_model", "res_id", "company_id",
        }
        if protected & set(vals):
            raise UserError(
                _("A message cannot be changed once it is sent. Write a new "
                  "one instead — both parties must keep the same history.")
            )
        return super().write(vals)

    def action_reply(self):
        """Open a composer for the same thread."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Reply"),
            "res_model": "bridge.message.compose",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_thread_key": self.thread_key,
                "default_res_model": self.res_model,
                "default_res_id": self.res_id,
                "default_res_reference": self.res_reference,
                "default_company_id": self.company_id.id,
            },
        }


class BridgeMessageCompose(models.TransientModel):
    """Write one message. Separate from the message model because writing is
    the only thing a user may do, and a form on the immutable model invites
    editing it."""

    _name = "bridge.message.compose"
    _description = "Write a message"

    thread_key = fields.Char(required=True)
    body = fields.Text(string="Message", required=True)
    res_model = fields.Char()
    res_id = fields.Integer()
    res_reference = fields.Char()
    company_id = fields.Many2one("res.company", required=True)
    author_kind = fields.Selection(
        selection=[("customer", "Customer"), ("agency", "Accounting firm")],
        default="customer",
        required=True,
    )

    def action_send(self):
        """Create the message and queue it for delivery."""
        self.ensure_one()
        message = self.env["bridge.message"].create({
            "thread_key": self.thread_key,
            "body": self.body,
            "res_model": self.res_model,
            "res_id": self.res_id,
            "res_reference": self.res_reference,
            "company_id": self.company_id.id,
            "author_kind": self.author_kind,
            "author_name": self.env.user.name,
        })
        # Read the first line directly rather than through the stored compute:
        # within the same transaction the computed value may not be flushed
        # yet, and an unflushed summary is NULL -- which the outbox refuses.
        first_line = (self.body or "").strip().splitlines()
        self.env["bridge.outbox"].enqueue(
            event_type="chat_message",
            summary=(first_line[0] if first_line else "")[:120],
            body=message.body,
            res_model=self.res_model,
            res_id=self.res_id,
            res_reference=self.res_reference,
            extra={"message_uuid": message.uuid,
                   "thread_key": self.thread_key},
        )
        return {"type": "ir.actions.act_window_close"}
