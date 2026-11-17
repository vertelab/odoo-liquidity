# -*- coding: utf-8 -*-
"""The bridge seen from the firm's side.

The properties asserted here are about honesty rather than features: a button
that cannot work must not be shown, and the three reasons a bridge is unusable
must stay apart because they need different actions.
"""
from unittest.mock import patch

from odoo import fields
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
        with self._status({"version": "18.0.1.0.0", "enabled": False, "allows_impersonation": True}):
            status = self.tenant._bridge_status()
        self.assertFalse(status["available"])
        self.assertIn("switched off", status["reason"])

    def test_impersonation_refused_is_its_own_reason(self):
        """A different action from 'switched off': ask the customer to allow it."""
        with self._status({"version": "18.0.1.0.0", "enabled": True, "allows_impersonation": False}):
            status = self.tenant._bridge_status()
        self.assertFalse(status["available"])
        self.assertIn("does not allow", status["reason"])

    def test_a_working_bridge_is_available(self):
        with self._status({"version": "18.0.1.0.0", "enabled": True, "allows_impersonation": True}):
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
        with self._status({"version": "18.0.1.0.0", "enabled": True, "allows_impersonation": True}):
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


@tagged("post_install", "-at_install")
class TestIncomingEvents(TransactionCase):
    """3.1-3.3: what the customer sent, received at the firm.

    The property that matters most is that an event the firm cannot place is
    *visible*, not dropped -- dropping it looks identical to the customer never
    writing, which is the worst failure for a feature that replaces a phone
    call.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env["liquidity.tenant"].create({
            "name": "Inkommande Kund",
            "base_url": "https://ink.test",
            "db_name": "ink",
            "login": "byra@ink.test",
        })
        cls.company = cls.env["liquidity.tenant.company"].create({
            "tenant_id": cls.tenant.id,
            "company_ref": 7,
            "name": "Inkommande AB",
        })

    def _payload(self, uuid="evt-1", company_ref=7):
        return {
            "uuid": uuid,
            "event_type": "invoice_question",
            "summary": "Fråga om faktura 123",
            "body": "Kan ni förklara?",
            "company_ref": company_ref,
            "res_reference": "INV/2026/0012",
            "sent_by": "Kundens Ekonom",
        }

    def test_an_event_for_a_known_company_is_matched(self):
        event, created = self.env["liquidity.incoming.event"].receive(
            self._payload(), tenant=self.tenant
        )
        self.assertTrue(created)
        self.assertEqual(event.state, "matched")
        self.assertEqual(event.tenant_company_id, self.company)

    def test_the_same_uuid_is_not_stored_twice(self):
        """Idempotent delivery: the customer retries, we do not duplicate."""
        first, created_first = self.env["liquidity.incoming.event"].receive(
            self._payload(), tenant=self.tenant
        )
        second, created_second = self.env["liquidity.incoming.event"].receive(
            self._payload(), tenant=self.tenant
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first, second)
        self.assertEqual(
            self.env["liquidity.incoming.event"].search_count(
                [("uuid", "=", "evt-1")]), 1,
        )

    def test_an_event_for_an_unimported_company_is_kept_as_unmatched(self):
        event, _created = self.env["liquidity.incoming.event"].receive(
            self._payload(uuid="evt-2", company_ref=99), tenant=self.tenant
        )
        self.assertEqual(event.state, "unmatched")
        self.assertFalse(event.tenant_company_id)
        self.assertIn("99", event.match_note,
                      "The note must say which company was missing.")

    def test_an_event_from_an_unknown_source_is_kept_as_unmatched(self):
        event, _created = self.env["liquidity.incoming.event"].receive(
            self._payload(uuid="evt-3"), tenant=None
        )
        self.assertEqual(event.state, "unmatched")
        self.assertIn("not registered", event.match_note)

    def test_rematch_finds_the_company_after_it_is_imported(self):
        event, _created = self.env["liquidity.incoming.event"].receive(
            self._payload(uuid="evt-4", company_ref=99), tenant=self.tenant
        )
        self.assertEqual(event.state, "unmatched")

        late = self.env["liquidity.tenant.company"].create({
            "tenant_id": self.tenant.id,
            "company_ref": 99,
            "name": "Sent Importerad AB",
        })
        event.action_rematch()
        self.assertEqual(event.state, "matched")
        self.assertEqual(event.tenant_company_id, late)

    def test_ignoring_keeps_the_event(self):
        """Ignored is a decision, not a deletion: the record stays visible."""
        event, _created = self.env["liquidity.incoming.event"].receive(
            self._payload(uuid="evt-5"), tenant=self.tenant
        )
        event.action_ignore()
        self.assertEqual(event.state, "ignored")
        self.assertTrue(event.exists())


@tagged("post_install", "-at_install")
class TestFirmSideChat(TransactionCase):
    """5.1-5.4 from the firm's side: the same message, the other database."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env["liquidity.tenant"].create({
            "name": "Chattkund",
            "base_url": "https://chatt.test",
            "db_name": "chatt",
            "login": "byra@chatt.test",
        })

    def _message(self, body="Hej från kunden", author="customer"):
        return self.env["liquidity.bridge.message"].create({
            "uuid": "msg-%s" % body[:6],
            "thread_key": "account.move:1",
            "body": body,
            "author_kind": author,
            "author_name": "Test",
            "sent_at": fields.Datetime.now(),
            "tenant_id": self.tenant.id,
        })

    def test_a_message_cannot_be_edited_on_the_firm_side_either(self):
        message = self._message()
        with self.assertRaises(UserError):
            message.write({"body": "ändrat"})

    def test_the_summary_is_derived_from_the_body(self):
        message = self._message(body="Första\nAndra")
        self.assertEqual(message.summary, "Första")

    def test_the_uuid_is_the_identity_across_the_two_databases(self):
        """The same message exists twice; the uuid is what makes them one."""
        message = self._message(body="Identitet")
        self.assertTrue(message.uuid)
        self.assertEqual(message.thread_key, "account.move:1")


@tagged("post_install", "-at_install")
class TestBridgeVersion(TransactionCase):
    """7.1: an incompatible bridge says so, instead of behaving oddly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env["liquidity.tenant"].create({
            "name": "Version",
            "base_url": "https://v.test",
            "db_name": "v",
            "login": "b@v.test",
            "bridge_keykeep_ref": "v-bridge-key",
        })

    def _status(self, payload):
        return patch.object(
            type(self.tenant), "_bridge_call",
            return_value={"ok": True, "data": payload},
        )

    def test_a_matching_version_is_supported(self):
        ok, reason = self.tenant._bridge_version_supported("18.0.1.0.0")
        self.assertTrue(ok, reason)

    def test_a_different_series_is_refused_with_a_reason(self):
        ok, reason = self.tenant._bridge_version_supported("17.0.1.0.0")
        self.assertFalse(ok)
        self.assertIn("17.0", reason)
        self.assertIn("18.0", reason,
                      "The message must say what is supported, not just that "
                      "something is wrong.")

    def test_a_missing_version_is_refused(self):
        """A bridge that cannot say what it is cannot be trusted to behave
        like anything in particular."""
        ok, reason = self.tenant._bridge_version_supported(None)
        self.assertFalse(ok)
        self.assertIn("did not report", reason)

    def test_an_unparseable_version_is_refused(self):
        ok, reason = self.tenant._bridge_version_supported("banana")
        self.assertFalse(ok)
        self.assertIn("unreadable", reason)

    def test_a_version_mismatch_hides_the_button_and_says_why(self):
        with self._status({"version": "17.0.1.0.0", "enabled": True,
                           "allows_impersonation": True}):
            status = self.tenant._bridge_status()
        self.assertFalse(status["available"])
        self.assertIn("17.0", status["reason"])

    def test_a_version_mismatch_is_not_reported_as_a_setting(self):
        """The fix is an upgrade, not a checkbox. Saying otherwise sends the
        reader to the wrong screen."""
        with self._status({"version": "17.0.1.0.0", "enabled": True,
                           "allows_impersonation": True}):
            reason = self.tenant._bridge_status()["reason"]
        self.assertNotIn("does not allow", reason)
        self.assertIn("supports", reason)
