# -*- coding: utf-8 -*-
"""Drivers against recorded answers.

No customer credential is needed to verify the parsing: every test builds a
fake session that returns a recorded body. That is deliberate -- a driver whose
tests need a live Fortnox account is a driver nobody runs.

What is asserted here is the part that is easy to get subtly wrong:

* a partially paid invoice is worth what is left, not what was billed,
* a missing invoice date is not age zero,
* a 429 and a 401 are different things and neither is "not answering",
* a source without invoice dates produces no ageing at all.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..services import api_transport, fortnox, raw_data, visma


class FakeResponse(object):
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeSession(object):
    """Records calls and replays a queue of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "params": params,
             "json": json, "headers": dict(self.headers)}
        )
        if not self.responses:
            return FakeResponse(200, {})
        return self.responses.pop(0)


def _credential(**overrides):
    credential = {
        "client_id": "client-1",
        "client_secret": "secret",
        "access_token": "token",
        "refresh_token": "refresh",
        "expires_at": 9999999999,
    }
    credential.update(overrides)
    return credential


class TestFortnoxParsing(TransactionCase):
    """6.1 -- recorded answers give the expected raw data."""

    def _driver(self, responses):
        return fortnox.FortnoxDriver(
            _credential(),
            session=FakeSession(responses),
            limiter=api_transport.RateLimiter(min_interval=0),
        )

    def test_recorded_answer_gives_expected_raw(self):
        """The three endpoints answer; the sections carry the right totals."""
        accounts = {"Accounts": [
            {"Number": 1930, "Description": "Företagskonto",
             "BalanceCarriedForward": "125000.50"},
            {"Number": 2440, "Description": "Leverantörsskulder",
             "BalanceCarriedForward": "-30000"},
        ]}
        invoices = {"Invoices": [
            {"DocumentNumber": 11, "CustomerName": "Kund A",
             "Balance": "1000", "Total": "1250",
             "InvoiceDate": "2026-10-01", "DueDate": "2026-10-31"},
            {"DocumentNumber": 12, "CustomerName": "Kund B",
             "Balance": "500", "Total": "500",
             "InvoiceDate": "2026-10-15", "DueDate": "2026-11-14"},
        ]}
        supplier = {"SupplierInvoices": [
            {"DocumentNumber": 21, "SupplierName": "Lev X",
             "Balance": "2000", "InvoiceDate": "2026-10-05",
             "DueDate": "2026-11-04"},
        ]}

        driver = self._driver([
            FakeResponse(200, accounts),
            FakeResponse(200, invoices),
            FakeResponse(200, supplier),
        ])
        raw = driver.fetch_raw()

        self.assertTrue(raw.has(raw_data.SECTION_BALANCES))
        self.assertEqual(len(raw.items(raw_data.SECTION_BALANCES)), 2)
        self.assertTrue(raw.has(raw_data.SECTION_RECEIVABLES))
        self.assertEqual(len(raw.items(raw_data.SECTION_RECEIVABLES)), 2)
        self.assertTrue(raw.has(raw_data.SECTION_PAYABLES))

        computed = raw_data.compute_kpis(raw)
        kpis = computed["kpis"]
        # Balance is preferred over Total: a partially paid invoice is worth
        # what is left, not what was billed.
        self.assertAlmostEqual(kpis["receivables_open"], 1500.0)
        self.assertAlmostEqual(kpis["payables_open"], 2000.0)
        self.assertAlmostEqual(kpis["net_open"], -500.0)

    def test_bank_accounts_come_from_account_19xx(self):
        accounts = {"Accounts": [
            {"Number": 1930, "Description": "Företagskonto",
             "BalanceCarriedForward": "50000"},
            {"Number": 1931, "Description": "Plusgiro",
             "BalanceCarriedForward": "10000"},
            {"Number": 2440, "Description": "Leverantörsskulder",
             "BalanceCarriedForward": "-5000"},
        ]}
        driver = self._driver([
            FakeResponse(200, accounts),
            FakeResponse(200, {"Invoices": []}),
            FakeResponse(200, {"SupplierInvoices": []}),
        ])
        raw = driver.fetch_raw()
        banks = raw.items(raw_data.SECTION_BANK)
        self.assertEqual(len(banks), 2)
        computed = raw_data.compute_kpis(raw)
        # Bank accounts win over the whole account list for the cash position:
        # 2440 is a liability, not cash.
        self.assertAlmostEqual(computed["kpis"]["cash_position"], 60000.0)

    def test_missing_invoice_date_is_not_age_zero(self):
        """A missing date must not read as 'freshly invoiced'.

        This is the most flattering possible reading of missing data, and it is
        exactly what a naive ``(today - date).days`` would produce.
        """
        self.assertIsNone(fortnox._age_days(None))
        self.assertIsNone(fortnox._age_days(""))
        self.assertIsNone(fortnox._age_days("not-a-date"))

    def test_ageing_needs_invoice_dates(self):
        """A source without invoice dates produces no ageing at all.

        6.4: a KPI the source cannot carry is absent, never zero.
        """
        raw = raw_data.RawLiquidityData(source_type="test")
        raw.capabilities = {"balances", "receivables"}
        raw.set_section(raw_data.SECTION_BALANCES, raw_data.OK,
                        [{"account": "1930", "balance": 1000.0}])
        raw.set_section(raw_data.SECTION_RECEIVABLES, raw_data.OK,
                        [{"amount": 500.0, "age_days": None}])
        computed = raw_data.compute_kpis(raw)
        self.assertNotIn("receivables_ageing", computed["kpis"])
        self.assertNotIn("receivables_ageing",
                         raw_data.supported_kpis(raw.capabilities))


class TestFortnoxFailures(TransactionCase):
    """6.2 -- rate limits and expired tokens are not 'not answering'."""

    def _driver(self, responses, sleeps=None):
        return fortnox.FortnoxDriver(
            _credential(),
            session=FakeSession(responses),
            limiter=api_transport.RateLimiter(min_interval=0),
            sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        )

    def test_rate_limit_is_retried_then_reported_as_limited(self):
        """A 429 is not a failure: it is 'ask again', and it says so."""
        sleeps = []
        driver = self._driver(
            [FakeResponse(429, {}, {"Retry-After": "0"})] * 3,
            sleeps=sleeps,
        )
        result = driver._get("/accounts")
        self.assertEqual(result.outcome, api_transport.RATE_LIMITED)
        # Bounded: it retried, then stopped. It did not loop.
        self.assertEqual(len(sleeps), api_transport.MAX_RATE_LIMIT_RETRIES)

    def test_rate_limit_does_not_become_failed_section(self):
        """The row must not say 'not answering' when the quota was hit."""
        driver = self._driver([FakeResponse(429, {}, {"Retry-After": "0"})] * 3)
        raw = driver.fetch_raw()
        self.assertNotIn(
            raw_data.SECTION_BALANCES,
            [n for n in raw_data.SECTIONS
             if raw.status.get(n) == raw_data.FAILED],
        )
        self.assertIn(raw_data.RATE_LIMITED, raw.status.values())

    def test_expired_token_is_its_own_outcome(self):
        """401 means renew the connection, not 'the source is down'."""
        driver = self._driver([FakeResponse(401, {})])
        result = driver._get("/accounts")
        self.assertEqual(result.outcome, api_transport.AUTH_EXPIRED)
        self.assertIn("renew", result.message.lower())

    def test_expired_token_refreshes_and_retries(self):
        """A dead access token is refreshed, not surfaced as a failure."""
        driver = fortnox.FortnoxDriver(
            _credential(expires_at=1),
            session=FakeSession([
                FakeResponse(200, {"access_token": "new", "refresh_token": "r2",
                                   "expires_in": 3600}),
                FakeResponse(200, {"Accounts": []}),
                FakeResponse(200, {"Invoices": []}),
                FakeResponse(200, {"SupplierInvoices": []}),
            ]),
            limiter=api_transport.RateLimiter(min_interval=0),
        )
        raw = driver.fetch_raw()
        self.assertNotIn(raw_data.AUTH_EXPIRED, raw.status.values())
        self.assertIsNotNone(driver.refreshed_credential)
        self.assertEqual(driver.refreshed_credential["access_token"], "new")

    def test_failed_refresh_is_auth_expired_not_failed(self):
        driver = fortnox.FortnoxDriver(
            _credential(expires_at=1),
            session=FakeSession([FakeResponse(401, {})]),
            limiter=api_transport.RateLimiter(min_interval=0),
        )
        raw = driver.fetch_raw()
        self.assertEqual(raw.status[raw_data.SECTION_BALANCES],
                         raw_data.AUTH_EXPIRED)


class TestPartialAnswers(TransactionCase):
    """6.3 -- a partial answer is half a row with a sentence, not a blank."""

    def test_balances_answer_but_receivables_fail(self):
        accounts = {"Accounts": [
            {"Number": 1930, "Description": "Företagskonto",
             "BalanceCarriedForward": "75000"},
        ]}
        driver = fortnox.FortnoxDriver(
            _credential(),
            session=FakeSession([
                FakeResponse(200, accounts),
                FakeResponse(500, {}),          # invoices fail
                FakeResponse(200, {"SupplierInvoices": []}),
            ]),
            limiter=api_transport.RateLimiter(min_interval=0),
        )
        raw = driver.fetch_raw()

        self.assertTrue(raw.is_partial)
        self.assertFalse(raw.is_total_failure)
        self.assertTrue(raw.has(raw_data.SECTION_BALANCES))
        self.assertFalse(raw.has(raw_data.SECTION_RECEIVABLES))
        # The sentence names the missing part, in the reader's language.
        self.assertIn("kundfordringar", raw.missing_description())

        computed = raw_data.compute_kpis(raw)
        self.assertIn("cash_position", computed["kpis"])
        self.assertNotIn("receivables_open", computed["kpis"])

    def test_empty_section_is_not_a_failure(self):
        """A customer with no open invoices answered perfectly well."""
        driver = fortnox.FortnoxDriver(
            _credential(),
            session=FakeSession([
                FakeResponse(200, {"Accounts": []}),
                FakeResponse(200, {"Invoices": []}),
                FakeResponse(200, {"SupplierInvoices": []}),
            ]),
            limiter=api_transport.RateLimiter(min_interval=0),
        )
        raw = driver.fetch_raw()
        self.assertFalse(raw.is_partial)
        self.assertEqual(raw.status[raw_data.SECTION_RECEIVABLES],
                         raw_data.EMPTY)
        self.assertTrue(raw.has(raw_data.SECTION_RECEIVABLES))


class TestVismaProfiles(TransactionCase):
    """3.1/3.3 -- the product decides the capabilities, not hope."""

    def test_undeclared_product_supports_nothing(self):
        driver = visma.VismaDriver(
            {"client_id": "c", "access_token": "t"},
            session=FakeSession([]),
            limiter=api_transport.RateLimiter(min_interval=0),
        )
        self.assertEqual(driver.capabilities(), set())
        raw = driver.fetch_raw()
        self.assertEqual(raw.status[raw_data.SECTION_BALANCES],
                         raw_data.UNSUPPORTED)
        self.assertIn("produkt", raw.message.lower())

    def test_eekonomi_supports_currency_administration_does_not(self):
        eekonomi = visma.VismaDriver({"product": "eekonomi"})
        administration = visma.VismaDriver({"product": "administration"})
        self.assertIn("currency", eekonomi.capabilities())
        self.assertNotIn("currency", administration.capabilities())

    def test_administration_parses_its_own_field_names(self):
        driver = visma.VismaDriver(
            _credential(product="administration"),
            session=FakeSession([
                FakeResponse(200, {"Accounts": [
                    {"AccountNumber": 1930, "Name": "Bank",
                     "Balance": "40000"},
                ]}),
                FakeResponse(200, {"CustomerInvoices": [
                    {"InvoiceNumber": 5, "CustomerName": "Kund",
                     "RemainingAmount": "750", "Amount": "1000",
                     "InvoiceDate": "2026-09-01", "DueDate": "2026-10-01"},
                ]}),
                FakeResponse(200, {"SupplierInvoices": []}),
            ]),
            limiter=api_transport.RateLimiter(min_interval=0),
        )
        raw = driver.fetch_raw()
        computed = raw_data.compute_kpis(raw)
        # RemainingAmount wins over Amount: what is left, not what was billed.
        self.assertAlmostEqual(computed["kpis"]["receivables_open"], 750.0)
        self.assertAlmostEqual(computed["kpis"]["cash_position"], 40000.0)


class TestCapabilityGate(TransactionCase):
    """6.4 -- unsupported KPIs are absent, never a guess and never a zero."""

    def test_supported_kpis_follow_capabilities(self):
        bare = {"balances"}
        full = {"balances", "bank_accounts", "receivables", "payables",
                "invoice_dates"}
        self.assertNotIn("receivables_ageing", raw_data.supported_kpis(bare))
        self.assertIn("receivables_ageing", raw_data.supported_kpis(full))
        self.assertIn("net_open", raw_data.supported_kpis(full))

    def test_no_capabilities_means_no_figures(self):
        raw = raw_data.RawLiquidityData(source_type="test")
        raw.capabilities = set()
        raw.set_all(raw_data.UNSUPPORTED)
        computed = raw_data.compute_kpis(raw)
        self.assertEqual(computed["kpis"], {})

    def test_every_capability_maps_to_a_kpi_or_is_declared(self):
        """A capability that produces nothing is a lie in the declaration.

        This is the guard against the two lists drifting: adding a capability
        without a computation would make the view promise a number it cannot
        produce.
        """
        raw = raw_data.RawLiquidityData(source_type="test")
        raw.capabilities = set(raw_data.ALL_CAPABILITIES)
        raw.set_section(raw_data.SECTION_BALANCES, raw_data.OK,
                        [{"account": "1930", "balance": 1.0}])
        raw.set_section(raw_data.SECTION_BANK, raw_data.OK,
                        [{"name": "bank", "balance": 1.0}])
        raw.set_section(raw_data.SECTION_RECEIVABLES, raw_data.OK,
                        [{"amount": 1.0, "age_days": 10}])
        raw.set_section(raw_data.SECTION_PAYABLES, raw_data.OK,
                        [{"amount": 1.0, "age_days": 10}])
        raw.set_section("next_events", raw_data.OK,
                        [{"date": "2026-12-12", "label": "moms"}])

        computed = raw_data.compute_kpis(raw)
        supported = raw_data.supported_kpis(raw.capabilities)
        # Everything declared as supported was actually produced.
        self.assertEqual(supported - set(computed["kpis"]), set())
