# -*- coding: utf-8 -*-
import logging
from concurrent.futures import ThreadPoolExecutor

from odoo import _, api, fields, models

from ..services import kpi_reader

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
            row.crisis_kind = data.get("crisis_kind", "unknown") if has_figures else "unknown"
            row.lowest_balance = data.get("lowest_balance") if has_figures else False
            row.lowest_balance_date = data.get("lowest_balance_date") if has_figures else False
            row.opening_balance = data.get("opening_balance") if has_figures else False
            row.computed_at = data.get("computed_at") if has_figures else False

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
