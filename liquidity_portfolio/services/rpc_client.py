# -*- coding: utf-8 -*-
"""JSON-RPC client for reading a source Odoo instance.

Design rules, all of them learned from what goes wrong in a portfolio of
hundreds of customers:

* **Never raise.** A portfolio that raises on one bad source shows nothing at
  all. Every failure is classified and returned.
* **Never retry inside the timeout.** A retry that silently doubles the wait
  turns a slow customer into a hung view. The timeout is the contract.
* **Classify, do not flatten.** "The instance is down", "the key is wrong" and
  "liquidity_forecast is missing" are three different things to the user, and
  only one of them is worth waking someone up for.
"""
import json
import logging

import requests

_logger = logging.getLogger(__name__)

# Outcomes. These are the only values a caller has to handle.
UNREACHABLE = "unreachable"      # no answer within the timeout
AUTH_ERROR = "auth_error"        # answered, but refused the key
NO_LIQUIDITY = "no_liquidity"    # answered, but liquidity_forecast is absent
RPC_ERROR = "rpc_error"          # answered with an error we did not expect
OK = "ok"

DEFAULT_TIMEOUT = 10


class RpcResult:
    """Outcome of one call. ``value`` is only meaningful when ``outcome`` is OK."""

    __slots__ = ("outcome", "value", "message")

    def __init__(self, outcome, value=None, message=""):
        self.outcome = outcome
        self.value = value
        self.message = message

    @property
    def ok(self):
        return self.outcome == OK

    def __repr__(self):
        return "<RpcResult %s%s>" % (
            self.outcome,
            " %s" % self.message if self.message else "",
        )


class OdooRpcClient:
    """Minimal JSON-RPC client. One instance per call — it holds no session."""

    def __init__(self, base_url, db, login, api_key, timeout=DEFAULT_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.db = db
        self.login = login
        self.api_key = api_key
        self.timeout = timeout or DEFAULT_TIMEOUT
        self._uid = None

    # -- transport ---------------------------------------------------------

    def _call(self, service, method, args):
        """One JSON-RPC round trip. Returns RpcResult, never raises."""
        url = "%s/jsonrpc" % self.base_url
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": 1,
        }
        try:
            response = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            return RpcResult(
                UNREACHABLE,
                # Phrased as an observation, not a verdict. A customer whose
                # server is slow at 08:00 is not broken; the reader needs to
                # know what happened and what to do, not that something failed.
                message=(
                    "no answer within %s seconds — open the bookkeeping to "
                    "try again" % self.timeout
                ),
            )
        except requests.exceptions.RequestException as exc:
            return RpcResult(UNREACHABLE, message=str(exc)[:200])

        if response.status_code in (401, 403):
            return RpcResult(AUTH_ERROR, message="HTTP %s" % response.status_code)

        try:
            body = response.json()
        except ValueError:
            return RpcResult(
                RPC_ERROR,
                message="HTTP %s, not JSON" % response.status_code,
            )

        error = body.get("error")
        if error:
            return self._classify_error(error)

        return RpcResult(OK, value=body.get("result"))

    @staticmethod
    def _classify_error(error):
        """Map an Odoo JSON-RPC error envelope to one of our outcomes."""
        data = error.get("data") or {}
        name = data.get("name") or ""
        text = (data.get("message") or error.get("message") or "")[:300]

        if "AccessDenied" in name or "AccessError" in name:
            return RpcResult(AUTH_ERROR, message=text)
        if "SessionExpired" in name:
            return RpcResult(AUTH_ERROR, message=text)
        return RpcResult(RPC_ERROR, message="%s: %s" % (name or "error", text))

    # -- authentication ----------------------------------------------------

    def authenticate(self):
        """Verify the key. Returns RpcResult whose value is the uid."""
        result = self._call(
            "common", "authenticate", [self.db, self.login, self.api_key, {}]
        )
        if not result.ok:
            return result
        uid = result.value
        if not uid:
            return RpcResult(
                AUTH_ERROR,
                message="the key was refused for %s" % self.login,
            )
        return RpcResult(OK, value=uid)

    # -- data access -------------------------------------------------------

    def search_read(self, model, domain, fields, limit=None, order=None):
        """search_read over JSON-RPC. Returns RpcResult whose value is a list."""
        kwargs = {"fields": fields}
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self._call(
            "object", "execute_kw",
            [self.db, self._uid, self.api_key, model, "search_read",
             [domain], kwargs],
        )

    def _set_uid(self, uid):
        self._uid = uid

    def login_and(self, uid):
        """Bind the uid from ``authenticate`` so search_read can be called."""
        self._set_uid(uid)
        return self


def connect(base_url, db, login, api_key, timeout=DEFAULT_TIMEOUT):
    """Authenticate and return a bound client, or an RpcResult describing why not.

    Returns a tuple ``(client_or_None, RpcResult)``. The RpcResult is OK when a
    client was returned.
    """
    client = OdooRpcClient(base_url, db, login, api_key, timeout=timeout)
    auth = client.authenticate()
    if not auth.ok:
        return None, auth
    client.login_and(auth.value)
    return client, auth


def module_version(client, module_name="liquidity_forecast"):
    """Read the installed version of a module in the source.

    Returns RpcResult whose value is the version string, or None when the
    module is not installed.
    """
    result = client.search_read(
        "ir.module.module",
        [("name", "=", module_name), ("state", "=", "installed")],
        ["name", "latest_version", "state"],
        limit=1,
    )
    if not result.ok:
        return result
    rows = result.value or []
    if not rows:
        return RpcResult(NO_LIQUIDITY, message="%s is not installed" % module_name)
    return RpcResult(OK, value=rows[0].get("latest_version") or "")
