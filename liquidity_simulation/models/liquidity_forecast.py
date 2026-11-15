# -*- coding: utf-8 -*-
from odoo import _, api, fields, models


class LiquidityForecast(models.Model):
    """Attach simulations to the forecast without changing its own contract.

    Nothing here alters ``lowest_balance``, ``days_to_crisis`` or
    ``risk_level``. Those keep their deterministic meaning; the simulation
    adds a second, probability-based reading alongside them. Uninstalling
    this module removes the fields below and leaves the baseline intact.
    """

    _inherit = "liquidity.forecast"

    simulation_ids = fields.One2many(
        "liquidity.simulation",
        "forecast_id",
        string="Simulations",
        readonly=True,
    )
    latest_simulation_id = fields.Many2one(
        "liquidity.simulation",
        string="Latest Simulation",
        compute="_compute_latest_simulation",
        help="The newest computed simulation, or empty when none exists. "
             "Empty means 'no data', which is not the same as 'no risk'.",
    )
    simulation_is_stale = fields.Boolean(
        string="Simulation Stale",
        compute="_compute_latest_simulation",
        help="True when the forecast was recomputed after the latest "
             "simulation. The band then describes an older baseline, and the "
             "view must show its age rather than present it as current.",
    )
    # A Float cannot hold "no value": Odoo reads an empty Float back as 0.0,
    # which would turn "no simulation" into "zero risk". The probability
    # therefore lives in a Char, formatted by the compute below -- "—" when
    # there is no simulation, "12.3 %" when there is. The numeric value is
    # available on ``latest_simulation_id.p_crisis_90`` for anything that
    # needs to compute with it.
    simulation_p_crisis_display = fields.Char(
        string="P(crisis within horizon)",
        compute="_compute_latest_simulation",
        help="Read from the latest simulation. Shows a dash when no "
             "simulation exists — never '0 %', so 'no data' and 'zero risk' "
             "stay distinguishable.",
    )
    effective_risk_level = fields.Selection(
        selection=[
            ("good", "Good"),
            ("warning", "Warning"),
            ("danger", "Danger"),
        ],
        string="Effective Risk",
        compute="_compute_latest_simulation",
        help="The higher of the deterministic risk_level and the "
             "probability-based banner level. When the two disagree, the "
             "disagreement is itself information: it means the outcome is "
             "more uncertain than the single curve suggests.",
    )

    @api.depends(
        "simulation_ids.state",
        "simulation_ids.computed_at",
        "simulation_ids.p_crisis_90",
        "simulation_ids.state",
        "simulation_ids.banner_level",
        "computed_at",
        "risk_level",
    )
    def _compute_latest_simulation(self):
        ranking = {"good": 0, "warning": 1, "danger": 2}
        for forecast in self:
            computed = forecast.simulation_ids.filtered(
                lambda s: s.state == "computed"
            ).sorted(lambda s: (s.computed_at or fields.Datetime.now(), s.id))
            latest = computed[-1:] if computed else forecast.simulation_ids.browse()
            forecast.latest_simulation_id = latest
            forecast.simulation_is_stale = bool(
                latest
                and forecast.computed_at
                and latest.computed_at
                and forecast.computed_at > latest.computed_at
            )
            # A dash, not "0 %" -- the two are different statements and the
            # UI must not confuse them.
            if latest and latest.state == "computed":
                forecast.simulation_p_crisis_display = "%.1f %%" % (
                    (latest.p_crisis_90 or 0.0) * 100.0
                )
            else:
                forecast.simulation_p_crisis_display = "—"
            deterministic = forecast.risk_level or "good"
            probabilistic = latest.banner_level if latest else False
            if not probabilistic:
                forecast.effective_risk_level = deterministic
                continue
            forecast.effective_risk_level = (
                probabilistic
                if ranking.get(probabilistic, 0) > ranking.get(deterministic, 0)
                else deterministic
            )

    def _get_or_create_simulation(self):
        """Return the simulation to reuse for this forecast, or create one.

        One simulation per forecast is enough: the run is a cache, and its
        history lives in ``computed_at``/``duration_ms``. A separate record
        per night would grow without bound and nobody would read it.
        """
        self.ensure_one()
        simulation = self.simulation_ids[:1]
        if simulation:
            return simulation
        return (
            self.env["liquidity.simulation"]
            .sudo()
            .create(
                {
                    "name": _("Simulation %(date)s", date=self.date_from),
                    "forecast_id": self.id,
                    "horizon_days": self.horizon_days or 90,
                }
            )
        )
