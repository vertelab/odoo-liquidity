# -*- coding: utf-8 -*-
"""The shape of a driver's answer, and the arithmetic that turns it into KPIs.

Two rules run through this file, and both come from the same place: a driven
row is a *derived* row, and a derived row must be able to show its derivation.

1. **Nothing is invented.** A KPI that the underlag cannot carry is reported as
   unsupported, not as zero. Zero is a claim about the customer's money; an
   absent value is not.

2. **Partial is a first-class state.** An API can answer for balances and fail
   on receivables. The row is then shown half-filled with a sentence naming the
   missing part -- not blanked, and not marked "not answering", because the
   source did answer.
"""
import logging

_logger = logging.getLogger(__name__)

# Re-exported from the contract module. ``raw_data`` is where the arithmetic
# lives and where callers look first, so the vocabulary has to be reachable
# from here -- but it is defined once, in ``driver``, because it is part of the
# contract a driver declares and not part of the computation.
from .driver import ALL_CAPABILITIES  # noqa: E402,F401

# Section names used by both the raw data and the status reporting. Keeping
# them as constants means a typo cannot silently make a section "missing".
SECTION_BALANCES = "balances"
SECTION_BANK = "bank_accounts"
SECTION_RECEIVABLES = "receivables"
SECTION_PAYABLES = "payables"

SECTIONS = (SECTION_BALANCES, SECTION_BANK, SECTION_RECEIVABLES, SECTION_PAYABLES)

# Per-section outcomes. "empty" is not the same as "failed": a customer with no
# open supplier invoices answered perfectly well, they just have nothing there.
OK = "ok"
EMPTY = "empty"
FAILED = "failed"
UNSUPPORTED = "unsupported"
AUTH_EXPIRED = "auth_expired"
RATE_LIMITED = "rate_limited"


class RawLiquidityData(object):
    """Underlag from one source, one company. Close to what the API returned.

    Deliberately not a "result": no ratios, no days-to-crisis, no verdicts.
    The point is that the caller can always answer *where did this number come
    from*.
    """

    __slots__ = (
        "sections",
        "status",
        "message",
        "currency",
        "as_of",
        "capabilities",
        "verify_url",
        "source_type",
    )

    def __init__(self, source_type=None, currency=None, as_of=None,
                 capabilities=None):
        self.source_type = source_type
        # section name -> {"status": OK|EMPTY|FAILED|UNSUPPORTED, "items": [...]}
        self.sections = {}
        # section name -> one of OK/EMPTY/FAILED/UNSUPPORTED/AUTH_EXPIRED/RATE_LIMITED
        self.status = {}
        self.message = ""
        self.currency = currency
        self.as_of = as_of
        # The capabilities the source declared. Set here rather than by a
        # later assignment so the object is never briefly "supports nothing"
        # while holding data -- a distinction that decides whether a KPI is
        # shown or suppressed.
        self.capabilities = set(capabilities or ())
        self.verify_url = None

    # -- writing -----------------------------------------------------------

    def set_section(self, name, status, items=None, message=""):
        """Record one section's outcome. ``items`` is only meaningful for OK."""
        self.sections[name] = {
            "status": status,
            "items": items or [],
            "message": message,
        }
        self.status[name] = status
        return self

    def set_all(self, status, message=""):
        """Apply one status to every known section -- for a total failure."""
        for name in SECTIONS:
            self.set_section(name, status, message=message)
        return self

    # -- reading -----------------------------------------------------------

    def has(self, section):
        """True when the section answered with data (or legitimately empty)."""
        return self.status.get(section) in (OK, EMPTY)

    def items(self, section):
        entry = self.sections.get(section) or {}
        return entry.get("items") or []

    def failed_sections(self):
        return [
            name
            for name in SECTIONS
            if self.status.get(name) in (FAILED, AUTH_EXPIRED, RATE_LIMITED)
        ]

    def unsupported_sections(self):
        return [
            name for name in SECTIONS if self.status.get(name) == UNSUPPORTED
        ]

    @property
    def is_partial(self):
        """Some sections answered, some did not. The row must say which."""
        answered = [n for n in SECTIONS if self.has(n)]
        return bool(answered) and bool(self.failed_sections())

    @property
    def is_total_failure(self):
        return not any(self.has(n) for n in SECTIONS)

    def missing_description(self):
        """Plain sentence naming what is missing, for the row's status text."""
        missing = self.failed_sections()
        if not missing:
            return ""
        words = {
            SECTION_BALANCES: "saldon",
            SECTION_BANK: "bankkonton",
            SECTION_RECEIVABLES: "kundfordringar",
            SECTION_PAYABLES: "leverantörsskulder",
        }
        names = [words.get(name, name) for name in missing]
        if len(names) == 1:
            return "%s saknas" % names[0]
        return "%s saknas" % ", ".join(names[:-1]) + " och " + names[-1]

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<RawLiquidityData %s %s>" % (self.source_type, self.status)


class ConnectionResult(object):
    """Outcome of a connection test. Never an exception."""

    __slots__ = ("ok", "kind", "message")

    def __init__(self, ok, kind="ok", message=""):
        self.ok = ok
        self.kind = kind
        self.message = message

    def __repr__(self):  # pragma: no cover
        return "<ConnectionResult %s %s>" % ("ok" if self.ok else self.kind,
                                             self.message)


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------

#: The currency principle, stated once so the view and the documentation cannot
#: drift apart. It is deliberately conservative: we do not convert.
#:
#: Converting would need a rate, a rate date and a rate source, and every one
#: of those is a choice that changes the number. A portfolio that silently
#: converts at today's rate produces a total that matches no document the
#: customer has. So: amounts are counted in the source's own currency, and a
#: source that reports more than one currency is flagged rather than summed.
CURRENCY_PRINCIPLE = (
    "Belopp räknas i källans egen valuta. Ingen omräkning görs: en omräkning "
    "kräver kurs, kurstidpunkt och kurskälla, och var och en av dem ändrar "
    "talet. En källa som rapporterar flera valutor flaggas i stället för att "
    "summeras tyst."
)


def mixed_currencies(raw):
    """Currencies present in the underlag, when more than one.

    Returns a sorted list of currency codes, or an empty list when the data is
    single-currency (or carries none). The caller shows a warning for a
    non-empty result instead of adding apples to oranges.
    """
    found = set()
    for section in (SECTION_BANK, SECTION_BALANCES, SECTION_RECEIVABLES,
                    SECTION_PAYABLES):
        for item in raw.items(section):
            currency = item.get("currency")
            if currency:
                found.add(currency)
    return sorted(found) if len(found) > 1 else []


def compute_kpis(raw, capabilities=None):
    """Turn underlag into the KPI figures the overview shows.

    Every figure returned here can be traced back to ``raw``: the function
    reads nothing else. A KPI whose underlag is missing from ``raw`` is left
    out entirely -- the caller then renders "stöds inte av källan" instead of a
    number, which is the only honest option.

    Returns a dict of figures plus ``derivation``, so the view can show *which
    entries* a figure was built from.
    """
    capabilities = capabilities if capabilities is not None else raw.capabilities
    kpis = {}
    derivation = {}

    # -- cash position: bank accounts when known, else the balance section --
    if raw.has(SECTION_BANK) and "bank_accounts" in capabilities:
        accounts = raw.items(SECTION_BANK)
        if accounts:
            total = sum(float(a.get("balance") or 0.0) for a in accounts)
            kpis["cash_position"] = total
            derivation["cash_position"] = {
                "kind": "bank_accounts",
                "entries": accounts,
            }
    if "cash_position" not in kpis and raw.has(SECTION_BALANCES):
        balances = raw.items(SECTION_BALANCES)
        if balances:
            total = sum(float(b.get("balance") or 0.0) for b in balances)
            kpis["cash_position"] = total
            derivation["cash_position"] = {
                "kind": "account_balances",
                "entries": balances,
            }

    # -- receivables and payables, and the net of them ---------------------
    if raw.has(SECTION_RECEIVABLES):
        items = raw.items(SECTION_RECEIVABLES)
        kpis["receivables_open"] = sum(float(i.get("amount") or 0.0) for i in items)
        kpis["receivables_count"] = len(items)
        derivation["receivables_open"] = {"kind": "open_items", "entries": items}

    if raw.has(SECTION_PAYABLES):
        items = raw.items(SECTION_PAYABLES)
        kpis["payables_open"] = sum(float(i.get("amount") or 0.0) for i in items)
        kpis["payables_count"] = len(items)
        derivation["payables_open"] = {"kind": "open_items", "entries": items}

    if "receivables_open" in kpis and "payables_open" in kpis:
        kpis["net_open"] = kpis["receivables_open"] - kpis["payables_open"]

    # -- ageing is only possible when the source exposes invoice dates -----
    #
    # This is the gate that matters most: without invoice dates there is no
    # ageing, and a zero would read as "nothing is overdue" for a customer who
    # may be drowning in overdue invoices.
    if "invoice_dates" in capabilities:
        for section, key in (
            (SECTION_RECEIVABLES, "receivables_ageing"),
            (SECTION_PAYABLES, "payables_ageing"),
        ):
            if not raw.has(section):
                continue
            buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
            aged = 0
            for item in raw.items(section):
                age = item.get("age_days")
                if age is None:
                    continue
                amount = float(item.get("amount") or 0.0)
                if age <= 30:
                    buckets["0-30"] += amount
                elif age <= 60:
                    buckets["31-60"] += amount
                elif age <= 90:
                    buckets["61-90"] += amount
                else:
                    buckets["90+"] += amount
                aged += 1
            if aged:
                kpis[key] = buckets
                derivation[key] = {
                    "kind": "ageing",
                    "entries": raw.items(section),
                }

    # -- next known event, when the source knows its own calendar ----------
    if raw.has("next_events"):
        events = raw.items("next_events")
        if events:
            ordered = sorted(
                (e for e in events if e.get("date")),
                key=lambda e: e["date"],
            )
            if ordered:
                kpis["next_event"] = ordered[0]
                derivation["next_event"] = {
                    "kind": "calendar",
                    "entries": ordered[:3],
                }

    return {"kpis": kpis, "derivation": derivation}


def supported_kpis(capabilities):
    """Which KPI names a source with these capabilities can produce.

    Used by the view to decide between a number and "not supported by the
    source". Kept next to ``compute_kpis`` so the two cannot drift apart: a
    capability added here without a computation there (or the reverse) is a
    bug, and the tests assert the mapping both ways.
    """
    supported = set()
    if SECTION_BALANCES in capabilities or "bank_accounts" in capabilities:
        supported.add("cash_position")
    if SECTION_RECEIVABLES in capabilities or "receivables" in capabilities:
        supported.update({"receivables_open", "receivables_count"})
    if SECTION_PAYABLES in capabilities or "payables" in capabilities:
        supported.update({"payables_open", "payables_count"})
    if "receivables_open" in supported and "payables_open" in supported:
        supported.add("net_open")
    if "invoice_dates" in capabilities:
        if "receivables" in capabilities:
            supported.add("receivables_ageing")
        if "payables" in capabilities:
            supported.add("payables_ageing")
    if "next_events" in capabilities:
        supported.add("next_event")
    return supported
