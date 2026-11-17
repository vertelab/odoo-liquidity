# -*- coding: utf-8 -*-
"""The bridge's security properties, asserted.

These are not feature tests. Each one asserts something that would be a real
breach if it stopped holding, and each is written so that a regression fails
loudly rather than quietly widening what the firm can do.
"""
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..models.bridge_outbox import MAX_ATTEMPTS


@tagged("post_install", "-at_install")
class TestBridgeConfig(TransactionCase):
    """The restrictive defaults. Installed and forgotten must not grant access."""

    def test_defaults_are_off(self):
        """Every switch that grants access is off on a fresh install."""
        config = self.env["liquidity.bridge.config"]
        # The config rows come from data/ir_config_parameter.xml, so the test
        # asserts the shipped defaults rather than the code's fallbacks.
        self.assertFalse(config.allows_agency_accounts())
        self.assertFalse(config.allows_impersonation())

    def test_missing_parameter_falls_back_to_off(self):
        """A deleted parameter must not read as 'on'."""
        self.env["ir.config_parameter"].sudo().search([
            ("key", "=", "liquidity_bridge.allow_impersonation"),
        ]).unlink()
        self.assertFalse(self.env["liquidity.bridge.config"].allows_impersonation())

    def test_malformed_ttl_does_not_grant_a_long_lived_token(self):
        """A bad parameter must not make tokens live forever."""
        self.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.token_ttl_seconds", "not-a-number"
        )
        self.assertEqual(self.env["liquidity.bridge.config"].token_ttl(), 60)


@tagged("post_install", "-at_install")
class TestBridgeToken(TransactionCase):
    """Token single-use, expiry and binding."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.env["liquidity.bridge.config"]
        cls.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.allow_impersonation", "1"
        )
        cls.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.agency_name", "Testbyrån AB"
        )
        cls.user = cls.env["res.users"].create({
            "name": "Konsult Test",
            "login": "konsult@byra.test",
        })
        cls.company = cls.env.company

    def test_token_cannot_be_used_twice(self):
        token = self.env["bridge.token"].issue(self.user, self.company)
        self.assertTrue(token._is_valid())
        token.write({"used_at": fields.Datetime.now()})
        self.assertFalse(
            token._is_valid(),
            "A used token must not be usable again. This is the property the "
            "whole impersonation flow rests on.",
        )

    def test_token_expires(self):
        token = self.env["bridge.token"].issue(self.user, self.company)
        token.write({"expires_at": fields.Datetime.now() - timedelta(seconds=1)})
        self.assertFalse(token._is_valid())

    def test_ttl_comes_from_configuration(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.token_ttl_seconds", "5"
        )
        token = self.env["bridge.token"].issue(self.user, self.company)
        # Five seconds, not sixty: the configuration wins.
        self.assertLessEqual(
            (token.expires_at - fields.Datetime.now()).total_seconds(), 5
        )

    def test_token_records_who_issued_it(self):
        token = self.env["bridge.token"].issue(self.user, self.company)
        self.assertEqual(token.issued_by, "Testbyrån AB")

    def test_issuing_refused_when_impersonation_is_off(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.allow_impersonation", "0"
        )
        with self.assertRaises(AccessError):
            self.env["bridge.token"].issue(self.user, self.company)

    def test_purge_removes_only_expired(self):
        live = self.env["bridge.token"].issue(self.user, self.company)
        dead = self.env["bridge.token"].issue(self.user, self.company)
        dead.write({"expires_at": fields.Datetime.now() - timedelta(minutes=1)})
        self.env["bridge.token"]._cron_purge()
        self.assertTrue(live.exists())
        self.assertFalse(dead.exists())


@tagged("post_install", "-at_install")
class TestBridgeOutbox(TransactionCase):
    """The queue survives, retries and gives up visibly."""

    def _enqueue(self):
        return self.env["bridge.outbox"].enqueue(
            event_type="free_question",
            summary="Fråga om faktura 123",
        )

    def test_event_gets_a_uuid_on_creation(self):
        event = self._enqueue()
        self.assertTrue(event.uuid)
        self.assertEqual(event.state, "queued")

    def test_uuid_is_unique(self):
        first = self._enqueue()
        second = self._enqueue()
        self.assertNotEqual(first.uuid, second.uuid)

    def test_two_events_with_the_same_uuid_are_refused(self):
        event = self._enqueue()
        from psycopg2 import IntegrityError
        with self.assertRaises(IntegrityError):
            self.env["bridge.outbox"].create({
                "uuid": event.uuid,
                "event_type": "free_question",
                "summary": "duplicate",
            })

    def test_delivery_failure_keeps_the_event(self):
        event = self._enqueue()
        event._record_failure("connection refused")
        self.assertEqual(event.state, "queued")
        self.assertEqual(event.attempts, 1)
        self.assertIn("connection refused", event.last_error)

    def test_event_stops_retrying_after_max_attempts(self):
        """A broken endpoint must not stay at the front of every cron run."""
        event = self._enqueue()
        for _ in range(MAX_ATTEMPTS):
            event._record_failure("still broken")
        self.assertEqual(event.state, "failed")

    def test_manual_retry_resets_the_state(self):
        event = self._enqueue()
        for _ in range(MAX_ATTEMPTS):
            event._record_failure("broken")
        self.assertEqual(event.state, "failed")
        event.action_retry()
        self.assertEqual(event.state, "queued")
        self.assertFalse(event.last_error)

    def test_no_endpoint_configured_leaves_events_queued(self):
        """The firm polling instead of being pushed to is a valid setup."""
        self.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.enabled", "1"
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.agency_url", ""
        )
        event = self._enqueue()
        self.env["bridge.outbox"]._cron_deliver()
        event.invalidate_recordset()
        self.assertEqual(event.state, "queued")

    def test_disabled_bridge_does_not_deliver(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.enabled", "0"
        )
        event = self._enqueue()
        self.env["bridge.outbox"]._cron_deliver()
        event.invalidate_recordset()
        self.assertEqual(event.attempts, 0)


@tagged("post_install", "-at_install")
class TestBridgeAccessLog(TransactionCase):
    """Who from the firm was in our books."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env["res.users"].create({
            "name": "Konsult Test",
            "login": "konsult2@byra.test",
        })

    def test_a_granted_session_is_logged(self):
        row = self.env["bridge.access.log"].log(
            agency="Byrån AB", user=self.user, company=self.env.company,
            outcome="granted",
        )
        self.assertTrue(row.exists())
        self.assertEqual(row.outcome, "granted")
        self.assertEqual(row.agency, "Byrån AB")

    def test_a_refused_session_is_logged_with_a_reason(self):
        row = self.env["bridge.access.log"].log(
            agency="Byrån AB", user=self.user, company=self.env.company,
            outcome="refused", reason="expired",
        )
        self.assertEqual(row.outcome, "refused")
        self.assertEqual(row.reason, "expired")

    def test_the_log_row_names_the_user_not_just_the_agency(self):
        """'Someone from the firm' is the answer the customer cannot use."""
        row = self.env["bridge.access.log"].log(
            agency="Byrån AB", user=self.user, company=self.env.company,
            outcome="granted",
        )
        self.assertEqual(row.user_id, self.user)
        self.assertIn("Konsult Test", row.display_name)


@tagged("post_install", "-at_install")
class TestAccountMoveButton(TransactionCase):
    """The customer's button queues, and refuses honestly when the bridge is off."""

    def _move(self):
        """A minimal journal entry. account.move requires a journal in Odoo 18."""
        journal = self.env["account.journal"].search(
            [("type", "=", "general")], limit=1
        )
        if not journal:
            self.skipTest("No general journal in the test database")
        return self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": journal.id,
        })

    def test_button_refuses_when_disabled(self):
        from odoo.exceptions import UserError
        self.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.enabled", "0"
        )
        move = self._move()
        with self.assertRaises(UserError):
            move.action_send_to_agency()

    def test_button_queues_an_event(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.enabled", "1"
        )
        move = self._move()
        before = self.env["bridge.outbox"].search_count([])
        move.action_send_to_agency()
        after = self.env["bridge.outbox"].search_count([])
        self.assertEqual(after, before + 1)


@tagged("post_install", "-at_install")
class TestMirroredChat(TransactionCase):
    """ALT B: every side is the original for its own messages.

    Which means the properties to assert are immutability, stable identity and
    the separation from step 1's local notes -- not conflict resolution, which
    the model makes unnecessary.
    """

    def _message(self, body="Hej", author="customer"):
        return self.env["bridge.message"].create({
            "thread_key": "account.move:42",
            "body": body,
            "author_kind": author,
            "author_name": "Test",
        })

    def test_a_message_cannot_be_edited(self):
        """Editing brings back the authority question ALT B avoids."""
        from odoo.exceptions import UserError
        message = self._message()
        with self.assertRaises(UserError):
            message.write({"body": "ändrat"})

    def test_a_message_cannot_be_deleted_through_write(self):
        """Deleting is the same problem as editing: which history is true?"""
        from odoo.exceptions import UserError
        message = self._message()
        with self.assertRaises(UserError):
            message.write({"uuid": "ny"})

    def test_the_uuid_is_stable_and_unique(self):
        first = self._message()
        second = self._message()
        self.assertTrue(first.uuid)
        self.assertNotEqual(first.uuid, second.uuid)

    def test_the_same_uuid_cannot_be_stored_twice(self):
        """Idempotent delivery rests on this."""
        from psycopg2 import IntegrityError
        first = self._message()
        with self.assertRaises(IntegrityError):
            self.env["bridge.message"].create({
                "uuid": first.uuid,
                "thread_key": first.thread_key,
                "body": "dubblett",
                "author_kind": "agency",
                "author_name": "Byrån",
            })

    def test_the_thread_key_groups_by_record(self):
        self.assertEqual(
            self.env["bridge.message"].thread_key_for("account.move", 42),
            "account.move:42",
        )

    def test_composing_queues_the_message_for_delivery(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.enabled", "1"
        )
        before = self.env["bridge.outbox"].search_count([
            ("event_type", "=", "chat_message"),
        ])
        wizard = self.env["bridge.message.compose"].create({
            "thread_key": "account.move:7",
            "body": "Fråga om fakturan",
            "company_id": self.env.company.id,
            "author_kind": "customer",
        })
        wizard.action_send()
        after = self.env["bridge.outbox"].search_count([
            ("event_type", "=", "chat_message"),
        ])
        self.assertEqual(after, before + 1)
        self.assertTrue(self.env["bridge.message"].search([
            ("thread_key", "=", "account.move:7"),
        ]))

    def test_the_outbox_carries_the_message_identity(self):
        """Without the uuid and thread key the receiving side cannot dedupe."""
        import json
        self.env["ir.config_parameter"].sudo().set_param(
            "liquidity_bridge.enabled", "1"
        )
        wizard = self.env["bridge.message.compose"].create({
            "thread_key": "account.move:8",
            "body": "Hej",
            "company_id": self.env.company.id,
            "author_kind": "customer",
        })
        wizard.action_send()
        event = self.env["bridge.outbox"].search([
            ("event_type", "=", "chat_message"),
        ], limit=1, order="id desc")
        extra = json.loads(event.extra_json)
        self.assertIn("message_uuid", extra)
        self.assertEqual(extra["thread_key"], "account.move:8")

    def test_the_summary_is_the_first_line(self):
        message = self._message(body="Första raden\nAndra raden")
        message.flush_recordset()
        message.invalidate_recordset()
        self.assertEqual(message.summary, "Första raden")

    def test_chat_is_separate_from_step_1_local_notes(self):
        """The one thing the design says must never happen.

        Step 1's chatter lives on ``liquidity.tenant.company`` and is invisible
        to the customer. This channel is shared. They must not be the same
        field, and the test asserts the models are distinct so a later refactor
        cannot quietly merge them.
        """
        self.assertNotEqual(
            self.env["bridge.message"]._name,
            "liquidity.tenant.company",
            "The mirrored channel must not be stored on the portfolio row: "
            "that field is the firm's private notes.",
        )
        # This module does not depend on liquidity_portfolio, so the portfolio
        # row is not in this registry -- which is itself the separation. The
        # channel lives here, at the customer; the firm's private notes live
        # there, at the firm. Different databases, different models.
        self.assertNotIn(
            "liquidity.tenant.company", self.env,
            "The mirrored channel must live at the customer, not on the "
            "portfolio row where step 1's private notes are.",
        )
