# -*- coding: utf-8 -*-
import logging
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import distributions, simulation_engine

_logger = logging.getLogger(__name__)

DEFAULT_ITERATIONS = simulation_engine.DEFAULT_ITERATIONS
DEFAULT_HORIZON_DAYS = simulation_engine.DEFAULT_HORIZON_DAYS

#: Risk banner thresholds on p_crisis_90. Fixed in this phase; a wrong
#: threshold is easy to change later, and one threshold beats none.
BANNER_RED_ABOVE = 0.30
BANNER_YELLOW_ABOVE = 0.10


class LiquiditySimulation(models.Model):
    _name = "liquidity.simulation"
    _description = "Liquidity Monte Carlo Simulation"
    _order = "computed_at desc, id desc"
    _rec_name = "name"

    name = fields.Char(
        string="Reference",
        required=True,
        default=lambda self: _("Simulation"),
    )
    forecast_id = fields.Many2one(
        "liquidity.forecast",
        string="Forecast",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        related="forecast_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    currency_id = fields.Many2one(
        related="forecast_id.currency_id",
        readonly=True,
    )
    date_from = fields.Date(
        related="forecast_id.date_from",
        store=True,
        readonly=True,
    )

    horizon_days = fields.Integer(
        string="Horizon (days)",
        default=DEFAULT_HORIZON_DAYS,
        required=True,
        help="Number of days simulated. Matches the forecast horizon by "
             "default; the band is meaningless on a different span.",
    )
    iterations = fields.Integer(
        string="Iterations",
        default=DEFAULT_ITERATIONS,
        required=True,
        help="Number of simulated scenarios. 10 000 is the default; 1 000 "
             "is enough for a rough band. Lower it if the nightly run "
             "takes too long.",
    )
    seed = fields.Integer(
        string="Seed",
        help="Random seed. Defaults to the record id, so a given run is "
             "always reproducible without the user having to choose one.",
    )

    # --- aggregate results ---
    p_crisis_90 = fields.Float(
        string="P(crisis within horizon)",
        digits=(5, 4),
        readonly=True,
        help="Share of iterations where the balance went negative on some "
             "day. Not the sum of the daily probabilities — those are "
             "different numbers.",
    )
    lowest_p10 = fields.Monetary(
        string="Lowest P10",
        currency_field="currency_id",
        readonly=True,
    )
    lowest_p10_date = fields.Date(
        string="Lowest P10 Date",
        readonly=True,
    )
    lowest_p50 = fields.Monetary(
        string="Lowest P50",
        currency_field="currency_id",
        readonly=True,
    )
    lowest_p90 = fields.Monetary(
        string="Lowest P90",
        currency_field="currency_id",
        readonly=True,
    )
    worst_case_p95 = fields.Monetary(
        string="Worst 5%",
        currency_field="currency_id",
        readonly=True,
        help="The lowest balance in the worst 5 % of iterations. This is the "
             "figure a credit decision should lean on — not the median.",
    )
    confidence = fields.Selection(
        selection=[
            ("high", "High"),
            ("medium", "Medium"),
            ("low", "Low"),
        ],
        string="Confidence",
        readonly=True,
        help="Reflects how much of the simulated amount rests on partners "
             "with thin history. Low confidence means the band is wide "
             "because the data is thin — not because the simulation is "
             "unstable.",
    )
    sample_warnings = fields.Text(
        string="Thin Data Warnings",
        readonly=True,
        help="One line per partner whose profile is low, with the number of "
             "payments behind it. Shown in the view in plain text, never "
             "hidden in a tooltip.",
    )
    banner_level = fields.Selection(
        selection=[
            ("good", "Good"),
            ("warning", "Warning"),
            ("danger", "Danger"),
        ],
        string="Banner Level",
        compute="_compute_banner_level",
        help="Probability-based banner level. The deterministic risk_level "
             "is kept alongside as a secondary signal; the higher of the two "
             "wins in the UI.",
    )
    disclaimer = fields.Char(
        string="Disclaimer",
        compute="_compute_disclaimer",
        help="The responsible-use sentence. Rendered in the form as text, "
             "not as a tooltip: a probability band invites a decision, and "
             "the sentence that limits it has to be read.",
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
    )
    state_message = fields.Text(
        string="Status Message",
        readonly=True,
    )
    computed_at = fields.Datetime(
        string="Computed At",
        readonly=True,
    )
    duration_ms = fields.Integer(
        string="Duration (ms)",
        readonly=True,
        help="Wall-clock time of the run. Exposed to Zabbix; a run over "
             "30 000 ms is a problem, not a curiosity.",
    )

    line_ids = fields.One2many(
        "liquidity.simulation.line",
        "simulation_id",
        string="Daily Percentiles",
        readonly=True,
    )
    variance_contribution_ids = fields.One2many(
        "liquidity.variance.contribution",
        "simulation_id",
        string="Variance Contributions",
        readonly=True,
    )
    line_count = fields.Integer(compute="_compute_line_count")

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends("line_ids")
    def _compute_line_count(self):
        for simulation in self:
            simulation.line_count = len(simulation.line_ids)

    @api.depends("p_crisis_90", "state")
    def _compute_banner_level(self):
        for simulation in self:
            if simulation.state != "computed":
                simulation.banner_level = False
            elif simulation.p_crisis_90 > BANNER_RED_ABOVE:
                simulation.banner_level = "danger"
            elif simulation.p_crisis_90 > BANNER_YELLOW_ABOVE:
                simulation.banner_level = "warning"
            else:
                simulation.banner_level = "good"

    @api.depends("state")
    def _compute_disclaimer(self):
        for simulation in self:
            simulation.disclaimer = _(
                "The band shows historical payment behaviour, not a "
                "guarantee about the future."
            )

    # ------------------------------------------------------------------
    # Engine glue
    # ------------------------------------------------------------------
    def _get_baseline_balances(self):
        """Deterministic closing balance per day index, from the forecast."""
        self.ensure_one()
        lines = self.forecast_id.forecast_line_ids.sorted(lambda l: l.date)
        return [line.closing_balance for line in lines]

    def _build_items(self):
        """Turn the forecast's open documents into simulation items.

        Only documents whose maturity falls inside the horizon are included —
        the same set the deterministic curve uses. Anything outside the
        horizon is not simulated, because it is not in the forecast either;
        adding it here would make the band and the baseline describe
        different worlds.
        """
        self.ensure_one()
        forecast = self.forecast_id
        horizon = self.horizon_days
        date_from = forecast.date_from
        Profile = self.env["liquidity.payment.profile"].sudo()

        items = []
        warnings = []
        low_profiles = 0

        for source_type, direction, getter in (
            ("receivable", "receivable", forecast._get_open_receivable_lines),
            ("payable", "payable", forecast._get_open_payable_lines),
        ):
            for line in getter():
                if not line.date_maturity:
                    continue
                offset = (line.date_maturity - date_from).days
                if offset < 0 or offset >= horizon:
                    continue
                amount = abs(line.amount_residual)
                if not amount:
                    continue
                partner = line.partner_id
                profile = False
                if partner:
                    profile = Profile._get_or_create_for_partner(
                        partner, forecast.company_id, direction
                    )
                if profile:
                    delay_dist = profile._get_delay_distribution()
                    confidence = profile.confidence
                    p_pay = 1.0 - (profile.p_default or 0.0)
                    if confidence == "low":
                        low_profiles += 1
                        warnings.append(
                            _(
                                "%(partner)s: %(count)s payments, industry "
                                "default used",
                                partner=partner.display_name,
                                count=profile.sample_size,
                            )
                        )
                else:
                    # No partner at all: the amount is real, the behaviour is
                    # unknown. Use the conservative fallback, and say so.
                    delay_dist = distributions.NormalDist(
                        distributions.FALLBACK_PROFILE["mean_delay_days"],
                        distributions.FALLBACK_PROFILE["stdev_delay_days"],
                        cap_days=distributions.OUTLIER_CAP_MIN_DAYS,
                    )
                    confidence = "low"
                    p_pay = 1.0 - distributions.FALLBACK_PROFILE["p_default"]
                    low_profiles += 1
                    warnings.append(
                        _("(no partner): no payment history available")
                    )

                if source_type == "receivable":
                    dist = distributions.BernoulliDist(p_pay, delay_dist=delay_dist)
                    signed = amount
                else:
                    # A payable is paid by us, essentially always, on a date
                    # we control. It carries no default risk, so no Bernoulli.
                    dist = delay_dist
                    signed = -amount

                items.append(
                    simulation_engine.SimItem(
                        key="%s-%s" % (source_type, line.id),
                        label="%s — %s" % (
                            line.move_id.name or _("(no reference)"),
                            partner.display_name if partner else _("(no partner)"),
                        ),
                        amount=signed,
                        due_offset=offset,
                        dist=dist,
                        source_type=source_type,
                        partner_id=partner.id if partner else False,
                        partner_name=partner.display_name if partner else False,
                        confidence=confidence,
                    )
                )

        # Confidence follows the share of simulated documents that rest on
        # thin history — not the share of amount, because one large uncertain
        # invoice still makes the band wide. With no documents at all there is
        # nothing to be confident about: the band is a flat line from the
        # opening balance, and calling that "high confidence" would be a
        # claim about data that does not exist.
        low_share = (low_profiles / float(len(items))) if items else 1.0
        if low_share >= 0.5:
            confidence = "low"
        elif low_share >= 0.2:
            confidence = "medium"
        else:
            confidence = "high"

        return items, warnings, confidence

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_compute(self):
        """Run the simulation and store the result.

        Refuses to run without an opening balance: a band that starts from
        zero is worse than no band, because it is a claim about a reality
        that does not exist (design.md decision 8).
        """
        for simulation in self:
            forecast = simulation.forecast_id
            # ``opening_balance`` is a Monetary field: Odoo stores "empty" as
            # 0.0, not False, so the guard must test the value. A forecast
            # without a cash position is the same thing as one with zero.
            if not forecast.opening_balance:
                raise UserError(
                    _(
                        "The forecast has no opening balance. A simulation "
                        "needs the actual cash position to start from — "
                        "connect a bank account or enter the balance "
                        "manually. Simulating from zero would produce a "
                        "band that looks computed but is not."
                    )
                )
            started = time.monotonic()
            try:
                with self.env.cr.savepoint():
                    items, warnings, confidence = simulation._build_items()
                    baseline = simulation._get_baseline_balances()
                    seed = simulation.seed or simulation.id or 0
                    result = simulation_engine.run(
                        items,
                        opening_balance=forecast.opening_balance,
                        horizon_days=simulation.horizon_days,
                        iterations=simulation.iterations,
                        seed=seed,
                        baseline=baseline,
                    )
                    simulation._store_result(result, items, warnings, confidence)
                    simulation.write(
                        {
                            "state": "computed",
                            "state_message": False,
                            "computed_at": fields.Datetime.now(),
                            "duration_ms": int(
                                (time.monotonic() - started) * 1000
                            ),
                        }
                    )
            except UserError:
                raise
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                _logger.exception(
                    "Liquidity simulation %s failed", simulation.id
                )
                simulation.write(
                    {
                        "state": "error",
                        "state_message": _(
                            "The last simulation failed: %(error)s",
                            error=str(exc),
                        ),
                        "duration_ms": int(
                            (time.monotonic() - started) * 1000
                        ),
                    }
                )
        return True

    def _store_result(self, result, items, warnings, confidence):
        """Write percentiles, variance contributions and aggregates."""
        self.ensure_one()
        Line = self.env["liquidity.simulation.line"].sudo()
        Contribution = self.env["liquidity.variance.contribution"].sudo()
        Line.search([("simulation_id", "=", self.id)]).unlink()
        Contribution.search([("simulation_id", "=", self.id)]).unlink()

        date_from = self.forecast_id.date_from
        line_values = []
        for row in result["lines"]:
            line_values.append(
                {
                    "simulation_id": self.id,
                    "date": date_from + timedelta(days=row["date_offset"]),
                    "p10": row["p10"],
                    "p50": row["p50"],
                    "p90": row["p90"],
                    "p95": row["p95"],
                    "p99": row["p99"],
                    "p_negative": row["p_negative"],
                    "baseline_balance": row["baseline_balance"],
                    "expected_events": ", ".join(row["expected_events"]),
                }
            )
        Line.create(line_values)

        by_key = {item.key: item for item in items}
        contribution_values = []
        for key, share in result["variance_share"].items():
            item = by_key.get(key)
            if not item:
                continue
            contribution_values.append(
                {
                    "simulation_id": self.id,
                    "source_type": item.source_type,
                    "partner_id": item.partner_id or False,
                    "label": item.label,
                    "variance_share": share,
                }
            )
        if contribution_values:
            Contribution.create(contribution_values)

        self.write(
            {
                "p_crisis_90": result["p_crisis_90"],
                "lowest_p10": result["lowest_p10"],
                "lowest_p10_date": date_from
                + timedelta(days=result["lowest_p10_offset"]),
                "lowest_p50": result["lowest_p50"],
                "lowest_p90": result["lowest_p90"],
                "worst_case_p95": result["worst_case_p95"],
                "confidence": confidence,
                "sample_warnings": "\n".join(warnings) or False,
                "seed": result["seed"],
            }
        )
        return True

    def action_recompute(self):
        return self.action_compute()

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    @api.model
    def _cron_simulate_all(self):
        """Nightly simulation for every computable forecast.

        Runs after the forecast cron (sequence 20 vs 10). A forecast that is
        not ``computed`` is skipped rather than forced: the band needs a
        baseline to sit on.
        """
        forecasts = (
            self.env["liquidity.forecast"]
            .sudo()
            .search([("state", "=", "computed")])
        )
        for forecast in forecasts:
            simulation = forecast._get_or_create_simulation()
            try:
                simulation.action_compute()
            except UserError as exc:
                _logger.warning(
                    "Liquidity simulation for forecast %s skipped: %s",
                    forecast.id,
                    exc,
                )
            except Exception:  # noqa: BLE001 - one forecast must not stop the rest
                _logger.exception(
                    "Liquidity simulation for forecast %s failed", forecast.id
                )
        return True
