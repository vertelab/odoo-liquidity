# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = "account.journal"

    is_liquidity_forecast = fields.Boolean(
        string="Liquidity Forecast Journal",
        default=False,
        copy=False,
        help="Marks this journal as the pseudo-journal where liquidity forecast "
             "entries are stored. Entries in this journal carry "
             "liquidity_type='forecast' and are never real bookkeeping. "
             "Only one journal per company should carry this flag; the module "
             "reuses the marked journal instead of creating another.",
    )

    liquidity_forecast_account_id = fields.Many2one(
        "account.account",
        string="Forecast Account",
        copy=False,
        domain="[('account_type', '=', 'asset_current'), ('company_ids', 'in', company_id)]",
        help="The account forecast entries are booked against. It is a "
             "dedicated holding account — forecast entries must never be "
             "booked against a real result or cash account, because that would "
             "mix projections into the real ledger. Created on first use and "
             "reused afterwards.",
    )
