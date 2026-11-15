# odoo-liquidity

Liquidity forecasting for Odoo 18. Turns open receivables and payables into a
day-by-day cash curve you can defend — and, in a later phase, a probability band
around that curve.

## Modules

### `liquidity_forecast` — deterministic baseline (Fas 1, landed)

Builds the baseline curve. Every projected krona comes from a document that
already exists in the ledger; nothing is invented and nothing is smoothed.

**What it does**

- Reads open customer receivables and vendor bills inside the horizon.
- Writes one forecast `account.move` per source document, marked
  `liquidity_type='forecast'`, in a pseudo-journal flagged
  `is_liquidity_forecast`.
- Emits one `liquidity.forecast.line` per calendar day of the horizon —
  including days with no movement, so the curve has no holes.
- Reports `lowest_balance`, `lowest_balance_date`, `days_to_crisis` and
  `risk_level`.

**What it deliberately does not do**

- It never posts to a real result or liquidity account. All projected rows live
  on two dedicated forecast accounts (below).
- It never moves an overdue receivable forward to today. The money has not
  arrived; a forecast that pretends it has is worse than no forecast.
- It never reports `computed` on an invented opening balance. Without one, the
  forecast stays in `draft` with a message.

## Installation

```bash
checkmodule -d <db> -m liquidity_forecast
```

Depends on `account`, `mn_finance_insights` and `cron_tick`. The module is
scheduled through `cron.task` — cadence, deterministic spread, locking and run
history are owned by that engine, not by `ir.cron`.

Tests:

```bash
checkmodule -d <db> -m liquidity_forecast -t -l test --test-tags /liquidity_forecast
```

## The two forecast accounts

A forecast `account.move` must balance, so the projection cannot sit alone on one
account and still be read back unambiguously. It is carried by two accounts,
created on first use:

| Code | Role |
|------|------|
| `LQDFC00` | **Holding.** Carries the projected amount. The *only* account the aggregation reads. |
| `LQDFC01` | **Counterpart.** Exists so the entry balances. Never read by anything. |

Because the holding account is never mixed with the counterpart, the daily
figure is simply `SUM(debit - credit)` on `LQDFC00` — no `/2.0`, no sign column.
The direction is carried structurally by which side of the entry the amount sits
on.

Each forecast entry is three lines: the amount on the holding account, the
mirror on the counterpart, and a note line with `display_type='line_note'` and
`account_id = False`. The note exists only to say in the UI that this is not
bookkeeping.

## Incoming and outgoing are gross

`incoming` and `outgoing` are reported **gross per day**, never netted. A day
that both receives 1000 and pays 400 shows both figures. Netting them to 600
would make a busy day indistinguishable from a quiet one whose flows happen to
cancel — and the whole point of the row is to show what happens on that day.

## Known limitations

- **Single company.** Forecasts are per-company but the pseudo-journal lookup
  assumes one liquidity holding account per company.
- **No currency conversion.** Amounts are taken as stored; a multi-currency
  receivable is counted in its own currency.
- **The band is an addition, not a replacement.** Percentile bands
  (`P10`–`P90`) come from `liquidity_simulation` (below). The deterministic
  curve is unchanged and is still the baseline the band is drawn around.
- **`liquidity_type` is nullable.** The column carries no `NOT NULL` on
  purpose: Odoo's create pipeline does not include it in its INSERT, so
  `required=True` would make every ordinary invoice fail to insert. A `create`
  override stamps `'actual'` on real bookkeeping, so the value is never
  ambiguous in practice.
- **Performance is not measured here.** Run duration is recorded by
  `cron.task.log`; the model deliberately has no `duration_ms` field of its own.

### `liquidity_simulation` — probability band (Fas 5, landed)

Answers the question the single curve cannot: *how likely is it that the cash
runs out?* Every open receivable and payable is sampled from a distribution
built out of **that partner's own payment history**, the horizon is simulated N
times, and the result is stored as percentiles per day.

**What it does**

- Builds `liquidity.payment.profile` per partner, company and direction from
  `account.partial.reconcile` (payment date minus due date). Fewer than five
  payments means `confidence='low'` and a conservative fallback — never an
  invented distribution.
- Samples with `random.Random` and `statistics` only. No numpy, no scipy, no
  pandas; a test enforces it.
- Runs in cron through `cron_tick` (sequence 20, after the forecast cron), never
  in web traffic. The view reads cached rows.
- Stores `p_crisis_90` as the share of **iterations** that went negative on some
  day — not the sum of the daily probabilities. The two look alike and differ by
  a large factor.
- Refuses to run without an opening balance. A band that starts from zero looks
  computed and is not.

**What it deliberately does not do**

- It does not touch `liquidity.forecast.line`. No probability field is added to
  the baseline contract; a test asserts it.
- It does not show "0 %" where there is no data. The probability reads as a dash
  until a simulation exists — "no data" and "zero risk" are different
  statements.
- It does not patch the deterministic risk level. `effective_risk_level` is the
  higher of the two, so a disagreement between them stays visible.

**Still open** (see `openspec/changes/monte-carlo-liquidity-band/tasks.md`):
event markers in the OWL band, the per-day drill-down, the responsible-use
text in the view, and the Zabbix `duration_ms` metric. None of them change the
stored numbers.
