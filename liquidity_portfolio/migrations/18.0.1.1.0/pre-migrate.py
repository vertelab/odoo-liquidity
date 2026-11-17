# -*- coding: utf-8 -*-
"""Move each tenant's single login into an account row.

Runs as a pre-migration rather than a data file because it depends on rows that
already exist in the database, not on rows the module ships.

Idempotent: the model method it calls only creates a row when the login has no
account row yet. Running the migration twice -- which happens when an upgrade
is retried -- must not produce duplicates.
"""


def migrate(cr, version):
    if not version:
        # Fresh install: there is nothing to migrate, and the models may not
        # exist yet in a form we can query.
        return

    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    Tenant = env["liquidity.tenant"]
    # The method lives on the model so the migration and the tests exercise the
    # same code. A migration that reimplements the logic is a second place for
    # it to be wrong.
    Tenant._migrate_single_account_to_accounts()
