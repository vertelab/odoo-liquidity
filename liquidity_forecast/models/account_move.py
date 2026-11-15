# -*- coding: utf-8 -*-
from odoo import api, fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    liquidity_type = fields.Selection(
        selection=[
            ("actual", "Actual"),
            ("forecast", "Forecast"),
        ],
        string="Liquidity Type",
        default="actual",
        # Deliberately *not* required. Odoo's own create pipeline builds the
        # INSERT from the field defaults it chooses to honour, and it does not
        # carry this column, so ``required=True`` turns the database column into
        # NOT NULL with no default — and every ordinary invoice then fails to
        # insert. A nullable column with a default keeps real bookkeeping
        # working while the marker stays accurate on the rows that set it.
        copy=False,
        index=True,
        help="'actual' is real bookkeeping. 'forecast' marks an entry created "
             "by the liquidity forecast engine in the pseudo-journal — it is a "
             "projection, not a posted fact, and must never be read as real "
             "movements.\n\n"
             "The forecast engine deletes only entries with liquidity_type="
             "'forecast'. Filtering on date instead would risk deleting real "
             "bookkeeping if a journal were ever mis-marked.",
    )

    liquidity_origin = fields.Reference(
        selection=[("account.move", "Journal Entry")],
        string="Liquidity Origin",
        copy=False,
        help="The source document this forecast entry was generated from, so "
             "every projected krona can be traced back. For an overdue "
             "receivable this points at the customer invoice it came from.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Stamp real bookkeeping as ``actual`` on the way in.

        The column is nullable so that Odoo's own inserts never fail, but every
        move that is not a projection is real bookkeeping and should say so —
        leaving it NULL would make "not forecast" ambiguous between "actual" and
        "whoever created this did not know about the field".
        """
        for vals in vals_list:
            if not vals.get("liquidity_type"):
                vals["liquidity_type"] = "actual"
        return super().create(vals_list)
