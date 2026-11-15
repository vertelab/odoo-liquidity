# -*- coding: utf-8 -*-
from odoo import fields, models


class LiquidityForecastLine(models.Model):
    _name = "liquidity.forecast.line"
    _description = "Liquidity Forecast Daily Line"
    _order = "date asc, id asc"

    forecast_id = fields.Many2one(
        "liquidity.forecast",
        string="Forecast",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        related="forecast_id.company_id",
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        related="forecast_id.currency_id",
        readonly=True,
    )
    date = fields.Date(
        string="Date",
        required=True,
        index=True,
    )
    opening_balance = fields.Monetary(
        string="Opening Balance",
        currency_field="currency_id",
    )
    incoming = fields.Monetary(
        string="Incoming",
        currency_field="currency_id",
        help="Expected receipts this day, from open customer receivables.",
    )
    outgoing = fields.Monetary(
        string="Outgoing",
        currency_field="currency_id",
        help="Expected payments this day, from open vendor payables.",
    )
    closing_balance = fields.Monetary(
        string="Closing Balance",
        currency_field="currency_id",
    )
    has_event = fields.Boolean(
        string="Has Event",
        default=False,
        help="Reserved for the event markers introduced in phase 2 "
             "(liquidity_svensk). In phase 1 it stays False for every row — "
             "ordinary receivables and payables are not events.\n\n"
             "The field exists now because a later phase reads it to explain "
             "dips, and adding it afterwards would need a module revision plus "
             "a migration on an already-deployed system.\n\n"
             "False therefore means 'not computed yet', not 'no events exist'.",
    )
    event_type = fields.Char(
        string="Event Type",
        help="Empty when has_event is False. Never defaulted to a placeholder "
             "like 'other' — a placeholder would claim an event that does not "
             "exist.",
    )

    _sql_constraints = [
        (
            "forecast_date_uniq",
            "unique(forecast_id, date)",
            "A forecast can only have one line per day.",
        ),
    ]
