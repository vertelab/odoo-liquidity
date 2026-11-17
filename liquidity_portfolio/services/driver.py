# -*- coding: utf-8 -*-
"""The driver contract: fetch *underlag*, never finished KPIs.

Step 1 reads finished figures out of the customer's own ``liquidity_forecast``
and shows them unchanged. That is the whole point of step 1: if the portfolio
and the customer's screen ever disagreed, the login link would prove the
portfolio wrong.

A Fortnox or Visma customer has no ``liquidity_forecast`` to read. There is
nothing to be identical *to*. So the contract has to change, and it has to
change in a way that stays honest:

    a driven source is not "the same number as the customer's screen".
    It is "a number derived from the customer's own bookkeeping,
    shown together with the entries it was derived from".

That is why a driver returns ``RawLiquidityData`` -- balances, receivables,
payables, due dates -- and not ``days_to_crisis``. The arithmetic happens in
one place (``liquidity.kpi.compute``), so it can be tested against known
underlag and shown with its workings. A driver that computed its own KPI would
put the arithmetic in as many places as there are APIs.

``capabilities()`` is the gate. A source that does not expose invoice dates
cannot support receivables ageing, and the honest answer is "not supported by
this source" -- never a zero, and never a guess. A zero is a claim; an empty
cell is not.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

#: Every KPI the portfolio can show, and therefore every KPI a driver must
#: declare whether it supports. Kept as a module constant so a test can assert
#: that no driver silently forgets one.
ALL_CAPABILITIES = (
    "balances",           # account balances / cash position
    "bank_accounts",      # named bank accounts with balances
    "receivables",        # open customer invoices
    "payables",           # open supplier invoices
    "due_dates",          # maturity dates on the open items
    "invoice_dates",      # invoice date (needed for ageing)
    "currency",           # per-item currency, needed for conversion
    "next_events",        # tax/payroll dates, if the source knows them
)


class LiquiditySourceDriver(models.AbstractModel):
    """Base class for non-Odoo sources.

    Subclasses implement ``fetch_raw`` and ``capabilities``; the rest has
    defaults that are correct for "we do not know yet" rather than for "fine".
    """

    _name = "liquidity.source.driver"
    _description = "Liquidity source driver (non-Odoo)"

    name = fields.Char(string="Driver", readonly=True)
    source_type = fields.Char(
        string="Source type",
        readonly=True,
        help="Stable identifier written on the bookkeeping row, e.g. 'fortnox'.",
    )

    # -- contract ----------------------------------------------------------

    @api.model
    def capabilities(self):
        """Which KPIs this source's underlag can carry.

        Returns a subset of ``ALL_CAPABILITIES``. The base class returns an
        empty set on purpose: a driver that has not declared anything supports
        nothing, and the view will say so rather than show a number nobody can
        justify.
        """
        return set()

    @api.model
    def test_connection(self, credential):
        """Check that the source answers and the credential works.

        Returns ``ConnectionResult``. Never raises: a portfolio of two hundred
        customers cannot afford one bad source taking the view down.
        """
        raise NotImplementedError

    @api.model
    def fetch_raw(self, credential):
        """Fetch underlag. Returns ``RawLiquidityData``.

        Partial answers are normal and must be representable: a source can
        answer for balances and fail on receivables. The result therefore
        carries per-section status rather than one global ok/failed.
        """
        raise NotImplementedError

    @api.model
    def verify_url(self, credential, company_ref=None):
        """A link the user can open to check the figures at the source.

        Returns None when the source has no such page. A driver must not
        invent an Odoo-style login link for a system that is not Odoo.
        """
        return None

    # -- helpers -----------------------------------------------------------

    @api.model
    def unsupported(self, capability):
        """The sentence the view shows for a KPI the source cannot carry."""
        return "stöds inte av källan" if capability else "stöds inte av källan"
