# -*- coding: utf-8 -*-
"""The RPC client must classify, never raise.

A portfolio of two hundred customers cannot afford one bad source taking the
whole view down. Each failure mode is asserted separately because they mean
different things to the user.
"""
from unittest.mock import patch

import requests

from odoo.tests.common import TransactionCase

from ..services import rpc_client


class TestRpcClassification(TransactionCase):

    def _client(self):
        return rpc_client.OdooRpcClient(
            "https://example.test", "db", "user", "key", timeout=3
        )

    def test_timeout_is_unreachable(self):
        with patch("requests.post", side_effect=requests.exceptions.Timeout()):
            result = self._client().authenticate()
        self.assertEqual(result.outcome, rpc_client.UNREACHABLE)
        self.assertFalse(result.ok)

    def test_connection_error_is_unreachable(self):
        with patch(
            "requests.post",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            result = self._client().authenticate()
        self.assertEqual(result.outcome, rpc_client.UNREACHABLE)

    def test_http_401_is_auth_error(self):
        class Response:
            status_code = 401

            def json(self):
                return {}

        with patch("requests.post", return_value=Response()):
            result = self._client().authenticate()
        self.assertEqual(result.outcome, rpc_client.AUTH_ERROR)

    def test_false_uid_is_auth_error(self):
        """Odoo answers 200 with result=false when the key is wrong."""

        class Response:
            status_code = 200

            def json(self):
                return {"jsonrpc": "2.0", "id": 1, "result": False}

        with patch("requests.post", return_value=Response()):
            result = self._client().authenticate()
        self.assertEqual(result.outcome, rpc_client.AUTH_ERROR)

    def test_access_error_is_auth_error(self):
        error = {
            "data": {
                "name": "odoo.exceptions.AccessError",
                "message": "You are not allowed to read this",
            }
        }
        result = rpc_client.OdooRpcClient._classify_error(error)
        self.assertEqual(result.outcome, rpc_client.AUTH_ERROR)

    def test_other_error_is_rpc_error(self):
        error = {
            "data": {
                "name": "odoo.exceptions.UserError",
                "message": "something else",
            }
        }
        result = rpc_client.OdooRpcClient._classify_error(error)
        self.assertEqual(result.outcome, rpc_client.RPC_ERROR)

    def test_missing_module_is_no_liquidity(self):
        client = self._client()

        class Response:
            status_code = 200

            def json(self):
                return {"jsonrpc": "2.0", "id": 1, "result": []}

        with patch("requests.post", return_value=Response()):
            result = rpc_client.module_version(client)
        self.assertEqual(result.outcome, rpc_client.NO_LIQUIDITY)

    def test_valid_uid_returns_client(self):
        class Response:
            status_code = 200

            def json(self):
                return {"jsonrpc": "2.0", "id": 1, "result": 7}

        with patch("requests.post", return_value=Response()):
            client, result = rpc_client.connect(
                "https://example.test", "db", "user", "key"
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.value, 7)
        self.assertIsNotNone(client)
