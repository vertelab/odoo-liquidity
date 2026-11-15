# -*- coding: utf-8 -*-
from odoo import fields, models


class LiquidityVarianceContribution(models.Model):
    _name = "liquidity.variance.contribution"
    _description = "Liquidity Simulation Variance Contribution"
    _order = "variance_share desc, id"
    _rec_name = "label"

    simulation_id = fields.Many2one(
        "liquidity.simulation",
        string="Simulation",
        required=True,
        ondelete="cascade",
        index=True,
    )
    source_type = fields.Selection(
        selection=[
            ("receivable", "Receivable"),
            ("payable", "Payable"),
            ("tax", "Tax"),
            ("payroll", "Payroll"),
            ("loan", "Loan"),
            ("manual", "Manual"),
        ],
        string="Source",
        required=True,
    )
    partner_id = fields.Many2one(
        "res.partner",
        string="Partner",
        ondelete="set null",
    )
    label = fields.Char(string="Item", required=True)
    variance_share = fields.Float(
        string="Share of Uncertainty",
        digits=(5, 4),
        help="How much of the outcome's uncertainty this item explains, "
             "measured as correlation with the result — not as its share of "
             "the amount. A small but erratic invoice can outrank a large "
             "predictable one, and that is the useful answer: it says what "
             "to go and fix.",
    )
