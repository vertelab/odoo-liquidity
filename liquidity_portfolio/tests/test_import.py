# -*- coding: utf-8 -*-
"""Company import: create, update by stable ref, mark missing, never delete."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..services import rpc_client


def _companies(*rows):
    return rpc_client.RpcResult(rpc_client.OK, value=list(rows))


class TestImport(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tenant = self.env["liquidity.tenant"].create({
            "name": "Testkund",
            "base_url": "https://example.test/",
            "db_name": "db",
            "login": "user",
            "keykeep_ref": "test-key",
            "request_timeout": 3,
        })

    def test_base_url_is_normalised(self):
        self.assertEqual(self.tenant.base_url, "https://example.test")

    def _import(self, companies):
        with patch.object(
            type(self.tenant), "_connect"
        ) as connect, patch.object(
            rpc_client, "module_version"
        ) as version, patch(
            "odoo.addons.liquidity_portfolio.services.kpi_reader.companies",
            return_value=companies,
        ):
            client = object()
            connect.return_value = (
                client,
                rpc_client.RpcResult(rpc_client.OK, value=1),
            )
            version.return_value = rpc_client.RpcResult(
                rpc_client.OK, value="18.0.1.0.0"
            )
            self.tenant.action_connect_and_import()

    def test_import_creates_rows(self):
        self._import(_companies(
            {"id": 1, "name": "Alfa AB", "currency_id": [1, "SEK"], "active": True},
            {"id": 2, "name": "Beta AB", "currency_id": [1, "SEK"], "active": True},
        ))
        self.assertEqual(len(self.tenant.company_ids), 2)
        self.assertEqual(
            sorted(self.tenant.company_ids.mapped("company_ref")), [1, 2]
        )

    def test_reimport_does_not_duplicate(self):
        rows = _companies(
            {"id": 1, "name": "Alfa AB", "currency_id": [1, "SEK"], "active": True},
        )
        self._import(rows)
        self._import(rows)
        self.assertEqual(len(self.tenant.company_ids), 1)

    def test_rename_keeps_the_row(self):
        """A rename in the customer's system must not create a new row."""
        self._import(_companies(
            {"id": 1, "name": "Alfa AB", "currency_id": [1, "SEK"], "active": True},
        ))
        row = self.tenant.company_ids
        self._import(_companies(
            {"id": 1, "name": "Alfa Holding AB", "currency_id": [1, "SEK"], "active": True},
        ))
        self.assertEqual(len(self.tenant.company_ids), 1)
        self.assertEqual(self.tenant.company_ids, row)
        self.assertEqual(row.name, "Alfa Holding AB")

    def test_gone_company_is_marked_not_deleted(self):
        self._import(_companies(
            {"id": 1, "name": "Alfa AB", "currency_id": [1, "SEK"], "active": True},
            {"id": 2, "name": "Beta AB", "currency_id": [1, "SEK"], "active": True},
        ))
        gone = self.tenant.company_ids.filtered(lambda r: r.company_ref == 2)

        self._import(_companies(
            {"id": 1, "name": "Alfa AB", "currency_id": [1, "SEK"], "active": True},
        ))

        self.assertTrue(gone.exists(), "the row must survive")
        self.assertTrue(gone.missing_since)

    def test_returning_company_clears_missing(self):
        self._import(_companies(
            {"id": 1, "name": "Alfa AB", "currency_id": [1, "SEK"], "active": True},
            {"id": 2, "name": "Beta AB", "currency_id": [1, "SEK"], "active": True},
        ))
        self._import(_companies(
            {"id": 1, "name": "Alfa AB", "currency_id": [1, "SEK"], "active": True},
        ))
        self._import(_companies(
            {"id": 1, "name": "Alfa AB", "currency_id": [1, "SEK"], "active": True},
            {"id": 2, "name": "Beta AB", "currency_id": [1, "SEK"], "active": True},
        ))
        row = self.tenant.company_ids.filtered(lambda r: r.company_ref == 2)
        self.assertFalse(row.missing_since)

    def test_hidden_company_keeps_existing(self):
        """Hiding is reversible: re-import must not resurrect the flag."""
        self._import(_companies(
            {"id": 1, "name": "Alfa AB", "currency_id": [1, "SEK"], "active": True},
        ))
        self.tenant.company_ids.write({"active": False})
        self._import(_companies(
            {"id": 1, "name": "Alfa AB", "currency_id": [1, "SEK"], "active": True},
        ))
        self.assertFalse(self.tenant.company_ids.active)


class TestKpiReading(TransactionCase):
    """Rows without a forecast must be empty, not zero.

    A zero is a claim. '0 days to crisis' on a company that has no forecast at
    all is a fabricated fact, and it is the kind of fabrication nobody notices
    until they trust it.
    """

    def setUp(self):
        super().setUp()
        self.tenant = self.env["liquidity.tenant"].create({
            "name": "KPI-kund",
            "base_url": "https://example.test",
            "db_name": "db",
            "login": "user",
            "keykeep_ref": "k",
        })
        self.row = self.env["liquidity.tenant.company"].create({
            "tenant_id": self.tenant.id,
            "company_ref": 1,
            "name": "Alfa AB",
        })

    def _read(self, data):
        from unittest.mock import patch
        with patch.object(
            type(self.row), "_read_tenant", return_value={self.row.id: data}
        ):
            self.row.invalidate_recordset()
            self.row._compute_kpis()

    def test_no_forecast_leaves_figures_empty(self):
        self._read({"kpi_status": "no_forecast", "kpi_message": ""})
        self.assertEqual(self.row.kpi_status, "no_forecast")
        self.assertFalse(self.row.risk_level)
        self.assertEqual(self.row.crisis_kind, "unknown")
        self.assertFalse(self.row.computed_at)

    def test_unreachable_leaves_figures_empty(self):
        self._read({"kpi_status": "unreachable", "kpi_message": "timeout"})
        self.assertEqual(self.row.kpi_status, "unreachable")
        self.assertFalse(self.row.risk_level)
        self.assertEqual(self.row.crisis_kind, "unknown")

    def test_healthy_forecast_reads_zero_as_no_crisis(self):
        self._read({
            "kpi_status": "ok",
            "kpi_message": "",
            "risk_level": "good",
            "days_to_crisis": 0,
            "crisis_kind": "none_in_horizon",
            "lowest_balance": 250000.0,
            "computed_at": "2026-11-15 03:00:00",
        })
        self.assertEqual(self.row.kpi_status, "ok")
        self.assertEqual(self.row.risk_level, "good")
        self.assertEqual(self.row.days_to_crisis, 0)
        self.assertEqual(
            self.row.crisis_kind, "none_in_horizon",
            "0 must never be read as crisis today",
        )
        self.assertEqual(self.row.lowest_balance, 250000.0)

    def test_stale_keeps_figures(self):
        """state=error keeps the previous good result — show it, marked."""
        self._read({
            "kpi_status": "stale",
            "kpi_message": "last run failed",
            "risk_level": "warning",
            "days_to_crisis": 21,
            "crisis_kind": "crisis",
            "lowest_balance": -5000.0,
            "computed_at": "2026-11-14 03:00:00",
        })
        self.assertEqual(self.row.kpi_status, "stale")
        self.assertEqual(self.row.risk_level, "warning")
        self.assertEqual(self.row.days_to_crisis, 21)
