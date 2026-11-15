# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ProductTemplate(models.Model):
    """The flag lives on the product, where the seller stands.

    This mirrors ``sale_project``: ``service_tracking`` decides whether a sale
    generates a project, and ``project_template_id`` says which one. Here the
    same two fields decide whether a sale generates a work plan.
    """

    _inherit = "product.template"

    liquidity_tracking = fields.Selection(
        selection=[
            ("no", "Nothing"),
            ("work_plan", "Work plan"),
        ],
        string="Create on Order",
        default="no",
        required=True,
        help="Work plan: confirming a sale order with this product creates a "
             "plan for the customer's bookkeeping, copied from the template "
             "below.",
    )
    liquidity_template_id = fields.Many2one(
        "liquidity.work.template",
        string="Work plan template",
        ondelete="restrict",
        help="The plan is a copy of this template. Editing the template later "
             "does not change plans already sold.",
    )

    @api.onchange("liquidity_tracking")
    def _onchange_liquidity_tracking(self):
        """Clear the template when the flag goes back to 'no'.

        Same behaviour as ``sale_project``'s onchange on ``service_tracking``:
        a leftover template on a product that generates nothing is a setting
        nobody can explain later.
        """
        if self.liquidity_tracking == "no":
            self.liquidity_template_id = False

    @api.constrains("liquidity_tracking", "liquidity_template_id")
    def _check_liquidity_template(self):
        for product in self:
            if (
                product.liquidity_tracking == "work_plan"
                and not product.liquidity_template_id
            ):
                raise ValidationError(_(
                    "The product %s is set to create a work plan but has no "
                    "template. Pick one, or set 'Create on Order' to Nothing.",
                    product.name,
                ))
            if product.liquidity_tracking == "no" and product.liquidity_template_id:
                raise ValidationError(_(
                    "The product %s has a work plan template but is set to "
                    "create nothing. Clear the template or set 'Create on "
                    "Order' to Work plan.",
                    product.name,
                ))
