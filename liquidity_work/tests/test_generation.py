# -*- coding: utf-8 -*-
import datetime

from .common import WorkCommon


class TestGeneration(WorkCommon):

    def setUp(self):
        super().setUp()
        self.plan = self.env["liquidity.work.plan"]._liquidity_create_plan(
            self.template, self.bookkeeping,
            name="Plan för Alfa",
        )
        self.plan.date_start = datetime.date(2026, 1, 1)
        self.Order = self.env["liquidity.work.order"]

    def _generate(self, today):
        return self.Order._generate_for_plan(self.plan, today=today)

    def test_one_order_per_period(self):
        """Monthly line over Jan-Mar gives three orders; quarterly gives one."""
        created = self._generate(datetime.date(2026, 3, 15))
        monthly = created.filtered(
            lambda o: o.plan_line_id == self.plan.line_ids[0]
        )
        quarterly = created.filtered(
            lambda o: o.plan_line_id == self.plan.line_ids[1]
        )
        self.assertEqual(len(monthly), 3)
        self.assertEqual(len(quarterly), 1)

    def test_deadline_uses_offset(self):
        """Offset 4 on a monthly line starting 1 Jan is due 5 Jan."""
        created = self._generate(datetime.date(2026, 1, 31))
        first = created.filtered(
            lambda o: o.period_start == datetime.date(2026, 1, 1)
            and o.plan_line_id == self.plan.line_ids[0]
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(first.date_deadline, datetime.date(2026, 1, 5))

    def test_cron_is_idempotent(self):
        """Cron runs every night. A second run must create nothing."""
        first = self._generate(datetime.date(2026, 2, 15))
        self.assertTrue(first)
        second = self._generate(datetime.date(2026, 2, 15))
        self.assertEqual(len(second), 0, "a second run must not duplicate")

    def test_unique_constraint_holds(self):
        self._generate(datetime.date(2026, 1, 31))
        order = self.Order.search([], limit=1)
        from psycopg2 import IntegrityError
        with self.assertRaises(IntegrityError):
            self.Order.create({
                "plan_id": order.plan_id.id,
                "plan_line_id": order.plan_line_id.id,
                "name": "Dubblett",
                "period_start": order.period_start,
                "date_deadline": order.date_deadline,
            })
            self.env.flush_all()

    def test_missed_period_is_visible_as_overdue(self):
        """A period that passed without being ticked off must show, not vanish."""
        created = self._generate(datetime.date(2026, 3, 15))
        jan = created.filtered(
            lambda o: o.period_start == datetime.date(2026, 1, 1)
            and o.plan_line_id == self.plan.line_ids[0]
        )
        self.assertEqual(len(jan), 1)
        self.assertTrue(jan.is_overdue, "a past period must read as overdue")
        self.assertEqual(jan.state, "planned")

    def test_nothing_is_created_for_future_periods(self):
        created = self._generate(datetime.date(2026, 1, 1))
        self.assertTrue(all(
            o.period_start <= datetime.date(2026, 1, 1) for o in created
        ))

    def test_closed_plan_generates_nothing(self):
        self.plan.action_close()
        self.Order._cron_generate_orders()
        self.assertEqual(len(self.plan.order_ids), 0)

    def test_cron_generates_for_running_plans(self):
        count = self.Order._cron_generate_orders()
        self.assertGreater(count, 0)
        self.assertTrue(self.plan.order_ids)
