# -*- coding: utf-8 -*-
{
    "name": "Liquidity Forecast",
    "version": "18.0.1.0.0",
    "category": "Accounting/Accounting",
    "summary": "Deterministic 90-day liquidity forecast from open receivables and payables",
    "description": """Liquidity Forecast
==================

Builds a deterministic day-by-day liquidity curve from open customer
receivables and vendor payables, without inventing numbers.

A pseudo-journal marked with ``is_liquidity_forecast`` holds forecast entries
as real ``account.move`` records, so every projected krona is traceable to its
source document through ``liquidity_origin``. ``liquidity.forecast`` stores the
run and its aggregate figures (lowest balance, days to crisis, risk level),
and ``liquidity.forecast.line`` stores one row per day of the horizon
including days with no movement, so the curve has no holes.

Overdue receivables keep their ``date_maturity`` and are never moved forward to
today. Moving them forward would make the cash position look better than it
is: the money has not arrived, and the forecast must say so.

Scheduling runs through ``cron_tick`` (``cron.task``) rather than ``ir.cron``.
The engine owns cadence, deterministic spread, locking and run history.

Without an opening balance the forecast stays in ``draft`` with a message. It
never reports ``computed`` on an invented zero.""",
    "author": "Vertel AB",
    "website": "https://vertel.se",
    "license": "LGPL-3",
    "depends": [
        "account",
        "mn_finance_insights",
        "cron_tick",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/cron_task.xml",
        "views/liquidity_forecast_line_views.xml",
        "views/liquidity_forecast_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
