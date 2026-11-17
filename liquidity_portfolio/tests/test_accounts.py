# -*- coding: utf-8 -*-
"""The account model: one consultant, one credential, and a log that names them.

The tests here assert the two properties the change exists for:

* a firm with several consultants has several accounts, not one shared login,
* revoking one consultant touches nobody else.
"""
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestTenantAccount(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env["liquidity.tenant"].create({
            "name": "Kund X",
            "base_url": "https://kund-x.test",
            "db_name": "kundx",
            "login": "byra@kund-x.test",
            "keykeep_ref": "kund-x-shared",
        })
        cls.anders = cls.env["res.users"].create({
            "name": "Anders", "login": "anders@byra.test",
        })
        cls.sara = cls.env["res.users"].create({
            "name": "Sara", "login": "sara@byra.test",
        })

    def _account(self, user, ref, login=None):
        return self.env["liquidity.tenant.account"].create({
            "tenant_id": self.tenant.id,
            "consultant_id": user.id,
            "login": login or user.login,
            "keykeep_ref": ref,
        })

    def test_two_consultants_get_two_accounts(self):
        """The situation the change exists for: eight consultants, eight keys."""
        self._account(self.anders, "kund-x-anders")
        self._account(self.sara, "kund-x-sara")
        self.assertEqual(len(self.tenant.account_ids), 2)
        self.assertEqual(
            len({a.keykeep_ref for a in self.tenant.account_ids}), 2,
            "Each consultant must have their own credential, not a shared one.",
        )

    def test_login_is_unique_per_tenant(self):
        self._account(self.anders, "ref-1", login="same@login.test")
        from psycopg2 import IntegrityError
        with self.assertRaises(IntegrityError):
            self._account(self.sara, "ref-2", login="same@login.test")

    def test_the_same_login_at_two_customers_is_fine(self):
        """Uniqueness is per source, not global."""
        other = self.env["liquidity.tenant"].create({
            "name": "Kund Y",
            "base_url": "https://kund-y.test",
            "db_name": "kundy",
            "login": "byra@kund-y.test",
        })
        self._account(self.anders, "ref-1", login="anders@byra.test")
        self.env["liquidity.tenant.account"].create({
            "tenant_id": other.id,
            "consultant_id": self.anders.id,
            "login": "anders@byra.test",
            "keykeep_ref": "kund-y-anders",
        })
        self.assertEqual(len(other.account_ids), 1)

    def test_two_accounts_sharing_one_credential_is_refused(self):
        """A shared key is the defect, so it is a constraint rather than a habit."""
        self._account(self.anders, "shared-ref")
        with self.assertRaises(ValidationError):
            self._account(self.sara, "shared-ref", login="sara-other@byra.test")

    def test_revoking_one_consultant_leaves_the_other_working(self):
        anders = self._account(self.anders, "kund-x-anders")
        sara = self._account(self.sara, "kund-x-sara")

        anders.action_revoke()

        self.assertFalse(anders.active)
        self.assertTrue(sara.active)
        # The revoked row stays, so an old access log entry still resolves.
        self.assertTrue(anders.with_context(active_test=False).exists())

    def test_the_read_path_picks_the_consultants_own_account(self):
        mine = self._account(self.anders, "kund-x-anders")
        self._account(self.sara, "kund-x-sara")
        self.assertEqual(self.tenant._get_account_for(self.anders), mine)

    def test_the_read_path_falls_back_when_no_account_belongs_to_the_user(self):
        """An unmapped user still reads, but not as somebody else."""
        first = self._account(self.anders, "kund-x-anders")
        self.assertEqual(self.tenant._get_account_for(self.sara), first)

    def test_migration_creates_one_account_per_tenant(self):
        """7.5: an existing tenant gains an account row, not a second login."""
        created = self.tenant._migrate_single_account_to_accounts()
        self.assertEqual(len(created), 1)
        self.assertEqual(created.login, "byra@kund-x.test")
        self.assertEqual(created.keykeep_ref, "kund-x-shared")
        # The old model could not say whose account it was, so the migration
        # must not claim it was the firm's.
        self.assertFalse(created.is_agency_account)

    def test_migration_is_idempotent(self):
        """A retried upgrade must not double the accounts."""
        self.tenant._migrate_single_account_to_accounts()
        self.tenant._migrate_single_account_to_accounts()
        self.assertEqual(
            len(self.tenant.account_ids.filtered(
                lambda a: a.login == "byra@kund-x.test"
            )), 1,
        )

    def test_migration_skips_a_tenant_without_login(self):
        # A driven source has no Odoo login at all -- that is why there is
        # nothing to migrate.
        bare = self.env["liquidity.tenant"].create({
            "name": "Kund Z",
            "base_url": "https://kund-z.test",
            "source_type": "fortnox",
        })
        bare._migrate_single_account_to_accounts()
        # The method migrates every tenant, so the assertion is about *this*
        # tenant: a driven source has no Odoo login and must gain no account.
        self.assertFalse(bare.account_ids)


@tagged("post_install", "-at_install")
class TestCustomerSource(TransactionCase):
    """Who the customer belongs to: Vertel or an agency."""

    def test_default_is_vertel(self):
        tenant = self.env["liquidity.tenant"].create({
            "name": "Kund", "base_url": "https://k.test", "db_name": "k",
            "login": "byra@k.test",
        })
        self.assertEqual(tenant.source, "vertel")

    def test_an_agency_customer_records_the_agency(self):
        agency = self.env["res.partner"].create({"name": "Byrån AB"})
        tenant = self.env["liquidity.tenant"].create({
            "name": "Kund", "base_url": "https://k2.test", "db_name": "k2",
            "login": "byra@k2.test",
            "source": "agency", "owning_agency_id": agency.id,
        })
        self.assertEqual(tenant.owning_agency_id, agency)


@tagged("post_install", "-at_install")
class TestAgencyProvisioning(TransactionCase):
    """The firm's side of provisioning: what it sends, and what it does not."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env["liquidity.tenant"].create({
            "name": "Provisioneringskund",
            "base_url": "https://prov.test",
            "db_name": "prov",
            "login": "byra@prov.test",
            "bridge_keykeep_ref": "prov-bridge-key",
        })

    def test_provisioning_sends_only_a_login_and_a_reference(self):
        """The signature is the threat model: what is not sent cannot be asked for."""
        from unittest.mock import patch
        captured = {}

        def fake_call(self, path, payload=None, method="GET"):
            captured["path"] = path
            captured["payload"] = payload
            return {"ok": True, "data": {"status": "created"}}

        with patch.object(type(self.tenant), "_bridge_call", fake_call):
            result = self.tenant._provision_agency_account(
                "konsult@byra.test", consultant_ref="k-1"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["path"], "/liquidity_bridge/account/provision")
        self.assertEqual(
            set(captured["payload"]), {"login", "consultant_ref"},
            "The provisioning call must carry no group, no password and no "
            "rights: the customer's bridge decides the group.",
        )

    def test_a_refusal_is_reported_not_raised_as_success(self):
        from unittest.mock import patch

        with patch.object(
            type(self.tenant), "_bridge_call",
            return_value={"ok": False, "reason": "already exists"},
        ):
            result = self.tenant._provision_agency_account("x@y.test")

        self.assertFalse(result["ok"])
        self.assertIn("already exists", result["reason"])

    def test_provisioning_marks_the_account_as_created_at_the_customer(self):
        from unittest.mock import patch
        account = self.env["liquidity.tenant.account"].create({
            "tenant_id": self.tenant.id,
            "login": "konsult2@byra.test",
            "keykeep_ref": "prov-k2",
        })
        self.assertFalse(account.provisioned_by_bridge)
        with patch.object(
            type(self.tenant), "_bridge_call",
            return_value={"ok": True, "data": {}},
        ):
            account.action_provision_at_customer()
        self.assertTrue(account.provisioned_by_bridge)

    def test_a_refused_provisioning_raises_with_the_reason(self):
        from unittest.mock import patch
        from odoo.exceptions import ValidationError
        account = self.env["liquidity.tenant.account"].create({
            "tenant_id": self.tenant.id,
            "login": "konsult3@byra.test",
            "keykeep_ref": "prov-k3",
        })
        with patch.object(
            type(self.tenant), "_bridge_call",
            return_value={"ok": False, "reason": "agency accounts are off"},
        ):
            with self.assertRaises(ValidationError) as raised:
                account.action_provision_at_customer()
        self.assertIn("agency accounts are off", str(raised.exception))

    def test_revoking_a_bridged_account_also_deactivates_it_at_the_customer(self):
        """A local revoke that leaves a working login behind is the worst of
        both worlds."""
        from unittest.mock import patch
        account = self.env["liquidity.tenant.account"].create({
            "tenant_id": self.tenant.id,
            "login": "konsult4@byra.test",
            "keykeep_ref": "prov-k4",
        })
        with patch.object(
            type(self.tenant), "_bridge_call",
            return_value={"ok": True, "data": {}},
        ):
            account.action_provision_at_customer()
        with patch.object(
            type(self.tenant), "_bridge_call",
            return_value={"ok": True, "data": {}},
        ) as mocked:
            account.action_revoke()
            self.assertTrue(mocked.called, "the customer must be told")
        self.assertFalse(account.active)

    def test_a_failed_remote_deactivation_is_recorded_not_hidden(self):
        from unittest.mock import patch
        account = self.env["liquidity.tenant.account"].create({
            "tenant_id": self.tenant.id,
            "login": "konsult5@byra.test",
            "keykeep_ref": "prov-k5",
            "provisioned_by_bridge": True,
        })
        with patch.object(
            type(self.tenant), "_bridge_call",
            return_value={"ok": False, "reason": "the bridge did not answer"},
        ):
            account.action_revoke()
        self.assertFalse(account.active)
        self.assertIn("could not be deactivated", account.bridge_note)

    def test_a_driven_source_has_no_users_to_provision(self):
        from odoo.exceptions import ValidationError
        fortnox = self.env["liquidity.tenant"].create({
            "name": "Fortnoxkund",
            "base_url": "https://fx.test",
            "source_type": "fortnox",
        })
        account = self.env["liquidity.tenant.account"].create({
            "tenant_id": fortnox.id,
            "login": "konsult@byra.test",
            "keykeep_ref": "fx-k1",
        })
        with self.assertRaises(ValidationError):
            account.action_provision_at_customer()
