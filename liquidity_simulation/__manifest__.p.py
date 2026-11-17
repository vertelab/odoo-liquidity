# -*- coding: utf-8 -*-
{
    "name": "Liquidity Simulation",
    "version": "1.0.0",
    "category": "Accounting/Accounting",
    "summary": "Monte Carlo probability band around the deterministic liquidity forecast",
    "description": """Liquidity Simulation
===================

The deterministic forecast draws one curve. It cannot answer the question a
business owner actually asks: *how likely is it that the cash runs out?*

This module answers it. Every open receivable and payable is sampled from a
distribution built out of **that partner's actual payment history**
(``account.partial.reconcile``), the horizon is simulated N times, and the
result is stored as percentiles per day.

Design commitments, in order of importance:

* **The band is an addition.** The deterministic curve, and
  ``mn_finance_insights``' three scenarios, stay exactly as they are. The band
  is drawn around the baseline, never instead of it.
* **No invented precision.** A partner with three payments does not get a
  distribution -- it gets a conservative fallback and ``confidence='low'``,
  shown in plain text. Without an opening balance there is no band at all,
  rather than a band that starts from zero.
* **Nothing external.** Sampling uses ``random.Random``, statistics uses
  ``statistics``. No numpy, no scipy.
* **Never in web traffic.** The simulation runs in cron, after the forecast
  cron. The view reads cached rows.
* **Removable.** Uninstalling this module leaves ``liquidity_forecast``
  working; no field on the baseline models is owned by it.

``p_crisis_90`` is the share of *iterations* that went negative on some day --
never the sum of the daily probabilities. The two look similar and differ by a
large factor; see design.md decision 7.""",
    "author": "Vertel AB",
    "website": "https://vertel.se",
    "license": "LGPL-3",
    "depends": [
        "liquidity_forecast",
    ],
    "data": [
        # #if VERSION >= "20.0"
        "security/ir.access.csv",
        # #else
        "security/ir.model.access.csv",
        # #endif
        "data/cron_task.xml",
        "views/liquidity_payment_profile_views.xml",
        "views/liquidity_simulation_views.xml",
        "views/liquidity_forecast_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "liquidity_simulation/static/src/components/simulation_band.js",
            "liquidity_simulation/static/src/components/simulation_band.xml",
            "liquidity_simulation/static/src/scss/simulation_band.scss",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
