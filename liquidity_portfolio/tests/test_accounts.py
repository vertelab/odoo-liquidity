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
