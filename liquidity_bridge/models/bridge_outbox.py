# -*- coding: utf-8 -*-
"""The outgoing queue: events the firm has not received yet.

The reason this exists is narrow and important. A bridge that posts directly to
the firm's server loses the event when that server is restarting, and the
customer has no way of knowing their question never arrived. The queue makes
"not delivered yet" a visible state instead of a silent loss.

Two rules:

* **Idempotent by UUID.** The event carries a UUID from the moment it is
  created. Delivering it twice must not create two work orders at the firm, so
  the UUID travels with it and the receiving side keys on it.
* **Retry is bounded and visible.** ``attempts`` and ``last_error`` are shown
  to the customer's own admin. An event that has failed ten times is a problem
  the customer can see, not one they discover by asking why nobody answered.
"""
import json
import logging
import uuid

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

#: After this many attempts the event is left in ``failed`` and stops being
#: retried automatically. Retrying forever keeps a broken endpoint at the front
#: of every cron run and hides the real problem.
MAX_ATTEMPTS = 10


class BridgeOutbox(models.Model):
    """One event waiting to reach the firm."""

    _name = "bridge.outbox"
    _description = "Outgoing event to the accounting firm"
    _order = "create_date desc, id desc"
    _rec_name = "summary"

    uuid = fields.Char(
        string="Event id",
        required=True,
        readonly=True,
        index=True,
        default=lambda self: str(uuid.uuid4()),
        help="Stable id from the moment the event is created. The firm keys "
             "on this, so delivering the same event twice cannot create two "
             "work orders.",
    )
    event_type = fields.Selection(
        selection=[
            ("entry_flagged", "Entry flagged for review"),
            ("invoice_question", "Question about an invoice"),
            ("free_question", "Question"),
            ("chat_message", "Chat message"),
        ],
        string="Type",
        required=True,
        default="free_question",
    )
    summary = fields.Char(
        string="Summary",
        required=True,
        help="One line the firm sees in its list. Written here, at the "
             "customer, so the wording is the customer's.",
    )
    body = fields.Text(string="Message")
    res_model = fields.Char(string="Related model")
    res_id = fields.Integer(string="Related record")
    res_reference = fields.Char(
        string="Related record",
        help="Human-readable reference (invoice number, entry name) so the "
             "firm can find the record without a database id.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Sent by",
        default=lambda self: self.env.user,
        readonly=True,
        help="The customer's own user who created the event. The firm sees "
             "who asked, not just that someone did.",
    )
    state = fields.Selection(
        selection=[
            ("queued", "Queued"),
            ("sending", "Sending"),
            ("delivered", "Delivered"),
            ("failed", "Failed"),
        ],
        string="Status",
        default="queued",
        required=True,
        index=True,
    )
    attempts = fields.Integer(string="Attempts", default=0, readonly=True)
    last_attempt_at = fields.Datetime(string="Last attempt", readonly=True)
    last_error = fields.Text(string="Last error", readonly=True)
    extra_json = fields.Text(
        string="Additional fields",
        help="Per-event-type fields as JSON (the message uuid and thread key "
             "for a chat message). A column per event type would grow the "
             "queue's schema every time a new event is invented.",
    )
    delivered_at = fields.Datetime(string="Delivered", readonly=True)

    _sql_constraints = [
        (
            "bridge_outbox_uuid_uniq",
            "unique(uuid)",
            "An event with this id already exists.",
        ),
    ]

    @api.model
    def enqueue(self, event_type, summary, body=None, res_model=None, res_id=None,
                res_reference=None, extra=None):
        """Put an event in the queue. Always succeeds -- that is the point.

        ``extra`` carries per-type fields (the message uuid and thread key for
        a chat message) as JSON. A column per event type would make the queue
        grow a schema every time a new event is invented.
        """
        return self.create({
            "event_type": event_type,
            "summary": summary,
            "body": body,
            "res_model": res_model,
            "res_id": res_id,
            "res_reference": res_reference,
            "extra_json": json.dumps(extra) if extra else False,
        })

    def action_retry(self):
        """Put a failed event back in the queue, by hand."""
        for event in self:
            if event.state in ("failed", "queued"):
                event.write({"state": "queued", "last_error": False})
        return True

    @api.model
    def _cron_deliver(self):
        """Deliver queued events. Never raises: one bad event must not stop the rest."""
        config = self.env["liquidity.bridge.config"]
        if not config.is_enabled():
            return True
        agency_url = config.agency_url()
        if not agency_url:
            # No endpoint configured: the queue is the whole mechanism, and the
            # firm polls. Leaving events queued is correct, not a failure.
            return True

        pending = self.search([
            ("state", "=", "queued"),
            ("attempts", "<", MAX_ATTEMPTS),
        ], limit=50)

        for event in pending:
            event._deliver_one(agency_url)
        return True

    def _deliver_one(self, agency_url):
        """One delivery attempt, with the outcome recorded either way."""
        self.ensure_one()
        config = self.env["liquidity.bridge.config"]
        self.write({"state": "sending"})
        try:
            import json
            import requests

            response = requests.post(
                agency_url.rstrip("/") + "/liquidity_bridge/event",
                data=json.dumps(self._payload()),
                headers={
                    "Content-Type": "application/json",
                    "X-Bridge-Key": config.bridge_key(),
                },
                timeout=15,
            )
            if response.status_code in (200, 201):
                self.write({
                    "state": "delivered",
                    "delivered_at": fields.Datetime.now(),
                    "attempts": self.attempts + 1,
                    "last_attempt_at": fields.Datetime.now(),
                    "last_error": False,
                })
                return
            self._record_failure("HTTP %s" % response.status_code)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            _logger.info("Bridge delivery failed for %s: %s", self.uuid, exc)
            self._record_failure(str(exc)[:300])

    def _record_failure(self, message):
        self.ensure_one()
        attempts = self.attempts + 1
        self.write({
            "attempts": attempts,
            "last_attempt_at": fields.Datetime.now(),
            "last_error": message,
            # After the last attempt the event stops being retried. It stays
            # visible as failed, which is the honest state: we tried, it did
            # not arrive, and someone has to look.
            "state": "failed" if attempts >= MAX_ATTEMPTS else "queued",
        })

    def _payload(self):
        """What the firm receives. Deliberately flat and self-describing."""
        self.ensure_one()
        return {
            "uuid": self.uuid,
            "event_type": self.event_type,
            "summary": self.summary,
            "body": self.body or "",
            "res_model": self.res_model or "",
            "res_id": self.res_id or 0,
            "res_reference": self.res_reference or "",
            "company_ref": self.company_id.id,
            "company_name": self.company_id.name,
            "sent_by": self.user_id.name or "",
            "sent_at": fields.Datetime.to_string(self.create_date),
            "extra": json.loads(self.extra_json) if self.extra_json else {},
            "bridge_version": self.env["ir.module.module"].sudo().search(
                [("name", "=", "liquidity_bridge")], limit=1
            ).latest_version or "",
        }
