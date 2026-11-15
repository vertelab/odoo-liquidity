# -*- coding: utf-8 -*-
from odoo import _, api, fields, models


class LiquidityWorkTimesheet(models.Model):
    """Minutes spent on a work order.

    This is underlying data, not an invoicing engine. The proposal says so
    explicitly, and the model keeps it that way: no product, no price, no
    invoice link.
    """

    _name = "liquidity.work.timesheet"
    _description = "Work order time"
    _order = "date desc, id desc"

    order_id = fields.Many2one(
        "liquidity.work.order",
        string="Work order",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        "liquidity.tenant.company",
        string="Bookkeeping",
        related="order_id.company_id",
        store=True,
        readonly=True,
        index=True,
        help="Stored so time can be summed per bookkeeping and period without "
             "joining through the order every time.",
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        default=lambda self: self.env.user,
        required=True,
    )
    date = fields.Date(
        string="Date",
        default=fields.Date.context_today,
        required=True,
    )
    minutes = fields.Integer(string="Minutes", required=True)
    note = fields.Char(string="Note")

    @api.constrains("minutes")
    def _check_minutes(self):
        for line in self:
            if line.minutes is not None and line.minutes < 0:
                raise ValueError(_("Minutes cannot be negative."))

    @api.model
    def _sum_for_company_period(self, company, date_from, date_to):
        """Total minutes for a bookkeeping in a period.

        Used by the summary the firm needs: how much did we actually spend on
        this customer this month.
        """
        groups = self._read_group(
            domain=[
                ("company_id", "=", company.id),
                ("date", ">=", date_from),
                ("date", "<=", date_to),
            ],
            groupby=[],
            aggregates=["minutes:sum"],
        )
        return int(groups[0][0]) if groups and groups[0][0] else 0
