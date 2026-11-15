# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round

_logger = logging.getLogger(__name__)

#: Number of days the forecast looks ahead. Fixed in phase 1 — see design.md
#: open question 1. Made configurable when a customer actually needs it.
HORIZON_DAYS = 90

#: Risk thresholds. Chosen without customer data (design.md open question 4);
#: keep as defaults, make configurable once someone says they are wrong.
CRISIS_DANGER_DAYS = 14
THIN_BALANCE_RATIO = 0.10


class LiquidityForecast(models.Model):
    _name = "liquidity.forecast"
    _description = "Liquidity Forecast"
    _order = "date_from desc, id desc"
    _rec_name = "name"

    name = fields.Char(
        string="Reference",
        required=True,
        default=lambda self: _("Liquidity forecast"),
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id",
        readonly=True,
    )
    journal_id = fields.Many2one(
        "account.journal",
        string="Forecast Journal",
        domain="[('is_liquidity_forecast', '=', True), ('company_id', '=', company_id)]",
        readonly=True,
        copy=False,
        help="The pseudo-journal where this run's forecast entries live.",
    )

    date_from = fields.Date(
        string="Start Date",
        required=True,
        default=lambda self: fields.Date.context_today(self),
    )
    date_to = fields.Date(
        string="End Date",
        compute="_compute_date_to",
        store=True,
        readonly=False,
        help="Always date_from + horizon - 1, so the number of days equals the "
             "horizon exactly.",
    )
    horizon_days = fields.Integer(
        string="Horizon (days)",
        default=HORIZON_DAYS,
        required=True,
        help="Number of days covered by the forecast. Fixed at 90 in phase 1.",
    )

    opening_balance = fields.Monetary(
        string="Opening Balance",
        currency_field="currency_id",
        help="Cash on hand at the start of the horizon. Taken from the "
             "company's cash/bank accounts when left empty, and can be "
             "overridden manually — a manual value always wins.",
    )
    opening_balance_manual = fields.Boolean(
        string="Manual Opening Balance",
        default=False,
        help="True once a user has typed an opening balance, so the automatic "
             "bank-account default does not overwrite it on the next run.",
    )

    lowest_balance = fields.Monetary(
        string="Lowest Balance",
        currency_field="currency_id",
        readonly=True,
        help="The smallest closing balance within the horizon.",
    )
    lowest_balance_date = fields.Date(
        string="Lowest Balance Date",
        readonly=True,
        help="The day the lowest balance occurs. A lowest balance without a "
             "date does not answer 'when'.",
    )
    days_to_crisis = fields.Integer(
        string="Days to Crisis",
        readonly=True,
        help="Days from the start date until the balance first goes negative.\n\n"
             "0 means 'no crisis within the horizon' — not 'crisis now'.\n"
             "A negative opening balance gives 1 (crisis on the first day), "
             "never 0.",
    )
    risk_level = fields.Selection(
        selection=[
            ("good", "Good"),
            ("warning", "Warning"),
            ("danger", "Danger"),
        ],
        string="Risk Level",
        readonly=True,
        help="danger: crisis within 14 days.\n"
             "warning: no crisis within 14 days, but the lowest balance is "
             "below 10% of the opening balance.\n"
             "good: neither.",
    )

    forecast_line_ids = fields.One2many(
        "liquidity.forecast.line",
        "forecast_id",
        string="Daily Lines",
        readonly=True,
    )
    line_count = fields.Integer(
        string="Days",
        compute="_compute_line_count",
    )

    state = fields.Selection(
        selection=[
            ("draft", "Not Computed"),
            ("computed", "Computed"),
            ("error", "Error"),
        ],
        string="Status",
        default="draft",
        required=True,
        readonly=True,
        copy=False,
        help="draft: created but not computable (typically a missing opening "
             "balance).\n"
             "computed: the curve exists and the figures match the daily "
             "lines.\n"
             "error: the last run failed. The previous good result is kept, so "
             "the user never loses the last known forecast.",
    )
    state_message = fields.Text(
        string="Status Message",
        readonly=True,
        help="Why the forecast is not computed — shown in the view, not only in "
             "the log.",
    )
    computed_at = fields.Datetime(
        string="Computed At",
        readonly=True,
        help="When the current figures were computed. Shown so the user knows "
             "how fresh the forecast is.",
    )

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends("date_from", "horizon_days")
    def _compute_date_to(self):
        for forecast in self:
            if forecast.date_from and forecast.horizon_days:
                forecast.date_to = forecast.date_from + timedelta(
                    days=forecast.horizon_days - 1
                )
            else:
                forecast.date_to = False

    @api.depends("forecast_line_ids")
    def _compute_line_count(self):
        for forecast in self:
            forecast.line_count = len(forecast.forecast_line_ids)

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------
    @api.model
    def _get_or_create_forecast_account(self, company=None, code="LQDFC00",
                                       name=None):
        """Return one of the dedicated forecast accounts for ``company``.

        Never a real result or cash account: forecast entries are projections,
        and mixing them into the real ledger would corrupt the books.

        Two accounts exist. ``LQDFC00`` (the holding account) carries the
        projected amount itself and is the only one the aggregation reads.
        ``LQDFC01`` (the counterpart) exists so the entry balances — an
        ``account.move`` must, and posting is not an option for a projection.
        Keeping the counterpart off the holding account is what lets the
        aggregation read a plain signed sum instead of a net that is zero by
        construction.
        """
        company = company or self.env.company
        name = name or (
            _("Liquidity Forecast Holding")
            if code == "LQDFC00"
            else _("Liquidity Forecast Counterpart")
        )
        Account = self.env["account.account"].sudo()
        # The code is a placeholder, not a claim on the real chart: forecast
        # entries live in their own pseudo-journal and never reach a report.
        account = Account.sudo().search(
            [
                ("code", "=", code),
                ("company_ids", "in", company.ids),
            ],
            limit=1,
        )
        if account:
            return account
        return Account.create(
            {
                "name": name,
                "code": code,
                "account_type": "asset_current",
                "company_ids": [(6, 0, company.ids)],
            }
        )

    @api.model
    def _get_or_create_forecast_journal(self, company=None):
        """Return the company's forecast pseudo-journal, creating it once.

        Reuses the journal marked ``is_liquidity_forecast`` if one exists, so
        two calls never produce two journals. The journal gets a dedicated
        holding account, so forecast entries never touch a real account.
        """
        company = company or self.env.company
        # sudo on the search too: without it a record rule can hide the existing
        # pseudo-journal, and every run would create yet another one.
        Journal = self.env["account.journal"].sudo()
        account = self._get_or_create_forecast_account(company)
        # Search on ``code`` rather than on the marker: the code is unique per
        # company and is reliably persisted at create, whereas a custom Boolean
        # can be dropped by account.journal's own create pipeline. The marker is
        # then re-applied below, so it always reflects reality.
        journal = Journal.sudo().search(
            [
                ("company_id", "=", company.id),
                ("code", "=", "LQDFC"),
            ],
            limit=1,
        )
        if journal:
            if not journal.liquidity_forecast_account_id:
                journal.liquidity_forecast_account_id = account
            return journal
        return Journal.create(
            {
                "name": _("Liquidity Forecast"),
                "code": "LQDFC",
                "type": "general",
                "company_id": company.id,
                "is_liquidity_forecast": True,
                "liquidity_forecast_account_id": account.id,
            }
        )

    # ------------------------------------------------------------------
    # Opening balance
    # ------------------------------------------------------------------
    def _default_opening_balance(self):
        """Cash on hand on date_from, summed over cash/bank accounts.

        Returns False when the company has no cash accounts — the caller must
        then leave the forecast in draft rather than invent a zero.
        """
        self.ensure_one()
        accounts = (
            self.env["account.account"]
            .sudo()
            .search(
                [
                    ("account_type", "in", ("asset_cash", "asset_current")),
                    ("company_ids", "in", self.company_id.ids),
                ]
            )
        )
        if not accounts:
            return False
        cutoff = self.date_from - timedelta(days=1)
        self.env.cr.execute(
            """
            SELECT COALESCE(SUM(aml.balance), 0.0)
              FROM account_move_line aml
              JOIN account_move am ON am.id = aml.move_id
             WHERE aml.account_id IN %s
               AND am.state = 'posted'
               AND aml.date <= %s
            """,
            (tuple(accounts.ids), cutoff),
        )
        return float_round(self.env.cr.fetchone()[0], precision_digits=2)

    # ------------------------------------------------------------------
    # Source lines
    # ------------------------------------------------------------------
    def _get_open_receivable_lines(self):
        """Open customer receivables falling inside the horizon."""
        self.ensure_one()
        return (
            self.env["account.move.line"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("account_type", "=", "asset_receivable"),
                    ("reconciled", "=", False),
                    ("amount_residual", "!=", 0.0),
                    ("parent_state", "=", "posted"),
                    ("date_maturity", ">=", self.date_from),
                    ("date_maturity", "<=", self.date_to),
                ]
            )
        )

    def _get_open_payable_lines(self):
        """Open vendor payables falling inside the horizon."""
        self.ensure_one()
        return (
            self.env["account.move.line"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("account_type", "=", "liability_payable"),
                    ("reconciled", "=", False),
                    ("amount_residual", "!=", 0.0),
                    ("parent_state", "=", "posted"),
                    ("date_maturity", ">=", self.date_from),
                    ("date_maturity", "<=", self.date_to),
                ]
            )
        )

    # ------------------------------------------------------------------
    # Engine
    # ------------------------------------------------------------------
    def _clear_forecast_entries(self):
        """Delete previous forecast entries in the pseudo-journal.

        Filters on ``liquidity_type='forecast'`` — never on date. Deleting by
        date would risk removing real bookkeeping if a journal were ever
        mis-marked.
        """
        for forecast in self:
            if not forecast.journal_id:
                continue
            moves = (
                self.env["account.move"]
                .sudo()
                .search(
                    [
                        ("journal_id", "=", forecast.journal_id.id),
                        ("liquidity_type", "=", "forecast"),
                    ]
                )
            )
            if moves:
                moves.unlink()

    def _create_forecast_move(self, amount, move_date, origin, label):
        """Create one forecast entry in the pseudo-journal.

        ``amount`` is signed from the liquidity perspective: positive for
        incoming, negative for outgoing.
        """
        self.ensure_one()
        journal = self.journal_id
        account = journal.liquidity_forecast_account_id
        counterpart = self._get_or_create_forecast_account(
            self.company_id, code="LQDFC01"
        )
        if not account:
            raise UserError(
                _(
                    "The forecast journal is not bound to a forecast account. "
                    "Recompute the forecast to let the module create one."
                )
            )
        # A move must balance, so the projection is carried by two lines on the
        # same holding account: the projected amount itself, and its counterpart
        # with the opposite sign. The amount is therefore read from the debit
        # side only, where the absolute value always sits — never from the net,
        # which is zero by construction. A note line documents the entry for
        # anyone reading the move in the UI; it carries no amount.
        lines = [
            (
                0,
                0,
                {
                    "name": label,
                    "date": move_date,
                    "account_id": account.id,
                    "debit": amount if amount > 0 else 0.0,
                    "credit": -amount if amount < 0 else 0.0,
                },
            ),
            (
                0,
                0,
                {
                    "name": _("Projection counterpart: %(ref)s", ref=origin),
                    "date": move_date,
                    "account_id": counterpart.id,
                    "debit": -amount if amount < 0 else 0.0,
                    "credit": amount if amount > 0 else 0.0,
                },
            ),
            (
                0,
                0,
                {
                    "display_type": "line_note",
                    "date": move_date,
                    "name": _(
                        "Projection only — not bookkeeping. Source: %(ref)s",
                        ref=origin,
                    ),
                },
            ),
        ]
        move = (
            self.env["account.move"]
            .sudo()
            .create(
                {
                    "journal_id": journal.id,
                    "date": move_date,
                    "ref": label,
                    "liquidity_type": "forecast",
                    "liquidity_origin": origin,
                    "line_ids": lines,
                }
            )
        )
        # A draft move drops ``date`` and derives the line dates from it, leaving
        # every line dateless. The forecast is a projection, not bookkeeping, so
        # posting it is wrong — instead the date is written back explicitly and
        # pushed onto the lines, which is what the daily aggregation reads.
        # ``create`` drops fields it does not know how to keep on a draft: the
        # date and the sign both fall back to defaults. Write them back
        # explicitly after the move exists, never relying on the create dict.
        move.write({"date": move_date})
        move.line_ids.write({"date": move_date})
        return move

    def _generate_forecast_entries(self):
        """Turn open receivables and payables into forecast entries.

        Overdue receivables keep their ``date_maturity`` — they are never moved
        forward to today. Moving them forward would make the cash position look
        better than it is; the money has not arrived, and the forecast must
        say so.
        """
        self.ensure_one()
        # Pin the pseudo-journal before anything is written. Without this the
        # first entry would create a journal of its own, and _compute_daily_lines
        # would then look in a different one and see no movement at all.
        if not self.journal_id:
            self.journal_id = self._get_or_create_forecast_journal(self.company_id)
        self._clear_forecast_entries()

        created = 0
        for line in self._get_open_receivable_lines():
            amount = abs(line.amount_residual)
            if not amount:
                continue
            label = _(
                "Forecast: %(invoice)s — %(partner)s",
                invoice=line.move_id.name or _("(no reference)"),
                partner=line.partner_id.display_name or _("(no partner)"),
            )
            self._create_forecast_move(
                amount,
                line.date_maturity,
                "account.move,%s" % line.move_id.id,
                label,
            )
            created += 1

        for line in self._get_open_payable_lines():
            # ``amount_residual`` carries the sign of the payable line itself,
            # which is negative on the credit side. Normalise to a magnitude and
            # let the projection's own sign make it outgoing; relying on the
            # stored sign would double-negate it into an incoming.
            amount = abs(line.amount_residual)
            if not amount:
                continue
            label = _(
                "Forecast: %(bill)s — %(partner)s",
                bill=line.move_id.name or _("(no reference)"),
                partner=line.partner_id.display_name or _("(no partner)"),
            )
            self._create_forecast_move(
                -amount,
                line.date_maturity,
                "account.move,%s" % line.move_id.id,
                label,
            )
            created += 1

        _logger.info(
            "Liquidity forecast %s: %s forecast entries created",
            self.id,
            created,
        )
        return created

    def _compute_daily_lines(self):
        """Build one line per day of the horizon and roll the balance.

        Every day gets a row — including days with no movement — so the curve
        has no holes and the row count equals the horizon exactly.
        """
        self.ensure_one()
        Line = self.env["liquidity.forecast.line"].sudo()
        # sudo: an access rule must never leave stale rows behind and turn
        # the recompute into a unique-constraint crash.
        Line.search([("forecast_id", "=", self.id)]).sudo().unlink()

        if not self.opening_balance and self.opening_balance != 0.0:
            raise UserError(
                _(
                    "No opening balance. The forecast cannot be computed "
                    "without cash on hand at %(start)s.",
                    start=self.date_from,
                )
            )

        if not self.journal_id:
            self.journal_id = self._get_or_create_forecast_journal(self.company_id)

        # Aggregate movement per day from the forecast entries.
        #
        # A forecast entry carries its amount on exactly one accountable line;
        # the accompanying note line has no amount. Summing debit minus credit
        # over accountable lines therefore recovers each entry's signed amount
        # exactly once, and summing per day gives the day's movement.
        # Raw SQL reads the database, not the ORM cache. Without a flush the
        # entries created a moment ago are still only in the cache and the
        # aggregation sees an empty day.
        #
        # Each projection is two balanced lines: the amount on the holding
        # account and its counterpart on the dedicated counterpart account. The
        # filter below keeps only the holding account, so this sums the
        # projected amount once. The direction is the sign of debit minus credit
        # on that line — incoming projects a debit, outgoing a credit — which
        # means no separate sign column is needed and none can drift.
        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT aml.date,
                   COALESCE(SUM(aml.debit), 0.0),
                   COALESCE(SUM(aml.credit), 0.0)
              FROM account_move_line aml
              JOIN account_move am ON am.id = aml.move_id
             WHERE am.journal_id = %s
               AND am.liquidity_type = 'forecast'
               AND aml.date >= %s
               AND aml.date <= %s
               AND aml.account_id = %s
             GROUP BY aml.date
            """,
            (
                self.journal_id.id,
                self.date_from,
                self.date_to,
                self.journal_id.liquidity_forecast_account_id.id,
            ),
        )
        movement = {
            row[0]: (
                float_round(row[1], precision_digits=2),
                float_round(row[2], precision_digits=2),
            )
            for row in self.env.cr.fetchall()
        }
        balance = self.opening_balance
        lowest = balance
        lowest_date = self.date_from
        first_negative_day = 0
        day_index = 0
        cursor = self.date_from
        values = []
        while cursor <= self.date_to:
            day_index += 1
            # Incoming and outgoing are reported gross, not netted: a day that
            # both receives and pays must show both, or the reader cannot tell a
            # quiet day from a busy one whose flows happen to cancel out.
            debits, credits = movement.get(cursor, (0.0, 0.0))
            incoming = debits
            outgoing = credits
            net = float_round(debits - credits, precision_digits=2)
            opening = balance
            balance = float_round(opening + net, precision_digits=2)

            if balance < lowest:
                lowest = balance
                lowest_date = cursor
            if balance < 0 and not first_negative_day:
                first_negative_day = day_index

            values.append(
                {
                    "forecast_id": self.id,
                    "date": cursor,
                    "opening_balance": opening,
                    "incoming": incoming,
                    "outgoing": outgoing,
                    "closing_balance": balance,
                    "has_event": False,
                    "event_type": False,
                }
            )
            cursor += timedelta(days=1)

        Line.create(values)
        return {
            "lowest_balance": lowest,
            "lowest_balance_date": lowest_date,
            "days_to_crisis": first_negative_day,
        }

    def _compute_risk_level(self, days_to_crisis, lowest_balance):
        """Map the numbers to a risk level.

        A crisis day beats an amount: a small shortfall soon is more dangerous
        than a large one in 80 days — time is what cannot be bought back.
        """
        self.ensure_one()
        if 0 < days_to_crisis <= CRISIS_DANGER_DAYS:
            return "danger"
        opening = self.opening_balance or 0.0
        if opening and abs(lowest_balance) < abs(opening) * THIN_BALANCE_RATIO:
            return "warning"
        if not opening and lowest_balance < 0:
            return "warning"
        return "good"

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_compute(self):
        """Compute the forecast, storing aggregates alongside the daily lines.

        On failure the forecast goes to ``error`` and keeps its previous good
        result — it never stays in ``draft`` as if nothing happened.
        """
        for forecast in self:
            try:
                with self.env.cr.savepoint():
                    if not forecast.journal_id:
                        forecast.journal_id = (
                            forecast._get_or_create_forecast_journal(
                                forecast.company_id
                            )
                        )
                    if not forecast.opening_balance_manual:
                        automatic = forecast._default_opening_balance()
                        if automatic is not False:
                            forecast.opening_balance = automatic

                    if forecast.opening_balance is False:
                        forecast.write(
                            {
                                "state": "draft",
                                "state_message": _(
                                    "No opening balance could be determined. "
                                    "The company has no cash or bank account "
                                    "with movements, and no balance was entered "
                                    "manually. Enter an opening balance to "
                                    "compute the forecast."
                                ),
                            }
                        )
                        continue

                    try:
                        forecast._generate_forecast_entries()
                        result = forecast._compute_daily_lines()
                    except UserError as exc:
                        forecast.write(
                            {
                                "state": "draft",
                                "state_message": str(exc),
                            }
                        )
                        continue

                    forecast.write(
                        {
                            "lowest_balance": result["lowest_balance"],
                            "lowest_balance_date": result["lowest_balance_date"],
                            "days_to_crisis": result["days_to_crisis"],
                            "risk_level": forecast._compute_risk_level(
                                result["days_to_crisis"],
                                result["lowest_balance"],
                            ),
                            "state": "computed",
                            "state_message": False,
                            "computed_at": fields.Datetime.now(),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                _logger.exception(
                    "Liquidity forecast %s failed to compute", forecast.id
                )
                forecast.write(
                    {
                        "state": "error",
                        "state_message": _(
                            "The last computation failed: %(error)s\n\n"
                            "The figures below are from "
                            "%(when)s and are older than the failed run.",
                            error=str(exc),
                            when=forecast.computed_at or _("never"),
                        ),
                    }
                )
        return True

    def action_set_manual_opening_balance(self):
        """Flag the opening balance as manually entered so it is not overwritten."""
        self.write({"opening_balance_manual": True})
        return True

    # ------------------------------------------------------------------
    # Cron entry point
    # ------------------------------------------------------------------
    @api.model
    def _cron_recompute_all(self):
        """Nightly recompute for every company, driven by cron_tick."""
        companies = self.env["res.company"].sudo().search([])
        for company in companies:
            forecast = (
                self.sudo()
                .search(
                    [("company_id", "=", company.id)],
                    limit=1,
                    order="id desc",
                )
            )
            if not forecast:
                forecast = self.sudo().create({"company_id": company.id})
            forecast.action_compute()
