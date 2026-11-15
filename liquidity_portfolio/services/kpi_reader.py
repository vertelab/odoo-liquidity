# -*- coding: utf-8 -*-
"""Read liquidity KPIs from a source Odoo. Never compute them here.

The field list below is the contract against ``liquidity_forecast``
18.0.1.0.0. It is read from ``models/liquidity_forecast.py`` in
``vertelab/odoo-liquidity`` — not invented from the PRD.

Two readings in that contract are easy to get wrong, and both are handled
here rather than in the view:

``days_to_crisis = 0``
    means *no crisis within the horizon*. A negative opening balance gives 1.
    A portfolio that renders 0 as "crisis today" cries wolf on every healthy
    customer.

``state``
    ``draft`` means the customer has no opening balance (no bank connection),
    so there is no forecast at all — show why, never zeros. ``error`` means the
    last nightly run failed but the previous good result is still stored —
    show the numbers with a freshness warning, not "not answering".
"""
import logging

from . import rpc_client

_logger = logging.getLogger(__name__)

# Level 1 + 2: what the overview needs per row. Deliberately narrow — this is
# fetched for every company on every page load.
LEVEL_1_2_FIELDS = [
    "risk_level",
    "days_to_crisis",
    "lowest_balance",
    "lowest_balance_date",
    "opening_balance",
    "date_from",
    "date_to",
    "computed_at",
    "state",
    "state_message",
    "currency_id",
]

# Level 3: only fetched when a row is opened.
LEVEL_3_LINE_FIELDS = [
    "date",
    "opening_balance",
    "incoming",
    "outgoing",
    "closing_balance",
    "has_event",
    "event_type",
]


def latest_forecast(client, company_ref):
    """Latest forecast for one company in the source.

    Uses the same ordering as ``_cron_recompute_all`` in liquidity_forecast:
    ``order="id desc", limit=1``. There is no ``latest_forecast_id`` on
    ``res.company``, so this lookup is the contract.
    """
    return client.search_read(
        "liquidity.forecast",
        [("company_id", "=", company_ref)],
        LEVEL_1_2_FIELDS,
        limit=1,
        order="id desc",
    )


def forecast_lines(client, forecast_id):
    """Daily lines for one forecast — the Level 3 curve."""
    return client.search_read(
        "liquidity.forecast.line",
        [("forecast_id", "=", forecast_id)],
        LEVEL_3_LINE_FIELDS,
        order="date asc",
    )


def companies(client):
    """All companies in the source. The import source of truth."""
    return client.search_read(
        "res.company",
        [],
        ["name", "currency_id", "active"],
        order="id asc",
    )


def read_level_1_2(client, company_ref):
    """Read the overview figures for one company.

    Returns a dict with a ``status`` key that the view can switch on:

    ``ok``        figures present and computed
    ``no_forecast``  the source has no forecast row for this company
    ``draft``     forecast exists but has no opening balance
    ``stale``     computed, but the last run failed — previous result kept
    ``rpc_error`` the call itself failed
    """
    result = latest_forecast(client, company_ref)
    if not result.ok:
        return {
            "status": "rpc_error",
            "message": result.message,
            "outcome": result.outcome,
        }

    rows = result.value or []
    if not rows:
        return {"status": "no_forecast", "message": ""}

    row = rows[0]
    state = row.get("state") or "draft"

    if state == "draft":
        return {
            "status": "draft",
            "message": row.get("state_message") or "",
            "forecast": row,
        }

    # state is "computed" or "error". Both carry usable figures: on error the
    # previous good result is kept on purpose (see the field's help text).
    return {
        "status": "stale" if state == "error" else "ok",
        "message": row.get("state_message") or "",
        "forecast": row,
    }


def interpret_days_to_crisis(value):
    """Turn the source's integer into something a human can act on.

    ``0`` is the trap: it means *no crisis within the horizon*, not *crisis
    now*. ``None`` means the field was empty.
    """
    if value is None:
        return {"kind": "unknown", "days": None}
    if value == 0:
        return {"kind": "none_in_horizon", "days": None}
    return {"kind": "crisis", "days": value}
