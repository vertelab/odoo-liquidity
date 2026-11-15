# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    """Generates work plans when an order is confirmed.

    Mirrors ``sale_project``'s ``_timesheet_service_generation``: the plan is
    created once, at confirmation, and a re-confirmation must not create a
    second one.
    """

    _inherit = "sale.order"

    liquidity_plan_ids = fields.One2many(
        "liquidity.work.plan",
        "sale_order_id",
        string="Work plans",
        readonly=True,
    )

    def action_confirm(self):
        result = super().action_confirm()
        self._liquidity_work_generation()
        return result

    def _liquidity_work_generation(self):
        """Create a plan for every line whose product asks for one."""
        Plan = self.env["liquidity.work.plan"].sudo()

        for order in self:
            for line in order.order_line:
                product = line.product_id
                if product.liquidity_tracking != "work_plan":
                    continue
                template = product.liquidity_template_id
                if not template:
                    # The constraint on the product should have caught this.
                    # Logging rather than raising: a sale must not fail because
                    # of a configuration error discovered late.
                    _logger.warning(
                        "sale %s: product %s asks for a work plan but has no "
                        "template; no plan created",
                        order.name, product.display_name,
                    )
                    continue

                # Idempotency: confirming twice must not create a second plan.
                if Plan.search_count([("sale_line_id", "=", line.id)]):
                    continue

                company = self._liquidity_resolve_company(order, template)
                if not company:
                    _logger.warning(
                        "sale %s: no bookkeeping found for customer %s; no "
                        "work plan created",
                        order.name, order.partner_id.display_name,
                    )
                    continue

                Plan._liquidity_create_plan(
                    template, company, sale_line=line,
                )

    @api.model
    def _liquidity_resolve_company(self, order, template):
        """Which bookkeeping the plan belongs to.

        The template already points at one, and that is the normal answer: a
        template belongs to a customer's bookkeeping. If the order's customer
        has several bookkeepings in the portfolio, the template's own choice is
        used — the seller picked the product, and the product knows its plan.
        """
        return template.company_id
