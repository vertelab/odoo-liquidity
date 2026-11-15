# -*- coding: utf-8 -*-
from odoo import fields, models


class LiquiditySimulationLine(models.Model):
    _name = "liquidity.simulation.line"
    _description = "Liquidity Simulation Daily Percentile"
    _order = "date asc, id asc"

    simulation_id = fields.Many2one(
        "liquidity.simulation",
        string="Simulation",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        related="simulation_id.company_id",
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        related="simulation_id.currency_id",
        readonly=True,
    )
    date = fields.Date(string="Date", required=True, index=True)

    p10 = fields.Monetary(
        string="P10",
        currency_field="currency_id",
        help="One iteration in ten ends below this balance. The pessimistic "
             "edge of the band.",
    )
    p50 = fields.Monetary(
        string="P50",
        currency_field="currency_id",
        help="The median outcome. A midline, not a promise.",
    )
    p90 = fields.Monetary(
        string="P90",
        currency_field="currency_id",
        help="Nine iterations in ten end above this balance. The optimistic "
             "edge of the band.",
    )
    p95 = fields.Monetary(string="P95", currency_field="currency_id")
    p99 = fields.Monetary(
        string="P99",
        currency_field="currency_id",
        help="Computed for completeness. No decision should lean on it — it "
             "is the tail, and the tail is where the model is weakest.",
    )
    p_negative = fields.Float(
        string="P(negative)",
        digits=(5, 4),
        help="Share of iterations with a negative balance this day.",
    )
    baseline_balance = fields.Monetary(
        string="Baseline",
        currency_field="currency_id",
        help="The deterministic forecast's closing balance the same day. "
             "Kept for comparison — the band must cover it, and if it does "
             "not, the distributions are wrong.",
    )
    expected_events = fields.Char(
        string="Events",
        help="Event markers (tax, payroll, loan) landing on this day, so a "
             "dip in the band has a visible explanation.",
    )

    _sql_constraints = [
        (
            "simulation_date_uniq",
            "unique(simulation_id, date)",
            "A simulation can only have one line per day.",
        ),
    ]
