# -*- coding: utf-8 -*-
"""The bridge seen from the firm's side.

The properties asserted here are about honesty rather than features: a button
that cannot work must not be shown, and the three reasons a bridge is unusable
must stay apart because they need different actions.
"""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestBridgeFromTheFirm(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env["liquidity.tenant"].create({
            "name": "Kund med brygga",
            "base_url": "https://kund.test",
            "db_name": "kund",
            "login": "byra@kund.test",
            "keykeep_ref": "kund-read-key",
            "bridge_keykeep_ref": "kund-bridge-key",
        })
        cls.row = cls.env["liquidity.tenant.company"].create({
            "tenant_id": cls.tenant.id,
            "company_ref": 1,
            "name": "Kund AB",
        })

    def _status(self, payload):
        return patch.object(
            type(self.tenant), "_bridge_call",
            return_value={"ok": True, "data": payload},
        )

    def test_no_bridge_key_means_no_button(self):
        self.tenant.bridge_keykeep_ref = False
        status = self.tenant._bridge_status()
        self.assertFalse(status["available"])
        self.assertIn("no bridge key", status["reason"])

    def test_switched_off_bridge_says_so(self):
        with self._status({"enabled": False, "allows_impersonation": True}):
            status = self.tenant._bridge_status()
        self.assertFalse(status["available"])
        self.assertIn("switched off", status["reason"])

    def test_impersonation_refused_is_its_own_reason(self):
        """A different action from 'switched off': ask the customer to allow it."""
        with self._status({"enabled": True, "allows_impersonation": False}):
            status = self.tenant._bridge_status()
        self.assertFalse(status["available"])
        self.assertIn("does not allow", status["reason"])

    def test_a_working_bridge_is_available(self):
        with self._status({"enabled": True, "allows_impersonation": True}):
            status = self.tenant._bridge_status()
        self.assertTrue(status["available"])

    def test_a_refused_key_is_reported_as_such(self):
        with patch.object(
            type(self.tenant), "_bridge_call",
            return_value={"ok": False, "reason": "the bridge key was refused"},
        ):
            status = self.tenant._bridge_status()
        self.assertFalse(status["available"])
        self.assertIn("refused", status["reason"])

    def test_the_button_refuses_without_a_bridge(self):
        self.tenant.bridge_keykeep_ref = False
        with self.assertRaises(UserError):
            self.row.action_open_at_customer()

    def test_a_driven_source_has_nothing_to_open(self):
        """A Fortnox customer has no Odoo to log in to."""
        self.tenant.write({
            "source_type": "fortnox",
            "db_name": False,
            "login": False,
        })
        self.row.invalidate_recordset()
        self.assertFalse(self.row.bridge_available)
        self.assertIn("driven", self.row.bridge_state)
        with self.assertRaises(UserError):
            self.row.action_open_at_customer()

    def test_the_button_returns_a_redirect_to_the_token_url(self):
        with self._status({"enabled": True, "allows_impersonation": True}):
            with patch.object(
                type(self.tenant), "_request_bridge_session",
                return_value={"url": "https://kund.test/liquidity_bridge/open?t=abc",
                              "reason": ""},
            ):
                action = self.row.action_open_at_customer()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertIn("/liquidity_bridge/open?t=", action["url"])
        # Same tab, not a new one: a token in two histories is two tokens.
        self.assertEqual(action["target"], "self")

    def test_using_an_account_records_when_it_was_used(self):
        # The account must belong to the user doing the opening, or the read
        # path correctly falls back to "some account" and cannot say which.
        account = self.env["liquidity.tenant.account"].create({
            "tenant_id": self.tenant.id,
            "consultant_id": self.env.user.id,
            "login": "byra@kund.test",
            "keykeep_ref": "kund-read-key",
        })
        # Only the session request is exercised here, so the mock answers that
        # one call -- a side_effect list would be consumed by whichever call
        # came first and make the test lie about what it proved.
        with patch.object(
            type(self.tenant), "_bridge_call",
            return_value={"ok": True, "data": {"url": "https://x/open?t=1"}},
        ):
            result = self.tenant._request_bridge_session(company_ref=1)
        self.assertTrue(result.get("url"), "the call must succeed: %s" % result)
        account.invalidate_recordset()
        self.assertTrue(
            account.last_used_at,
            "Opening a session must record when the account was used -- an "
            "account nobody uses is a credential somebody should revoke.",
        )
