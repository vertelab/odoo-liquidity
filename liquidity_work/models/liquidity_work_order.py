# -*- coding: utf-8 -*-
import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

INTERVAL_DELTA = {
    "day": lambda n: relativedelta(days=n),
    "week": lambda n: relativedelta(weeks=n),
    "month": lambda n: relativedelta(months=n),
    "year": lambda n: relativedelta(years=n),
}


class LiquidityWorkOrder(models.Model):
    """One period of one plan line: the thing a person actually does."""

    _name = "liquidity.work.order"
    _description = "Work order"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_deadline asc, id asc"
    _rec_name = "name"

    name = fields.Char(string="Work order", required=True, tracking=True)
    plan_id = fields.Many2one(
        "liquidity.work.plan",
        string="Plan",
        required=True,
        ondelete="cascade",
        index=True,
    )
    plan_line_id = fields.Many2one(
        "liquidity.work.plan.line",
        string="Plan line",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        "liquidity.tenant.company",
        string="Bookkeeping",
        related="plan_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    description = fields.Html(
        string="Description",
        help="Copied from the plan line when the order is created, so the "
             "instructions are the ones that applied when the work was due.",
    )
    description_excerpt = fields.Char(
        string="Description (text)",
        compute="_compute_description_excerpt",
        store=True,
        help="Plain text for the list view. Raw HTML there would show "
             "<ul><li> to the user.",
    )
    period_start = fields.Date(
        string="Period",
        required=True,
        readonly=True,
        help="The first day of the period this order covers. Together with the "
             "plan and the plan line it is the idempotency key: cron runs every "
             "night and must never create a second order for the same period.",
    )
    date_deadline = fields.Date(
        string="Due",
        required=True,
        tracking=True,
        help="Drives the calendar view and the agency's agenda.",
    )
    is_all_day = fields.Boolean(
        string="All day",
        default=True,
        help="A work order is a day, not an hour. The calendar view needs a "
             "field to read this from; it cannot take a literal.",
    )
    state = fields.Selection(
        selection=[
            ("planned", "Planned"),
            ("in_progress", "In progress"),
            ("done", "Done"),
            ("skipped", "Skipped"),
        ],
        string="Status",
        default="planned",
        required=True,
        tracking=True,
    )
    responsible_role = fields.Char(string="Responsible role")
    user_id = fields.Many2one(
        "res.users",
        string="Assigned to",
        tracking=True,
        help="The person who will do it. A role alone is not enough — someone "
             "has to actually do the work.",
    )
    done_at = fields.Datetime(string="Completed on", readonly=True)
    done_by_id = fields.Many2one(
        "res.users", string="Completed by", readonly=True
    )
    is_overdue = fields.Boolean(
        string="Overdue",
        compute="_compute_is_overdue",
        store=True,
        help="Due date passed and the work is not done. A missed period must be "
             "visible, never silently skipped.",
    )
    estimated_minutes = fields.Float(string="Estimated minutes")
    timesheet_ids = fields.One2many(
        "liquidity.work.timesheet", "order_id", string="Time"
    )
    minutes_spent = fields.Integer(
        string="Minutes spent", compute="_compute_minutes_spent", store=True
    )

    _sql_constraints = [
        (
            "order_period_uniq",
            "unique(plan_id, plan_line_id, period_start)",
            "One work order per plan line and period.",
        ),
    ]

    @api.depends("description")
    def _compute_description_excerpt(self):
        for order in self:
            order.description_excerpt = self.env[
                "liquidity.work.template.line"
            ]._excerpt(order.description)

    @api.depends("date_deadline", "state")
    def _compute_is_overdue(self):
        today = fields.Date.context_today(self)
        for order in self:
            order.is_overdue = bool(
                order.date_deadline
                and order.date_deadline < today
                and order.state in ("planned", "in_progress")
            )

    @api.depends("timesheet_ids", "timesheet_ids.minutes")
    def _compute_minutes_spent(self):
        for order in self:
            order.minutes_spent = int(sum(order.timesheet_ids.mapped("minutes")))

    # -- actions -----------------------------------------------------------

    def action_start(self):
        self.write({"state": "in_progress"})
        return True

    def action_done(self):
        """Mark done, with who and when.

        The timesheet entry is a convenience for the common case: the person
        ticks it off and says how long it took. It is not a requirement.
        """
        self.write({
            "state": "done",
            "done_at": fields.Datetime.now(),
            "done_by_id": self.env.user.id,
        })
        return True

    def action_skip(self):
        self.write({"state": "skipped"})
        return True

    def action_reset(self):
        self.write({
            "state": "planned",
            "done_at": False,
            "done_by_id": False,
        })
        return True

    # -- generation --------------------------------------------------------

    @api.model
    def _period_start(self, anchor, line, offset):
        """The period start ``offset`` intervals after ``anchor``."""
        delta = INTERVAL_DELTA.get(line.interval_type)
        if not delta:
            return anchor
        return anchor + delta(line.interval_number * offset)

    def _generate_for_plan(self, plan, today=None):
        """Create the work orders a plan is due for, up to ``today``.

        Walks forward from the plan's start in interval steps. For each step it
        creates the order if it does not exist, and stops at the first step
        whose period has not started yet.

        A missed period is created too — with a past due date — so it shows as
        overdue. Skipping it would hide that the work was not done.
        """
        today = today or fields.Date.context_today(self)
        Order = self.sudo()
        created = Order.browse()

        for line in plan.line_ids:
            anchor = plan.date_start
            step = 0
            # A plan running for years must not generate thousands of orders in
            # one cron pass; the cap is generous but finite.
            while step < 240:
                period_start = self._period_start(anchor, line, step)
                if period_start > today:
                    break
                exists = Order.search_count([
                    ("plan_id", "=", plan.id),
                    ("plan_line_id", "=", line.id),
                    ("period_start", "=", period_start),
                ])
                if not exists:
                    deadline = period_start + relativedelta(
                        days=line.offset_days or 0
                    )
                    created |= Order.create({
                        "plan_id": plan.id,
                        "plan_line_id": line.id,
                        "name": "%s — %s" % (line.name, period_start),
                        "description": line.description,
                        "period_start": period_start,
                        "date_deadline": deadline,
                        "responsible_role": line.responsible_role,
                        "estimated_minutes": line.estimated_minutes,
                    })
                step += 1
        return created

    @api.model
    def _cron_generate_orders(self):
        """Nightly generation, driven by cron_tick."""
        plans = self.env["liquidity.work.plan"].sudo().search([
            ("state", "=", "running"),
        ])
        total = 0
        for plan in plans:
            today = fields.Date.context_today(self)
            if plan.date_end and plan.date_end < today:
                continue
            total += len(self._generate_for_plan(plan, today=today))
        _logger.info("liquidity_work: %s work orders generated", total)
        return total
