# -*- coding: utf-8 -*-
"""Fortnox driver.

Fortnox's public API is OAuth2 with per-tenant credentials and a documented
scope model. Three things about it shape this driver:

**Tokens rotate.** An access token lives about an hour; the refresh token is
what actually belongs to the customer. So the credential holds both, and a
renewal writes the new pair back -- otherwise the connection dies silently an
hour after it was set up, which is the least useful kind of failure.

**The rate limit is per tenant and it is real.** Fortnox returns 429 with a
``Retry-After``. With two hundred customers this is the difference between a
working portfolio and a red wall.

**Not every endpoint is available on every plan.** A customer on a small plan
may answer for balances but refuse the invoice endpoint. That is not an error
to alarm about -- it is a capability the source does not have, and the row says
so.

Endpoint names are the documented Fortnox ones (``/3/accounts``,
``/3/invoices``, ``/3/supplierinvoices``, ``/3/accountingaccounts``). The
driver reads; it never writes.

Nothing here is called at import time: the unit tests run against recorded
bodies, so no customer credential is needed to verify the parsing.
"""
import logging

import requests

from . import api_transport as transport
from . import raw_data

_logger = logging.getLogger(__name__)

SOURCE_TYPE = "fortnox"

#: What a Fortnox connection can carry, given the endpoints the driver reads.
#: ``invoice_dates`` is included because ``/3/invoices`` returns ``InvoiceDate``
#: and ``DueDate``; ageing is therefore derivable, unlike for a source that only
#: exposes totals.
CAPABILITIES = frozenset({
    "balances",
    "bank_accounts",
    "receivables",
    "payables",
    "due_dates",
    "invoice_dates",
    "currency",
})

AUTHORIZE_URL = "https://apps.fortnox.se/oauth-v1/auth"
TOKEN_URL = "https://apps.fortnox.se/oauth-v1/token"
API_ROOT = "https://api.fortnox.se/3"

#: Scope needed for the endpoints read here. Read-only: the driver must not be
#: able to change a customer's books, even if a token were stolen.
SCOPES = "bookkeeping companyinformation"


class FortnoxDriver(object):
    """Fetches underlag from Fortnox. Returns RawLiquidityData, never KPIs."""

    source_type = SOURCE_TYPE
    capabilities_set = CAPABILITIES

    def __init__(self, credential, session=None, limiter=None, sleep=None):
        """``credential`` is a dict: client_id, client_secret, access_token,
        refresh_token, expires_at. Never a record -- the driver stays testable
        without a database, and the secret never lands in a model field.
        """
        self.credential = credential or {}
        self.session = session or requests.Session()
        self.limiter = limiter or transport.RateLimiter()
        self._sleep = sleep
        self._refreshed = None

    # -- contract ----------------------------------------------------------

    def capabilities(self):
        return set(self.capabilities_set)

    def verify_url(self, company_ref=None):
        """Fortnox has no per-invoice public page to link to.

        Deliberately None: linking to the Fortnox login page for a specific
        figure would look like verification without being it. The row instead
        offers the underlag behind the figure.
        """
        return None

    def test_connection(self):
        result = self._get("/companyinformation")
        if result.ok:
            return raw_data.ConnectionResult(True, "ok", "Fortnox svarar")
        return raw_data.ConnectionResult(
            False, result.outcome, result.message
        )

    def fetch_raw(self, company_ref=None):
        """Fetch balances, bank accounts, receivables and payables.

        Each section is fetched and classified on its own, so one failing
        endpoint leaves the others usable. That is the whole point of the
        partial state.
        """
        raw = raw_data.RawLiquidityData(
            source_type=SOURCE_TYPE,
            capabilities=self.capabilities(),
        )

        # If the token is dead, every section is dead. Say so once rather than
        # repeating the same 401 four times.
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
        self._fetch_receivables(raw)
        self._fetch_payables(raw)

        raw.currency = self._guess_currency(raw)
        return raw

    # -- sections ----------------------------------------------------------

    def _fetch_balances(self, raw):
        result = self._get("/accountingaccounts")
        if not result.ok:
            self._record_failure(raw, raw_data.SECTION_BALANCES, result)
            raw.set_section(raw_data.SECTION_BANK, raw_data.UNSUPPORTED)
            return

        rows = _rows(result.value)
        items = []
        for row in rows:
            balance = _as_float(row.get("BalanceCarriedForward"))
            if balance is None:
                balance = _as_float(row.get("BalanceBroughtForward"))
            if balance is None:
                continue
            items.append({
                "account": row.get("Number"),
                "name": row.get("Description") or "",
                "balance": balance,
            })

        if items:
            raw.set_section(raw_data.SECTION_BALANCES, raw_data.OK, items)
        else:
            raw.set_section(raw_data.SECTION_BALANCES, raw_data.EMPTY)

        # Bank accounts come from the same endpoint family; Fortnox exposes
        # them through the account list. When it does not, the capability is
        # reported unsupported rather than faked from the balance total.
        bank = [i for i in items if str(i.get("account", "")).startswith("19")]
        if bank:
            raw.set_section(
                raw_data.SECTION_BANK, raw_data.OK,
                [{"name": b["name"], "balance": b["balance"]} for b in bank],
            )
        else:
            raw.set_section(raw_data.SECTION_BANK, raw_data.EMPTY)

    def _fetch_receivables(self, raw):
        result = self._get("/invoices", params={"filter": "unpaid"})
        if not result.ok:
            self._record_failure(raw, raw_data.SECTION_RECEIVABLES, result)
            return
        items = _open_items(result.value, amount_keys=("Balance", "Total"),
                            date_keys=("DueDate", "InvoiceDate"))
        if items:
            raw.set_section(raw_data.SECTION_RECEIVABLES, raw_data.OK, items)
        else:
            raw.set_section(raw_data.SECTION_RECEIVABLES, raw_data.EMPTY)

    def _fetch_payables(self, raw):
        result = self._get("/supplierinvoices", params={"filter": "unpaid"})
        if not result.ok:
            self._record_failure(raw, raw_data.SECTION_PAYABLES, result)
            return
        items = _open_items(result.value, amount_keys=("Balance", "Total"),
                            date_keys=("DueDate", "InvoiceDate"))
        if items:
            raw.set_section(raw_data.SECTION_PAYABLES, raw_data.OK, items)
        else:
            raw.set_section(raw_data.SECTION_PAYABLES, raw_data.EMPTY)

    def _record_failure(self, raw, section, result):
        """Map a transport outcome onto a section outcome.

        The distinction that matters: ``auth_expired`` and ``rate_limited`` are
        NOT ``failed``. They are separate outcomes because they mean different
        things to the user -- "renew the connection" versus "ask again later".
        """
        mapping = {
            transport.AUTH_EXPIRED: raw_data.AUTH_EXPIRED,
            transport.RATE_LIMITED: raw_data.RATE_LIMITED,
            transport.UNREACHABLE: raw_data.FAILED,
            transport.API_ERROR: raw_data.FAILED,
            transport.PARTIAL: raw_data.FAILED,
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
        """Exchange the refresh token for a new access token.

        Returns ApiResult. The refreshed pair is exposed through
        ``refreshed_credential`` so the caller can persist it -- the driver
        itself has no database and must not write anywhere.
        """
        refresh_token = self.credential.get("refresh_token")
        client_id = self.credential.get("client_id")
        client_secret = self.credential.get("client_secret")
        if not (refresh_token and client_id and client_secret):
            return transport.ApiResult(
                transport.AUTH_EXPIRED,
                message="the connection needs renewing (no refresh token)",
            )

        result = transport.request_json(
            self.session,
            "POST",
            TOKEN_URL,
            messages={"timeout": "Fortnox svarade inte vid tokenförnyelse"},
            json_body={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
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
        """The new credential after a refresh, or None. The caller persists it."""
        return self._refreshed

    def _get(self, path, params=None):
        """Spaced GET against one tenant, with the bearer token attached."""
        self.limiter.wait(kwarg_key(self.credential))
        return self._authorized_get(path, self.credential.get("access_token"), params)

    def _authorized_get(self, path, token, params=None):
        """Send the bearer token. Separate from ``request_json`` so the retry
        path keeps the header and the tests can assert on it."""
        self.session.headers.update({"Authorization": "Bearer %s" % token,
                                     "Accept": "application/json"})
        return transport.request_json(
            self.session,
            "GET",
            "%s%s" % (API_ROOT, path),
            messages={"timeout": "Fortnox svarade inte inom tidsgränsen"},
            params=params,
            timeout=transport.DEFAULT_TIMEOUT,
            **({"sleep": self._sleep} if self._sleep else {})
        )

    def _guess_currency(self, raw):
        """Currency of the source, from whicheverse section answered first."""
        for section in (raw_data.SECTION_BANK, raw_data.SECTION_BALANCES,
                        raw_data.SECTION_RECEIVABLES):
            for item in raw.items(section):
                if item.get("currency"):
                    return item["currency"]
        return "SEK"


# ---------------------------------------------------------------------------
# Parsing helpers -- module level so tests can call them without a session
# ---------------------------------------------------------------------------

def kwarg_key(credential):
    """Identifies one tenant for the rate limiter. Never the token itself."""
    return credential.get("client_id") or credential.get("tenant") or "fortnox"


def _rows(body):
    """Fortnox wraps every collection in a named key. Accept the common shapes."""
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for key in ("Accounts", "Invoices", "SupplierInvoices", "FinancialYears"):
        if isinstance(body.get(key), list):
            return body[key]
    # Some endpoints return one object under a singular key.
    for key in ("Account", "Invoice", "SupplierInvoice"):
        if isinstance(body.get(key), dict):
            return [body[key]]
    return []


def _as_float(value):
    """Fortnox returns numbers as strings and sometimes as numbers."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _open_items(body, amount_keys, date_keys):
    """Normalise one open-item collection to the driver's item shape.

    ``amount`` prefers the outstanding balance over the invoice total: a
    partially paid invoice is worth what is left, not what was billed.
    """
    items = []
    for row in _rows(body):
        amount = None
        for key in amount_keys:
            amount = _as_float(row.get(key))
            if amount is not None:
                break
        if amount is None:
            continue
        due = None
        for key in date_keys:
            if row.get(key):
                due = row[key]
                break
        items.append({
            "reference": row.get("DocumentNumber") or row.get("Id"),
            "partner": row.get("CustomerName") or row.get("SupplierName") or "",
            "amount": amount,
            "due_date": due,
            "invoice_date": row.get("InvoiceDate"),
            "currency": row.get("Currency"),
            "age_days": _age_days(row.get("InvoiceDate")),
        })
    return items


def _age_days(invoice_date):
    """Days from the invoice date to today, or None when it is not derivable.

    None is important: without it the ageing computation would treat a missing
    date as age zero, i.e. "freshly invoiced", which is the most flattering
    possible reading of missing data.
    """
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
