# -*- coding: utf-8 -*-
import datetime

from .common import WorkCommon


class TestTimesheet(WorkCommon):

    def setUp(self):
        super().setUp()
        self.plan = self.env["liquidity.work.plan"]._liquidity_create_plan(
            self.template, self.bookkeeping,
        )
        self.plan.date_start = datetime.date(2026, 1, 1)
        self.order = self.env["liquidity.work.order"].create({
            "plan_id": self.plan.id,
            "plan_line_id": self.plan.line_ids[0].id,
            "name": "Avstämning bank — 2026-01",
            "period_start": datetime.date(2026, 1, 1),
            "date_deadline": datetime.date(2026, 1, 5),
        })

    def test_minutes_are_summed_on_the_order(self):
        self.env["liquidity.work.timesheet"].create([
            {"order_id": self.order.id, "minutes": 25,
             "date": datetime.date(2026, 1, 3)},
            {"order_id": self.order.id, "minutes": 15,
             "date": datetime.date(2026, 1, 4)},
        ])
        self.order.invalidate_recordset()
        self.assertEqual(self.order.minutes_spent, 40)

    def test_sum_per_bookkeeping_and_period(self):
        self.env["liquidity.work.timesheet"].create([
            {"order_id": self.order.id, "minutes": 25,
             "date": datetime.date(2026, 1, 3)},
            {"order_id": self.order.id, "minutes": 35,
             "date": datetime.date(2026, 2, 3)},
        ])
        total = self.env["liquidity.work.timesheet"]._sum_for_company_period(
            self.bookkeeping, datetime.date(2026, 1, 1), datetime.date(2026, 1, 31),
        )
        self.assertEqual(total, 25, "only January belongs to the period")

    def test_action_done_records_who_and_when(self):
        self.order.action_done()
        self.assertEqual(self.order.state, "done")
        self.assertTrue(self.order.done_at)
        self.assertEqual(self.order.done_by_id, self.env.user)
        self.assertFalse(self.order.is_overdue, "a done order is never overdue")
