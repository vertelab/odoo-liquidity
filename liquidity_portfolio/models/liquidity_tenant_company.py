# -*- coding: utf-8 -*-
import logging
from concurrent.futures import ThreadPoolExecutor

from odoo import api, fields, models

from ..services import kpi_reader, rpc_client

_logger = logging.getLogger(__name__)

# How many sources we ask at once. The list view has a hard per-source timeout,
# so the wall-clock cost is roughly (companies / workers) * timeout in the worst
# case. Eight is a compromise: enough that ten customers feel instant, small
# enough that a hundred do not open a hundred sockets.
MAX_WORKERS = 8


class LiquidityTenantCompany(models.Model):
    """One bookkeeping: a ``res.company`` in a source instance.

    This row is the unit of analysis. It exists from the moment the company was
    imported and keeps existing no matter what the source does afterwards — a
    row that disappears because a customer's server is down looks exactly like
    a customer who left, and that is the most dangerous wrong signal this
    module could send.
    """

    _name = "liquidity.tenant.company"
    _description = "Bookkeeping in the liquidity portfolio"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "tenant_id, name"
    _rec_name = "display_label"

    tenant_id = fields.Many2one(
        "liquidity.tenant",
        string="Source",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_ref = fields.Integer(
        string="Company reference",
        required=True,
        readonly=True,
        help="The company's id in the source instance. This is the stable key: "
             "a rename in the customer's system must update this row, never "
             "create a new one.",
    )
    name = fields.Char(string="Company", required=True, tracking=True)
    display_label = fields.Char(
        string="Bookkeeping",
        compute="_compute_display_label",
        store=True,
    )
    currency_name = fields.Char(string="Source currency")
    active = fields.Boolean(
        string="Selected",
        default=True,
        help="Untick to hide a bookkeeping from the overview. Hiding is "
             "reversible; the row and its notes are kept.",
    )
    first_seen_at = fields.Datetime(string="First seen", readonly=True)
    last_seen_at = fields.Datetime(string="Last seen in source", readonly=True)
    missing_since = fields.Datetime(
        string="Missing since",
        readonly=True,
        help="Set when the source answered but no longer lists this company. "
             "The row is kept so local notes are not lost, but it is shown as "
             "'no longer at the customer' rather than as a live bookkeeping.",
    )

    # -- live figures, never stored ----------------------------------------

    kpi_status = fields.Selection(
        selection=[
            ("unknown", "Not fetched"),
            ("ok", "OK"),
            ("stale", "Last run failed"),
            ("draft", "No opening balance"),
            ("no_forecast", "No forecast"),
            ("unreachable", "Not answering"),
            ("auth_error", "Key refused"),
            ("rpc_error", "Error"),
        ],
        string="Forecast status",
        compute="_compute_kpis",
        store=False,
    )
    kpi_message = fields.Char(
        string="Status message", compute="_compute_kpis", store=False
    )
    risk_level = fields.Selection(
        selection=[("good", "Good"), ("warning", "Warning"), ("danger", "Danger")],
        compute="_compute_kpis",
        store=False,
    )
    days_to_crisis = fields.Integer(
        compute="_compute_kpis",
        store=False,
        help="Read straight from the source. 0 means 'no crisis within the "
             "horizon', not 'crisis today' — always read crisis_kind alongside "
             "it, never this field alone.",
    )
    crisis_kind = fields.Selection(
        selection=[
            ("crisis", "Crisis"),
            ("none_in_horizon", "No crisis in horizon"),
            ("unknown", "Unknown"),
        ],
        compute="_compute_kpis",
        store=False,
        help="The source reports 0 for 'no crisis within the horizon', not for "
             "'crisis today'. This field carries that distinction so the view "
             "cannot get it wrong.",
    )
    lowest_balance = fields.Monetary(
        string="Lowest balance",
        currency_field="currency_id",
        compute="_compute_kpis",
        store=False,
    )
    lowest_balance_date = fields.Date(compute="_compute_kpis", store=False)
    opening_balance = fields.Monetary(
        currency_field="currency_id", compute="_compute_kpis", store=False
    )
    computed_at = fields.Datetime(
        string="Forecast computed",
        compute="_compute_kpis",
        store=False,
        help="The source recomputes nightly, so this figure can be up to a day "
             "old. A portfolio that looks live but shows yesterday's number is "
             "a quiet lie.",
    )
    currency_id = fields.Many2one(
        "res.currency",
        compute="_compute_currency",
        store=False,
    )
    login_url = fields.Char(
        string="Login link",
        compute="_compute_login_url",
        store=False,
        help="Plain bookmark into the customer's own Odoo. Carries no "
             "credential: a browser cannot log in with an API key, and a "
             "session token in a URL would leak into history and logs.",
    )

    _sql_constraints = [
        (
            "tenant_company_ref_uniq",
            "unique(tenant_id, company_ref)",
            "This company is already imported for this source.",
        ),
    ]

    @api.depends("name", "tenant_id.name")
    def _compute_display_label(self):
        for row in self:
            row.display_label = "%s / %s" % (row.tenant_id.name or "", row.name or "")

    @api.depends("tenant_id.base_url")
    def _compute_login_url(self):
        for row in self:
            base = (row.tenant_id.base_url or "").rstrip("/")
            row.login_url = "%s/web/login" % base if base else False

    @api.depends("currency_name")
    def _compute_currency(self):
        Currency = self.env["res.currency"]
        for row in self:
            row.currency_id = (
                Currency.search([("name", "=", row.currency_name)], limit=1)
                if row.currency_name
                else False
            )

    # -- reading -----------------------------------------------------------

    def _compute_kpis(self):
        """Read Level 1+2 for the whole recordset, one call per source company.

        Batched by tenant so a page of rows does not open one connection per
        row: the same source is asked once, and its companies share the
        connection. Calls run in parallel across tenants, each bounded by the
        tenant's own timeout.
        """
        results = {}
        by_tenant = {}
        for row in self:
            by_tenant.setdefault(row.tenant_id, self.browse())
            by_tenant[row.tenant_id] |= row

        if by_tenant:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {
                    pool.submit(self._read_tenant, tenant, rows): tenant
                    for tenant, rows in by_tenant.items()
                }
                for future, tenant in futures.items():
                    try:
                        results.update(future.result())
                    except Exception as exc:  # never let one source kill the view
                        _logger.exception(
                            "Portfolio read failed for tenant %s", tenant.name
                        )
                        for row in by_tenant[tenant]:
                            results[row.id] = {
                                "kpi_status": "rpc_error",
                                "kpi_message": str(exc)[:200],
                            }

        for row in self:
            data = results.get(row.id) or {
                "kpi_status": "unknown",
                "kpi_message": "",
            }
            row.kpi_status = data.get("kpi_status", "unknown")
            row.kpi_message = data.get("kpi_message", "")
            # Integer/Monetary fields default to 0 when assigned None, which
            # would render "0 days to crisis" on a row that has no forecast at
            # all. Those rows must be empty, not zero — a zero is a claim.
            has_figures = data.get("kpi_status") in ("ok", "stale")
            row.risk_level = data.get("risk_level") if has_figures else False
            # Integer has no empty state in Odoo: False reads back as 0. The
            # view therefore keys on crisis_kind, never on days_to_crisis alone.
            row.days_to_crisis = data.get("days_to_crisis") if has_figures else 0
            if has_figures:
                row.crisis_kind = data.get("crisis_kind", "unknown")
                row.lowest_balance = data.get("lowest_balance")
                row.lowest_balance_date = data.get("lowest_balance_date")
                row.opening_balance = data.get("opening_balance")
                row.computed_at = data.get("computed_at")
            else:
                row.crisis_kind = "unknown"
                row.lowest_balance = False
                row.lowest_balance_date = False
                row.opening_balance = False
                row.computed_at = False

    @api.model
    def _read_tenant(self, tenant, rows):
        """One connection, many companies. Returns {row_id: values}."""
        client, result = tenant._connect()
        if not result.ok:
            status = {
                "unreachable": "unreachable",
                "auth_error": "auth_error",
                "no_liquidity": "no_forecast",
            }.get(result.outcome, "rpc_error")
            return {
                row.id: {"kpi_status": status, "kpi_message": result.message}
                for row in rows
            }

        out = {}
        for row in rows:
            data = kpi_reader.read_level_1_2(client, row.company_ref)
            status = data.get("status")
            if status == "rpc_error":
                out[row.id] = {
                    "kpi_status": "rpc_error",
                    "kpi_message": data.get("message", ""),
                }
                continue

            forecast = data.get("forecast") or {}
            crisis = kpi_reader.interpret_days_to_crisis(
                forecast.get("days_to_crisis")
            )
            out[row.id] = {
                "kpi_status": status,
                "kpi_message": data.get("message", ""),
                "risk_level": forecast.get("risk_level"),
                "days_to_crisis": forecast.get("days_to_crisis"),
                "crisis_kind": crisis["kind"],
                "lowest_balance": forecast.get("lowest_balance"),
                "lowest_balance_date": self._to_date(
                    forecast.get("lowest_balance_date")
                ),
                "opening_balance": forecast.get("opening_balance"),
                "computed_at": forecast.get("computed_at"),
            }
        return out

    @staticmethod
    def _to_date(value):
        """JSON-RPC returns dates as strings; Odoo wants date or False."""
        if not value:
            return False
        return value if len(value) == 10 else str(value)[:10]

    # -- detail ------------------------------------------------------------

    def action_open_detail(self):
        """Open the detail view: Level 3 is fetched there, with a longer timeout."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "liquidity.tenant.company",
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
            "name": self.display_label,
        }

    def action_refresh(self):
        """Force a re-read. The figures are computed, so this just re-renders."""
        return True

    # -- level 3 -----------------------------------------------------------

    def _get_level_3(self):
        """Fetch the daily curve for the detail view.

        Level 3 is deliberately not fetched on the overview: it is one row per
        day for every company, which would be ninety times the payload for a
        screen nobody has opened yet. It is fetched here, on demand, with a
        longer timeout than the overview uses — a source that fell over the
        overview's deadline can still answer this one.

        Returns a dict with the same ``status`` vocabulary as the overview, so
        the view has one thing to switch on.
        """
        self.ensure_one()
        client, result = self.tenant_id._connect()
        if not result.ok:
            return {
                "status": "unreachable"
                if result.outcome == rpc_client.UNREACHABLE
                else "rpc_error",
                "message": result.message,
                "lines": [],
            }

        forecast = kpi_reader.latest_forecast(client, self.company_ref)
        if not forecast.ok:
            return {
                "status": "rpc_error",
                "message": forecast.message,
                "lines": [],
            }

        rows = forecast.value or []
        if not rows:
            return {"status": "no_forecast", "message": "", "lines": []}

        forecast_id = rows[0].get("id")
        if not forecast_id:
            # search_read without an explicit id field: re-read to get it.
            return {"status": "no_forecast", "message": "", "lines": []}

        lines = kpi_reader.forecast_lines(client, forecast_id)
        if not lines.ok:
            return {
                "status": "rpc_error",
                "message": lines.message,
                "lines": [],
            }

        return {
            "status": "ok",
            "message": "",
            "lines": lines.value or [],
        }

    def action_load_curve(self):
        """Return the daily curve as JSON for the detail view.

        Called from the form when it opens. The view draws the curve from this;
        it does not compute anything.
        """
        self.ensure_one()
        data = self._get_level_3()
        return {
            "status": data["status"],
            "message": data["message"],
            "currency": self.currency_name or "",
            "lines": [
                {
                    "date": line.get("date"),
                    "opening": line.get("opening_balance"),
                    "incoming": line.get("incoming"),
                    "outgoing": line.get("outgoing"),
                    "closing": line.get("closing_balance"),
                    "has_event": line.get("has_event"),
                    "event_type": line.get("event_type"),
                }
                for line in data["lines"]
            ],
        }


