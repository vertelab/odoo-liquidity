# -*- coding: utf-8 -*-
"""The bookkeeping row for a driven source.

An Odoo row reads finished figures and shows them unchanged. A driven row has
no finished figures to read: the portfolio fetches underlag from the source and
derives the KPIs itself. That difference has to be visible, or the list quietly
mixes two kinds of truth:

    Odoo row       "last ur kundens egen vy"        -> verify by logging in
    Driven row     "beräknad här, underlag visas"   -> verify by the entries

So the row carries:

* ``truth_kind``   which of the two it is, in the list,
* ``driver_status`` what the fetch produced, including the partial state,
* ``driver_kpis``  the derived figures,
* ``driver_derivation``  which entries each figure came from.

And the gate: a figure the source's capabilities do not carry is *absent*, not
zero. ``driver_unsupported`` names them, so the view can say "stöds inte av
källan" instead of showing a number nobody can justify.
"""
import logging

from odoo import api, fields, models

from ..services import fortnox, raw_data, visma

_logger = logging.getLogger(__name__)

#: Which driver class serves which source type. Kept here rather than in the
#: driver module so adding a source is one entry, not a search.
DRIVERS = {
    "fortnox": fortnox.FortnoxDriver,
    "visma": visma.VismaDriver,
}


class LiquidityTenantCompany(models.Model):
    _inherit = "liquidity.tenant.company"

    # -- what kind of truth is this row? -----------------------------------

    truth_kind = fields.Selection(
        selection=[
            ("read", "Läst ur källan"),
            ("derived", "Beräknad här"),
        ],
        string="Kind of truth",
        compute="_compute_truth_kind",
        store=True,
        help="A read row is the customer's own figure, verifiable by logging "
             "in. A derived row is computed here from the customer's "
             "bookkeeping, verifiable by the entries it was computed from. "
             "Showing the two without distinction would be the worst possible "
             "answer: the reader would not know what they are looking at.",
    )

    # -- driven-source status ----------------------------------------------

    driver_status = fields.Selection(
        selection=[
            ("unknown", "Not fetched"),
            ("ok", "OK"),
            ("partial", "Partly fetched"),
            ("unsupported", "Not supported by the source"),
            ("auth_expired", "Connection needs renewing"),
            ("rate_limited", "Delayed (quota)"),
            ("failed", "Fetch failed"),
        ],
        string="Fetch status",
        compute="_compute_driver_kpis",
        store=False,
    )
    driver_message = fields.Char(
        string="Fetch message",
        compute="_compute_driver_kpis",
        store=False,
        help="Names the missing part when the fetch was partial. 'Kundfordringar "
             "saknas' is a different statement from 'källan svarar inte'.",
    )
    driver_unsupported = fields.Char(
        string="Not supported by the source",
        compute="_compute_driver_kpis",
        store=False,
        help="KPIs this source's underlag cannot carry. The view shows them as "
             "'stöds inte av källan' — never as a zero, because a zero is a "
             "claim about the customer's money.",
    )

    # -- derived figures ----------------------------------------------------

    driver_cash_position = fields.Monetary(
        string="Cash position",
        currency_field="currency_id",
        compute="_compute_driver_kpis",
        store=False,
    )
    driver_receivables_open = fields.Monetary(
        string="Receivables outstanding",
        currency_field="currency_id",
        compute="_compute_driver_kpis",
        store=False,
    )
    driver_payables_open = fields.Monetary(
        string="Payables outstanding",
        currency_field="currency_id",
        compute="_compute_driver_kpis",
        store=False,
    )
    driver_net_open = fields.Monetary(
        string="Net open",
        currency_field="currency_id",
        compute="_compute_driver_kpis",
        store=False,
    )
    driver_receivables_count = fields.Integer(
        compute="_compute_driver_kpis",
        store=False,
    )
    driver_payables_count = fields.Integer(
        compute="_compute_driver_kpis",
        store=False,
    )
    driver_currency_warning = fields.Char(
        string="Mixed currencies",
        compute="_compute_driver_kpis",
        store=False,
        help="Set when the underlag carries more than one currency. The "
             "figures are then in the source's own currencies and are NOT "
             "summed across them: converting would need a rate, a rate date "
             "and a rate source, and each of those changes the number.",
    )
    driver_derivation = fields.Text(
        string="Underlag",
        compute="_compute_driver_kpis",
        store=False,
        help="The entries each figure was computed from, so the reader can "
             "check the arithmetic instead of trusting it.",
    )

    # -- computation -------------------------------------------------------

    @api.depends("tenant_id.source_type")
    def _compute_truth_kind(self):
        for row in self:
            row.truth_kind = (
                "derived" if row.tenant_id.source_type != "odoo" else "read"
            )

    def _compute_driver_kpis(self):
        """Fetch and derive, for driven rows only.

        Odoo rows are left with empty driver fields: assigning them nothing is
        what makes ``truth_kind`` meaningful rather than decorative.
        """
        for row in self:
            if row.tenant_id.source_type == "odoo":
                row.driver_status = "unknown"
                row.driver_message = ""
                row.driver_unsupported = ""
                row.driver_cash_position = False
                row.driver_receivables_open = False
                row.driver_payables_open = False
                row.driver_net_open = False
                row.driver_receivables_count = 0
                row.driver_payables_count = 0
                row.driver_derivation = False
                row.driver_currency_warning = False
                continue

            raw = row._fetch_driver_raw()
            if raw is None:
                row.driver_status = "failed"
                row.driver_message = "no driver is configured for this source"
                row.driver_unsupported = ""
                continue

            computed = raw_data.compute_kpis(raw, raw.capabilities)
            kpis = computed["kpis"]
            derivation = computed["derivation"]

            row.driver_status = self._driver_status_for(raw)
            row.driver_message = raw.missing_description() or raw.message
            row.driver_unsupported = self._unsupported_text(kpis, raw.capabilities)

            # A figure whose underlag is absent must stay absent. Monetary
            # fields read back as 0.0, but the view keys on driver_status and
            # driver_unsupported, so a zero cannot be mistaken for data.
            row.driver_cash_position = kpis.get("cash_position", False)
            row.driver_receivables_open = kpis.get("receivables_open", False)
            row.driver_payables_open = kpis.get("payables_open", False)
            row.driver_net_open = kpis.get("net_open", False)
            row.driver_receivables_count = kpis.get("receivables_count", 0)
            row.driver_payables_count = kpis.get("payables_count", 0)
            row.driver_derivation = self._format_derivation(derivation)
            mixed = raw_data.mixed_currencies(raw)
            row.driver_currency_warning = (
                "underlaget har flera valutor (%s) — beloppen räknas i "
                "källans egen valuta och summeras inte över dem"
                % ", ".join(mixed)
                if mixed else False
            )

    @staticmethod
    def _driver_status_for(raw):
        """Collapse the per-section outcomes into one row status.

        The order matters: an expired connection outranks a rate limit, which
        outranks a partial answer, which outranks a clean one. A row that is
        both limited and expired should say "renew", not "wait".
        """
        if raw_data.AUTH_EXPIRED in raw.status.values():
            return "auth_expired"
        if raw_data.RATE_LIMITED in raw.status.values():
            return "rate_limited"
        if raw.is_partial:
            return "partial"
        if raw.is_total_failure:
            if all(s == raw_data.UNSUPPORTED for s in raw.status.values()):
                return "unsupported"
            return "failed"
        return "ok"

    @staticmethod
    def _unsupported_text(kpis, capabilities):
        """Which KPIs this source cannot carry. Empty string when it can.

        This is the gate. A source without invoice dates gets no ageing, and
        the sentence says so -- the alternative, showing 0 for "nothing
        overdue", would be a specific and dangerous false statement.
        """
        expected = raw_data.supported_kpis(capabilities)
        absent = sorted(expected - set(kpis))
        if "invoice_dates" not in capabilities:
            absent = sorted(set(absent) | {"receivables_ageing", "payables_ageing"})
        return ", ".join(absent) if absent else ""

    @staticmethod
    def _format_derivation(derivation):
        """Render the workings as plain text for the form.

        Deliberately text and not a table: the reader needs to see that there
        *are* entries behind the number, and click into the source to check
        them. A pretty table would invite reading it instead of the source.
        """
        if not derivation:
            return False
        lines = []
        for name, block in sorted(derivation.items()):
            entries = block.get("entries") or []
            lines.append("%s (%s %s)" % (
                name, len(entries), "post" if len(entries) == 1 else "poster"
            ))
            for entry in entries[:10]:
                label = entry.get("name") or entry.get("partner") or entry.get("reference") or ""
                amount = entry.get("balance", entry.get("amount"))
                if amount is None:
                    continue
                lines.append("  %s: %s" % (label, amount))
            if len(entries) > 10:
                lines.append("  ... och %s till" % (len(entries) - 10))
        return "\n".join(lines) or False

    # -- driver wiring ------------------------------------------------------

    def _get_driver(self):
        """Instantiate the driver for this row's source type.

        Credentials come from keykeep as a dict, never stored on the record:
        the driver has no database handle and the secret never reaches a model
        field.
        """
        self.ensure_one()
        tenant = self.tenant_id
        driver_class = DRIVERS.get(tenant.source_type)
        if not driver_class:
            return None
        credential = tenant._get_driver_credential()
        if not credential:
            return None
        if tenant.source_type == "visma":
            credential = dict(credential)
            credential.setdefault("product", tenant.visma_product)
        return driver_class(credential)

    def _fetch_driver_raw(self):
        """One fetch, fully classified. Never raises."""
        self.ensure_one()
        try:
            driver = self._get_driver()
        except Exception:  # noqa: BLE001 - a bad credential must not kill the view
            _logger.exception(
                "Could not build a driver for %s", self.display_label
            )
            return None
        if driver is None:
            return None
        try:
            raw = driver.fetch_raw(self.company_ref)
        except Exception:  # noqa: BLE001 - one source must not kill the portfolio
            _logger.exception("Driver fetch failed for %s", self.display_label)
            failed = raw_data.RawLiquidityData(
                source_type=self.tenant_id.source_type,
            )
            failed.set_all(raw_data.FAILED, message="hämtningen föll")
            return failed

        self._persist_refreshed_credential(driver)
        return raw

    def _persist_refreshed_credential(self, driver):
        """Write a rotated token back to keykeep.

        Without this, an OAuth connection dies an hour after it was set up and
        the failure looks like a network problem. The write is best effort: a
        keykeep that cannot be written must not turn a successful fetch into a
        failure.
        """
        refreshed = getattr(driver, "refreshed_credential", None)
        if not refreshed:
            return
        try:
            self.tenant_id._store_driver_credential(refreshed)
        except Exception:  # noqa: BLE001 - see the docstring
            _logger.exception(
                "Could not persist the refreshed credential for %s",
                self.tenant_id.name,
            )
