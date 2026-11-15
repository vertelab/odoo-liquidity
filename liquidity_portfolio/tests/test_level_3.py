# -*- coding: utf-8 -*-
"""Level 3: the curve is fetched on demand, with a longer timeout."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..services import kpi_reader, rpc_client


class TestLevel3(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tenant = self.env["liquidity.tenant"].create({
            "name": "Kurvkund",
            "base_url": "https://example.test/",
            "db_name": "db",
            "login": "user",
            "keykeep_ref": "k",
        })
        self.row = self.env["liquidity.tenant.company"].create({
            "tenant_id": self.tenant.id,
            "company_ref": 7,
            "name": "Alfa AB",
            "currency_name": "SEK",
        })

    def _with_source(self, forecast_rows, line_rows):
        """Patch the source so latest_forecast and forecast_lines answer."""
        connect = patch.object(
            type(self.tenant), "_connect",
            return_value=(object(), rpc_client.RpcResult(rpc_client.OK, value=1)),
        )
        latest = patch.object(
            kpi_reader, "latest_forecast",
            return_value=rpc_client.RpcResult(rpc_client.OK, value=forecast_rows),
        )
        lines = patch.object(
            kpi_reader, "forecast_lines",
            return_value=rpc_client.RpcResult(rpc_client.OK, value=line_rows),
        )
        return connect, latest, lines

    def test_curve_is_fetched_on_demand(self):
        connect, latest, lines = self._with_source(
            [{"id": 42, "state": "computed"}],
            [
                {"date": "2026-11-15", "opening_balance": 100.0,
                 "incoming": 0.0, "outgoing": 0.0, "closing_balance": 100.0,
                 "has_event": False, "event_type": False},
                {"date": "2026-11-16", "opening_balance": 100.0,
                 "incoming": 50.0, "outgoing": 0.0, "closing_balance": 150.0,
                 "has_event": False, "event_type": False},
            ],
        )
        with connect, latest as latest_mock, lines as lines_mock:
            data = self.row.action_load_curve()

        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["lines"]), 2)
        self.assertEqual(data["lines"][1]["closing"], 150.0)
        # The curve is fetched for the forecast that was found, never guessed.
        lines_mock.assert_called_once()
        self.assertEqual(lines_mock.call_args[0][1], 42)

    def test_no_forecast_returns_empty_curve(self):
        connect, latest, lines = self._with_source([], [])
        with connect, latest, lines:
            data = self.row.action_load_curve()
        self.assertEqual(data["status"], "no_forecast")
        self.assertEqual(data["lines"], [])

    def test_unreachable_is_reported_not_raised(self):
        with patch.object(
            type(self.tenant), "_connect",
            return_value=(
                None,
                rpc_client.RpcResult(
                    rpc_client.UNREACHABLE, message="no answer within 10 seconds"
                ),
            ),
        ):
            data = self.row.action_load_curve()
        self.assertEqual(data["status"], "unreachable")
        self.assertEqual(data["lines"], [])

    def test_login_url_carries_no_credential(self):
        self.row.invalidate_recordset()
        url = self.row.login_url
        self.assertEqual(url, "https://example.test/web/login")
        self.assertNotIn("key", url.lower())
        self.assertNotIn("token", url.lower())
        self.assertNotIn("password", url.lower())

    def test_login_url_without_source_is_false(self):
        self.tenant.base_url = False
        self.row.invalidate_recordset()
        self.assertFalse(self.row.login_url)
