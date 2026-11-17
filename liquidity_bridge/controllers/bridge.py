# -*- coding: utf-8 -*-
"""The HTTP surface the firm talks to.

Four endpoints, and every one of them refuses by default:

    POST /liquidity_bridge/event          the firm sends a message to the customer
    POST /liquidity_bridge/request        the firm asks for a session
    GET  /liquidity_bridge/open?t=...     the browser redeems the token
    GET  /liquidity_bridge/status         what this customer allows

The order of the checks is the security design, not an accident:

    1. is the bridge enabled at all?          -> 403, says so
    2. does the key match?                    -> 401, says nothing else
    3. is this function allowed here?          -> 403, says which one
    4. does the token / user exist and fit?    -> 403, logged
    5. write the access log                    -> if this fails, no session

A request that fails at step 2 learns nothing about the customer's
configuration. A request that fails at step 3 learns only that the function is
off -- which the firm already knows from its own settings.
"""
import json
import logging

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request

_logger = logging.getLogger(__name__)


def _json(payload, status=200):
    return request.make_json_response(payload, status=status)


def _config():
    return request.env["liquidity.bridge.config"].sudo()


def _authorised():
    """Steps 1 and 2. Returns (ok, response) so callers stay flat."""
    config = _config()
    if not config.is_enabled():
        return False, _json(
            {"error": "disabled", "message": "the bridge is switched off here"},
            status=403,
        )
    expected = config.bridge_key()
    supplied = request.httprequest.headers.get("X-Bridge-Key", "")
    if not expected or not supplied or supplied != expected:
        # No detail: a wrong key must not learn whether the bridge is enabled.
        return False, _json({"error": "unauthorised"}, status=401)
    return True, None


def _client_ip():
    return request.httprequest.remote_addr or ""


class LiquidityBridgeController(http.Controller):

    # -- status ------------------------------------------------------------

    @http.route("/liquidity_bridge/status", type="http", auth="none",
                methods=["GET"], csrf=False)
    def status(self, **kwargs):
        """What this installation allows. The firm's connection test uses it."""
        ok, response = _authorised()
        if not ok:
            return response
        return _json(_config().status())

    # -- incoming event ----------------------------------------------------

    @http.route("/liquidity_bridge/event", type="http", auth="none",
                methods=["POST"], csrf=False)
    def receive_event(self, **kwargs):
        """An event from the firm, delivered to the customer.

        Idempotent by ``uuid``: the firm retries, and a retry must not create a
        second message. The uuid is the firm's, not ours.
        """
        ok, response = _authorised()
        if not ok:
            return response

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except ValueError:
            return _json({"error": "bad_json"}, status=400)

        event_uuid = payload.get("uuid")
        if not event_uuid:
            return _json({"error": "missing_uuid"}, status=400)

        Incoming = request.env["bridge.incoming"].sudo()
        existing = Incoming.search([("uuid", "=", event_uuid)], limit=1)
        if existing:
            # Already delivered. Answer 200 so the sender stops retrying.
            return _json({"status": "duplicate", "id": existing.id})

        row = Incoming.create({
            "uuid": event_uuid,
            "event_type": payload.get("event_type") or "message",
            "summary": payload.get("summary") or "",
            "body": payload.get("body") or "",
            "agency": _config().agency_name(),
            "res_model": payload.get("res_model") or False,
            "res_id": payload.get("res_id") or 0,
        })
        return _json({"status": "stored", "id": row.id}, status=201)

    # -- agency accounts ---------------------------------------------------

    @http.route("/liquidity_bridge/account/provision", type="http", auth="none",
                methods=["POST"], csrf=False)
    def provision_account(self, **kwargs):
        """Create an agency account at this customer.

        Deliberately has no parameter for groups, passwords or rights: the
        only inputs are a login and an optional consultant reference. That is
        the threat model's rule 1 expressed as a signature -- what cannot be
        passed cannot be escalated.
        """
        ok, response = _authorised()
        if not ok:
            return response

        config = _config()
        if not config.allows_agency_accounts():
            return _json(
                {"error": "agency_accounts_disabled",
                 "message": "this customer does not allow agency accounts"},
                status=403,
            )

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except ValueError:
            return _json({"error": "bad_json"}, status=400)

        login = payload.get("login")
        if not login:
            return _json({"error": "missing_login"}, status=400)

        try:
            record = request.env["bridge.agency.account"].sudo().provision(
                login, consultant_ref=payload.get("consultant_ref")
            )
        except AccessError as exc:
            # The message is the useful part: "already exists" versus "not
            # allowed" leads to different actions at the firm.
            return _json({"error": "refused", "message": str(exc)}, status=403)

        return _json({
            "status": "created",
            "id": record.id,
            "login": record.login,
        }, status=201)

    @http.route("/liquidity_bridge/account/deactivate", type="http", auth="none",
                methods=["POST"], csrf=False)
    def deactivate_account(self, **kwargs):
        """Archive an agency account. Kept, so the access log still resolves."""
        ok, response = _authorised()
        if not ok:
            return response

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except ValueError:
            return _json({"error": "bad_json"}, status=400)

        login = payload.get("login")
        if not login:
            return _json({"error": "missing_login"}, status=400)

        Account = request.env["bridge.agency.account"].sudo()
        account = Account.search([("login", "=", login), ("active", "=", True)],
                                 limit=1)
        if not account:
            # Not an error the firm can fix: the account is already gone or was
            # never here. Answer 200 so a retry loop stops.
            return _json({"status": "not_found"}, status=200)

        account.action_deactivate()
        return _json({"status": "deactivated", "login": login})

    # -- incoming chat message ---------------------------------------------

    @http.route("/liquidity_bridge/message", type="http", auth="none",
                methods=["POST"], csrf=False)
    def receive_message(self, **kwargs):
        """A chat message from the firm, into the mirrored channel.

        Idempotent by ``uuid``: the firm retries when it is unsure a delivery
        landed, and a retry must not duplicate the message. The uuid is the
        firm's, not ours, because the firm is the original for its own
        messages.
        """
        ok, response = _authorised()
        if not ok:
            return response

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except ValueError:
            return _json({"error": "bad_json"}, status=400)

        message_uuid = payload.get("uuid")
        body = payload.get("body")
        thread_key = payload.get("thread_key")
        if not message_uuid or not body or not thread_key:
            return _json({"error": "missing_fields"}, status=400)

        Message = request.env["bridge.message"].sudo()
        existing = Message.search([("uuid", "=", message_uuid)], limit=1)
        if existing:
            return _json({"status": "duplicate", "id": existing.id})

        message = Message.create({
            "uuid": message_uuid,
            "thread_key": thread_key,
            "body": body,
            "author_kind": "agency",
            "author_name": payload.get("author_name")
                           or _config().agency_name() or "agency",
            "sent_at": payload.get("sent_at") or fields_now(),
            "res_model": payload.get("res_model") or False,
            "res_id": payload.get("res_id") or 0,
            "res_reference": payload.get("res_reference") or False,
        })
        return _json({"status": "stored", "id": message.id}, status=201)

    # -- session request ---------------------------------------------------

    @http.route("/liquidity_bridge/request", type="http", auth="none",
                methods=["POST"], csrf=False)
    def request_session(self, **kwargs):
        """The firm asks for a session. Returns a token and the URL to open.

        The token is not a session. It is a ticket the browser redeems, so the
        firm's server never holds anything that can open the customer's books
        on its own.
        """
        ok, response = _authorised()
        if not ok:
            return response

        config = _config()
        if not config.allows_impersonation():
            return _json(
                {"error": "impersonation_disabled",
                 "message": "this customer does not allow the firm to open "
                            "their system"},
                status=403,
            )

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except ValueError:
            return _json({"error": "bad_json"}, status=400)

        login = payload.get("login")
        company_ref = payload.get("company_ref")
        if not login:
            return _json({"error": "missing_login"}, status=400)

        Users = request.env["res.users"].sudo()
        user = Users.search([("login", "=", login), ("active", "=", True)], limit=1)
        if not user:
            return _json({"error": "unknown_user"}, status=403)

        Companies = request.env["res.company"].sudo()
        company = (
            Companies.browse(int(company_ref))
            if company_ref else request.env.company
        )
        if not company.exists() or company not in user.company_ids:
            # The user must already have access to the company. We do not grant
            # it here: a session request is not a permission change.
            return _json({"error": "company_not_allowed"}, status=403)

        token = request.env["bridge.token"].sudo().issue(
            user, company, payload.get("menu_xmlid"),
        )
        base = request.httprequest.host_url.rstrip("/")
        return _json({
            "token": token.token,
            "url": "%s/liquidity_bridge/open?t=%s" % (base, token.token),
            "expires_at": token.expires_at.isoformat(),
        })

    # -- redeem ------------------------------------------------------------

    @http.route("/liquidity_bridge/open", type="http", auth="none",
                methods=["GET"], csrf=False)
    def open_session(self, t=None, **kwargs):
        """Redeem a token and open a session. The only place a session starts.

        The access log is written before the session exists, in this
        transaction. If the log write fails the request fails, and no session
        is created -- "no session without a log line" has to be a rule, because
        an unlogged session is exactly what the customer cannot forgive.
        """
        config = _config()
        if not config.is_enabled():
            return request.not_found()
        if not config.allows_impersonation():
            return request.not_found()
        if not t:
            return request.not_found()

        Token = request.env["bridge.token"].sudo()
        token = Token.search([("token", "=", t)], limit=1)

        if not token or not token._is_valid():
            # Log the refusal when we can identify the user, so a burst of
            # expired tokens is visible rather than invisible.
            if token:
                request.env["bridge.access.log"].sudo().log(
                    agency=token.issued_by,
                    user=token.user_id,
                    company=token.company_id,
                    outcome="refused",
                    reason="expired" if not token.used_at else "already used",
                    menu_xmlid=token.menu_xmlid,
                    ip_address=_client_ip(),
                )
            return request.not_found()

        # Burn the token first. If anything below fails, the token is spent --
        # which is the safe direction to fail in.
        token.write({"used_at": fields_now()})

        request.env["bridge.access.log"].sudo().log(
            agency=token.issued_by,
            user=token.user_id,
            company=token.company_id,
            outcome="granted",
            menu_xmlid=token.menu_xmlid,
            ip_address=_client_ip(),
        )

        # Open the session as the token's user, scoped to the token's company.
        request.session.uid = token.user_id.id
        request.session.login = token.user_id.login
        request.session.allowed_company_ids = [token.company_id.id]
        request.session.company_id = token.company_id.id
        request.env = request.env(user=token.user_id.id)

        target = "/odoo"
        if token.menu_xmlid:
            menu = request.env.ref(token.menu_xmlid, raise_if_not_found=False)
            if menu:
                target = "/odoo?menu_id=%s" % menu.id
        return request.redirect(target)


def fields_now():
    """Small indirection so the controller stays importable without odoo.fields."""
    from odoo import fields
    return fields.Datetime.now()
