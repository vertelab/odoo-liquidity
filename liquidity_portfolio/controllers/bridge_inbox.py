# -*- coding: utf-8 -*-
"""The firm's receiving end.

The customer's bridge posts here. The direction of trust is the opposite of
``liquidity_bridge``: that module is installed at the customer and accepts the
firm's key; this one runs at the firm and accepts the customer's.

Authentication is the same shared secret, held per source: the event arrives
with ``X-Bridge-Key`` and the firm looks up which tenant that key belongs to.
An unknown key is refused -- but an event from a *known* key whose company the
firm has not imported is stored and marked unmatched, not dropped.
"""
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def _json(payload, status=200):
    return request.make_json_response(payload, status=status)


def _tenant_for_key(key):
    """Which source this bridge key belongs to.

    Compared in Python rather than in SQL: the key is a secret, and a query
    that compares it in the database would put it in the query log.
    """
    if not key:
        return request.env["liquidity.tenant"].browse()
    Tenant = request.env["liquidity.tenant"].sudo()
    for tenant in Tenant.search([("bridge_keykeep_ref", "!=", False)]):
        try:
            expected = tenant._bridge_key()
        except Exception:  # noqa: BLE001 - a broken key must not 500 the endpoint
            _logger.exception("Could not read the bridge key for %s", tenant.name)
            continue
        if expected and expected == key:
            return tenant
    return Tenant.browse()


class BridgeInboxController(http.Controller):

    @http.route("/liquidity_portfolio/event", type="http", auth="none",
                methods=["POST"], csrf=False)
    def receive_event(self, **kwargs):
        """One event from a customer's bridge.

        Idempotent by uuid. Answers 200 for a duplicate so the customer's queue
        stops retrying -- a retry that keeps getting 201 would loop forever.
        """
        key = request.httprequest.headers.get("X-Bridge-Key", "")
        tenant = _tenant_for_key(key)
        if not tenant:
            # No detail: a wrong key learns nothing about which sources exist.
            return _json({"error": "unauthorised"}, status=401)

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except ValueError:
            return _json({"error": "bad_json"}, status=400)

        if not payload.get("uuid"):
            return _json({"error": "missing_uuid"}, status=400)

        # Chat messages are a channel, not a notification: they are stored in
        # their own model so the conversation has one home. Everything else is
        # an event the firm triages.
        if payload.get("event_type") == "chat_message":
            return self._receive_chat(payload, tenant)

        event, created = request.env["liquidity.incoming.event"].sudo().receive(
            payload, tenant=tenant
        )
        return _json(
            {"status": "stored" if created else "duplicate", "id": event.id},
            status=201 if created else 200,
        )

    def _receive_chat(self, payload, tenant):
        """A chat message from the customer, into the firm's side of the channel."""
        extra = payload.get("extra") or {}
        message_uuid = extra.get("message_uuid") or payload.get("uuid")
        thread_key = extra.get("thread_key")
        if not message_uuid or not thread_key:
            return _json({"error": "missing_message_fields"}, status=400)

        Message = request.env["liquidity.bridge.message"].sudo()
        existing = Message.search([("uuid", "=", message_uuid)], limit=1)
        if existing:
            return _json({"status": "duplicate", "id": existing.id})

        message = Message.create({
            "uuid": message_uuid,
            "thread_key": thread_key,
            "body": payload.get("body") or "",
            "author_kind": "customer",
            "author_name": payload.get("sent_by") or "customer",
            "sent_at": payload.get("sent_at") or False,
            "res_model": payload.get("res_model") or False,
            "res_id": payload.get("res_id") or 0,
            "res_reference": payload.get("res_reference") or False,
        })
        return _json({"status": "stored", "id": message.id}, status=201)
