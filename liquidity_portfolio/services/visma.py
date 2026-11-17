# -*- coding: utf-8 -*-
"""Visma driver.

Visma is not one API. It is a family of products with different APIs and
different coverage:

| Product | API | What it can give us |
|---|---|---|
| Visma eEkonomi | `/api/v2` (OAuth2) | accounts, invoices, supplier invoices |
| Visma Administration | `/api/v1` (OAuth2) | the same, different field names |
| Visma Netvisor / Business NXT | separate products, separate contracts | out of scope here |

The design decision is therefore: **one driver, a product profile per
connection**, and a capability set that follows from the profile rather than
from hope. A connection that has not declared its product gets no capabilities
at all, and the row says "not supported by the source" instead of guessing.

This is the honest version of "Visma support". Claiming a single Visma API
would produce a driver that works for one product and silently returns zeros
for the others -- exactly the failure mode this change exists to prevent.

Field names differ between the products, so the parsing is driven by a small
profile table rather than by hardcoded keys in the fetch code. Adding a product
is then a data change, not a code change.
"""
import logging

import requests

from . import api_transport as transport
from . import raw_data

_logger = logging.getLogger(__name__)

SOURCE_TYPE = "visma"

#: Product profiles. ``api_root`` and the field names are the documented ones
#: for each product; ``capabilities`` is what that product's endpoints carry.
#:
#: ``eekonomi`` is the primary product: it is what a Swedish accounting firm's
#: small customers actually run, and its v2 API exposes invoice dates, which is
#: what makes ageing derivable.
PROFILES = {
    "eekonomi": {
        "label": "Visma eEkonomi",
        "api_root": "https://eaccountingapi.vismaonline.com/v2",
        "accounts_path": "/accounts",
        "receivables_path": "/customerinvoices",
        "payables_path": "/supplierinvoices",
        "accounts_key": "Accounts",
        "receivables_key": "CustomerInvoices",
        "payables_key": "SupplierInvoices",
        "balance_keys": ("BalanceCarriedForward", "BalanceBroughtForward"),
        "amount_keys": ("Balance", "TotalAmount"),
        "due_keys": ("DueDate",),
        "invoice_date_keys": ("InvoiceDate",),
        "capabilities": frozenset({
            "balances",
            "receivables",
            "payables",
            "due_dates",
            "invoice_dates",
            "currency",
        }),
    },
    "administration": {
        "label": "Visma Administration",
        "api_root": "https://api.vismaspcs.se/api/v1",
        "accounts_path": "/accounts",
        "receivables_path": "/customerinvoices",
        "payables_path": "/supplierinvoices",
        "accounts_key": "Accounts",
        "receivables_key": "CustomerInvoices",
        "payables_key": "SupplierInvoices",
        "balance_keys": ("Balance",),
        "amount_keys": ("RemainingAmount", "Amount"),
        "due_keys": ("DueDate",),
        "invoice_date_keys": ("InvoiceDate",),
        # Administration does not expose per-item currency in the same way;
        # claiming it would make the conversion principle unenforceable.
        "capabilities": frozenset({
            "balances",
            "receivables",
            "payables",
            "due_dates",
            "invoice_dates",
        }),
    },
}

#: Used when the connection has not declared a product. Empty on purpose: a
#: driver that guesses its own capabilities is a driver that shows numbers it
#: cannot justify.
DEFAULT_PROFILE = None

AUTHORIZE_URL = "https://identity.vismaonline.com/connect/authorize"
TOKEN_URL = "https://identity.vismaonline.com/connect/token"


class VismaDriver(object):
    """Fetches underlag from one Visma product. Returns RawLiquidityData."""

    source_type = SOURCE_TYPE

    def __init__(self, credential, session=None, limiter=None, sleep=None):
        self.credential = credential or {}
        self.session = session or requests.Session()
        self.limiter = limiter or transport.RateLimiter()
        self._sleep = sleep
        self._refreshed = None
        self.profile_name = self.credential.get("product")
        self.profile = PROFILES.get(self.profile_name)

    # -- contract ----------------------------------------------------------

    def capabilities(self):
        """Whatever the declared product carries -- nothing when undeclared."""
        if not self.profile:
            return set()
        return set(self.profile["capabilities"])

    def verify_url(self, company_ref=None):
        """No public per-figure page in Visma either. See the Fortnox note."""
        return None

    def test_connection(self):
        if not self.profile:
            return raw_data.ConnectionResult(
                False,
                "unknown_product",
                "Visma-produkten är inte angiven för den här anslutningen",
            )
        result = self._get(self.profile["accounts_path"])
        if result.ok:
            return raw_data.ConnectionResult(
                True, "ok", "%s svarar" % self.profile["label"]
            )
        return raw_data.ConnectionResult(False, result.outcome, result.message)

    def fetch_raw(self, company_ref=None):
        raw = raw_data.RawLiquidityData(
            source_type=SOURCE_TYPE,
            capabilities=self.capabilities(),
        )

        if not self.profile:
            # No product declared: every section is unsupported, not failed.
            # "Unsupported" is the truth -- we did not try, because we do not
            # know what to ask.
            raw.set_all(raw_data.UNSUPPORTED, message="Visma-produkten är inte angiven")
            raw.message = "Visma-produkten är inte angiven för den här anslutningen"
            return raw

        if self._token_is_expired():
            refreshed = self._refresh_token()
            if not refreshed.ok:
                raw.set_all(
                    raw_data.AUTH_EXPIRED
                    if refreshed.outcome == transport.AUTH_EXPIRED
                    else raw_data.FAILED,
                    message=refreshed.message,
                )
                raw.message = refreshed.message
                return raw

        self._fetch_balances(raw)
        self._fetch_open_items(raw, raw_data.SECTION_RECEIVABLES,
                               self.profile["receivables_path"],
                               self.profile["receivables_key"])
        self._fetch_open_items(raw, raw_data.SECTION_PAYABLES,
                               self.profile["payables_path"],
                               self.profile["payables_key"])

        # The bank-account section is not exposed by these endpoints; saying so
        # is better than deriving it from account numbers, which differ between
        # products.
        raw.set_section(raw_data.SECTION_BANK, raw_data.UNSUPPORTED)
        raw.currency = "SEK"
        return raw

    # -- sections ----------------------------------------------------------

    def _fetch_balances(self, raw):
        result = self._get(self.profile["accounts_path"])
        if not result.ok:
            self._record_failure(raw, raw_data.SECTION_BALANCES, result)
            return
        rows = _rows(result.value, self.profile["accounts_key"])
        items = []
        for row in rows:
            balance = None
            for key in self.profile["balance_keys"]:
                balance = _as_float(row.get(key))
                if balance is not None:
                    break
            if balance is None:
                continue
            items.append({
                "account": row.get("Number") or row.get("AccountNumber"),
                "name": row.get("Name") or row.get("Description") or "",
                "balance": balance,
            })
        if items:
            raw.set_section(raw_data.SECTION_BALANCES, raw_data.OK, items)
        else:
            raw.set_section(raw_data.SECTION_BALANCES, raw_data.EMPTY)

    def _fetch_open_items(self, raw, section, path, key):
        result = self._get(path)
        if not result.ok:
            self._record_failure(raw, section, result)
            return
        rows = _rows(result.value, key)
        items = []
        for row in rows:
            amount = None
            for amount_key in self.profile["amount_keys"]:
                amount = _as_float(row.get(amount_key))
                if amount is not None:
                    break
            if amount is None:
                continue
            due = None
            for due_key in self.profile["due_keys"]:
                if row.get(due_key):
                    due = row[due_key]
                    break
            invoice_date = None
            for date_key in self.profile["invoice_date_keys"]:
                if row.get(date_key):
                    invoice_date = row[date_key]
                    break
            items.append({
                "reference": row.get("InvoiceNumber") or row.get("Id"),
                "partner": row.get("CustomerName") or row.get("SupplierName") or "",
                "amount": amount,
                "due_date": due,
                "invoice_date": invoice_date,
                "currency": row.get("CurrencyCode") or row.get("Currency"),
                "age_days": _age_days(invoice_date),
            })
        if items:
            raw.set_section(section, raw_data.OK, items)
        else:
            raw.set_section(section, raw_data.EMPTY)

    def _record_failure(self, raw, section, result):
        mapping = {
            transport.AUTH_EXPIRED: raw_data.AUTH_EXPIRED,
            transport.RATE_LIMITED: raw_data.RATE_LIMITED,
            transport.UNREACHABLE: raw_data.FAILED,
            transport.API_ERROR: raw_data.FAILED,
        }
        raw.set_section(
            section,
            mapping.get(result.outcome, raw_data.FAILED),
            message=result.message,
        )

    # -- transport ---------------------------------------------------------

    def _token_is_expired(self):
        expires_at = self.credential.get("expires_at")
        if not expires_at:
            return False
        import time
        return float(expires_at) <= time.time() + 30

    def _refresh_token(self):
        refresh_token = self.credential.get("refresh_token")
        client_id = self.credential.get("client_id")
        if not (refresh_token and client_id):
            return transport.ApiResult(
                transport.AUTH_EXPIRED,
                message="the connection needs renewing (no refresh token)",
            )
        result = transport.request_json(
            self.session,
            "POST",
            TOKEN_URL,
            messages={"timeout": "Visma svarade inte vid tokenförnyelse"},
            json_body={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=transport.DEFAULT_TIMEOUT,
        )
        if not result.ok:
            return result
        body = result.value or {}
        token = body.get("access_token")
        if not token:
            return transport.ApiResult(
                transport.AUTH_EXPIRED,
                message="the source returned no access token",
            )
        import time
        self.credential["access_token"] = token
        if body.get("refresh_token"):
            self.credential["refresh_token"] = body["refresh_token"]
        self.credential["expires_at"] = time.time() + float(
            body.get("expires_in") or 3600
        )
        self._refreshed = dict(self.credential)
        return transport.ApiResult(transport.OK, value=token)

    @property
    def refreshed_credential(self):
        return self._refreshed

    def _get(self, path, params=None):
        self.limiter.wait(self.credential.get("client_id") or "visma")
        self.session.headers.update({
            "Authorization": "Bearer %s" % self.credential.get("access_token"),
            "Accept": "application/json",
        })
        return transport.request_json(
            self.session,
            "GET",
            "%s%s" % (self.profile["api_root"], path),
            messages={"timeout": "Visma svarade inte inom tidsgränsen"},
            params=params,
            timeout=transport.DEFAULT_TIMEOUT,
            **({"sleep": self._sleep} if self._sleep else {})
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _rows(body, key):
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    if isinstance(body.get(key), list):
        return body[key]
    if isinstance(body.get(key), dict):
        return [body[key]]
    return []


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _age_days(invoice_date):
    if not invoice_date:
        return None
    from datetime import date, datetime
    try:
        if isinstance(invoice_date, str):
            parsed = datetime.strptime(invoice_date[:10], "%Y-%m-%d").date()
        else:
            parsed = invoice_date
        return (date.today() - parsed).days
    except (TypeError, ValueError):
        return None
