# -*- coding: utf-8 -*-
from .common import WorkCommon


class TestPlan(WorkCommon):

    def test_plan_is_a_copy_of_the_template(self):
        plan = self.env["liquidity.work.plan"]._liquidity_create_plan(
            self.template, self.bookkeeping,
        )
        self.assertEqual(len(plan.line_ids), 2)
        self.assertEqual(plan.template_id, self.template)
        self.assertEqual(plan.company_id, self.bookkeeping)

    def test_editing_the_template_does_not_change_the_plan(self):
        plan = self.env["liquidity.work.plan"]._liquidity_create_plan(
            self.template, self.bookkeeping,
        )
        self.line_month.name = "Avstämning bank (ändrad)"
        self.assertEqual(
            plan.line_ids.filtered(
                lambda line: line.template_line_id == self.line_month
            ).name,
            "Avstämning bank",
        )

    def test_plan_has_no_orders_at_creation(self):
        """Orders appear as their periods approach, not all at once."""
        plan = self.env["liquidity.work.plan"]._liquidity_create_plan(
            self.template, self.bookkeeping,
        )
        self.assertEqual(len(plan.order_ids), 0)

    def test_plan_belongs_to_a_bookkeeping(self):
        plan = self.env["liquidity.work.plan"]._liquidity_create_plan(
            self.template, self.bookkeeping,
        )
        self.assertEqual(plan.company_id._name, "liquidity.tenant.company")
