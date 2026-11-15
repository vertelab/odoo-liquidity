# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class LiquidityWorkTemplate(models.Model):
    """A reusable plan: the lines a bookkeeping's work is made of.

    A template is a plain record, not a configuration structure. A plan is
    created with ``template.copy()``, which is why editing a template later
    never changes a plan already sold — the customer bought the plan as it was.
    """

    _name = "liquidity.work.template"
    _description = "Work plan template"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    company_id = fields.Many2one(
        "liquidity.tenant.company",
        string="Bookkeeping",
        required=True,
        ondelete="cascade",
        help="The customer's bookkeeping, not the agency's own company. The "
             "agency works on the customer's books.",
    )
    line_ids = fields.One2many(
        "liquidity.work.template.line",
        "template_id",
        string="Lines",
        copy=True,
    )
    line_count = fields.Integer(
        string="Line count", compute="_compute_line_count"
    )
    active = fields.Boolean(default=True)

    @api.depends("line_ids")
    def _compute_line_count(self):
        for template in self:
            template.line_count = len(template.line_ids)


class LiquidityWorkTemplateLine(models.Model):
    """One recurring action: "reconcile the bank, every month"."""

    _name = "liquidity.work.template.line"
    _description = "Work plan template line"
    _order = "template_id, sequence, id"

    template_id = fields.Many2one(
        "liquidity.work.template",
        string="Template",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string="Action", required=True)
    description = fields.Html(
        string="Description",
        help="Rich text: a checklist, a link to the customer's routine. A "
             "single-line field would force it into one line.",
    )
    description_excerpt = fields.Char(
        string="Description (text)",
        compute="_compute_description_excerpt",
        store=True,
        help="Plain text from the HTML, truncated. A list view showing raw "
             "HTML would show <ul><li> to the user.",
    )
    interval_number = fields.Integer(
        string="Every", default=1, required=True,
        help="How many units between two work orders.",
    )
    interval_type = fields.Selection(
        selection=[
            ("day", "Days"),
            ("week", "Weeks"),
            ("month", "Months"),
            ("year", "Years"),
        ],
        string="Unit",
        default="month",
        required=True,
    )
    offset_days = fields.Integer(
        string="Offset (days)",
        default=0,
        help="Days after the period start when the work is due. A month-end "
             "reconciliation on a monthly plan is offset 0; a report due on "
             "the 5th is offset 4.",
    )
    responsible_role = fields.Char(
        string="Responsible role",
        help="A role, not a person: the person may leave. The work order gets "
             "an assignee when someone actually takes it.",
    )
    estimated_minutes = fields.Float(
        string="Estimated minutes",
        help="Used for load planning in the calendar view.",
    )

    @api.depends("description")
    def _compute_description_excerpt(self):
        for line in self:
            line.description_excerpt = self._excerpt(line.description)

    @staticmethod
    def _excerpt(html, length=120):
        """Plain text from HTML, truncated.

        ``html2plaintext`` is Odoo's own converter and handles the tags that
        actually occur in these fields. Truncation happens after conversion, so
        a tag is never cut in half.
        """
        if not html:
            return False
        from odoo.tools import html2plaintext

        text = " ".join(html2plaintext(html).split())
        if len(text) > length:
            return text[: length - 1] + "…"
        return text
