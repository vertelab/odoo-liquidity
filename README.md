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

### `liquidity_portfolio` — one overview across many customers (step 1 + driven sources)

An accounting firm with many Odoo customers has one question no single database
can answer: *which client needs me today?* This module answers it.

**The unit of analysis is `res.company`, not the instance.** One customer Odoo
may hold four companies; the portfolio shows four rows, because that is what the
firm actually works on.

**Reading, never recomputing — for Odoo sources.** An Odoo source has
`liquidity_forecast` installed. This module reads its finished figures over
JSON-RPC and shows them as they are: it does not compute a single KPI of its own,
because if the portfolio and the customer's own screen ever disagreed, the login
link would prove the portfolio wrong.

A Fortnox or Visma customer has no such module, and there the rule necessarily
changes — see *Driven sources* below. The list marks which kind of row you are
looking at, because the two are not the same kind of truth.

**No cache.** No snapshot table, no nightly copy. Every figure is fetched when
the view opens, in parallel, with a hard timeout per source.

**Degrade the numbers, never the row.** A row exists from the moment its company
was imported and keeps existing whatever the source does afterwards. A row that
disappears because a customer's server is down looks exactly like a customer who
left — and that is the most dangerous wrong signal this module could send.

#### The reading that must not be got wrong

`liquidity_forecast` reports `days_to_crisis = 0` for **no crisis within the
horizon**, not for *crisis today*. A negative opening balance gives 1. The
portfolio carries a separate `crisis_kind` field so the view cannot render 0 as
an alarm on every healthy customer.

`state` matters too:

| Source state | What it means | What the portfolio shows |
|---|---|---|
| `computed` | figures valid | the figures |
| `error` | last run failed, previous result kept | the figures, marked stale |
| `draft` | no opening balance (no bank connection) | "no opening balance", never zeros |

#### Setup

```bash
checkmodule -d <db> -m liquidity_portfolio
```

The API key belongs to the same user who bookkeeps for that customer, and lives
in keykeep — never in a plain field. Note that an Odoo API key is not a
permission model of its own: it authenticates as its user and inherits that
user's rights. This module only calls read methods, but the key is as broad as
the user it belongs to.

Tests:

```bash
checkmodule -d <db> -m liquidity_portfolio -t -l test --test-tags /liquidity_portfolio
```

#### Driven sources — Fortnox and Visma

A firm's customers are often mostly in Fortnox or Visma, where no
`liquidity_forecast` exists. There is nothing to be identical *to*, so the
requirement changes — and that change has to be visible or the list quietly
mixes two kinds of truth:

| Row kind | What the figures are | How you verify them |
|---|---|---|
| `read` (Odoo) | the customer's own figures, read as they are | log in to their Odoo |
| `derived` (driver) | computed here from the customer's bookkeeping | the entries they were computed from |

The list carries a **Kind** column for exactly this reason. A derived row offers
the underlag behind the figure, never a login link into a system that is not
Odoo.

**A driver fetches underlag, never KPIs.** `services/fortnox.py` and
`services/visma.py` return balances, open receivables and payables with their
dates; the arithmetic lives in `services/raw_data.py` so it can be tested
against known underlag and shown with its workings.

**`capabilities()` is the gate.** A source that does not expose invoice dates
cannot support receivables ageing, and the honest answer is *stöds inte av
källan* — never a zero, because a zero is a claim about the customer's money.

Visma is several products with different APIs. The driver therefore takes a
product profile per connection (`eekonomi`, `administration`), and a connection
that has not declared one supports nothing rather than guessing.

#### Three failure modes that must not be collapsed

| Situation | Shown as |
|---|---|
| the source does not answer at all | *Svarar inte* |
| the token expired or was revoked | *anslutningen behöver förnyas* |
| the quota was reached (429) | *fördröjd (kvot)* — retried, not alarmed |
| some sections answered, some failed | *delvis hämtad* + which part is missing |

An expired token rendered as "not answering" sends the reader looking for a
network problem that does not exist. A rate limit rendered as a failure turns a
portfolio of any size into a wall of red the reader learns to ignore.

#### Currency

Stated once, in `services/raw_data.py` (`CURRENCY_PRINCIPLE`): amounts are
counted in the source's own currency. **No conversion is done.** Converting
needs a rate, a rate date and a rate source, and each of those changes the
number — a portfolio that silently converts at today's rate produces a total
that matches no document the customer has. A source reporting more than one
currency is flagged on the row rather than summed across.

#### The truth shift, stated plainly

For a driven source the requirement is **"derivable from the customer's own
bookkeeping, and shown with its underlag"** — not *identical to a screen in
another system*. That is a deliberate, documented shift; blending read and
derived rows in one list without marking them would be the worst possible
answer.

#### One account per consultant

Step 1 had one login and one API key per customer, and a firm with eight
consultants shared them. That destroys the audit trail: the customer can no
longer see *who* was in their books, only that someone was.

`liquidity.tenant.account` fixes it — one row per consultant per source, each
with its own keykeep credential:

- the access log names a person, not just a firm,
- revoking one consultant is one row, not a rotation of a shared key that
  nobody dares do because it breaks everyone,
- two accounts sharing one credential is refused by a constraint, because a
  shared key is the defect this model exists to remove.

`liquidity.tenant.source` records whether the customer is Vertel's or was
brought in by an agency. Set at onboarding, never changed — the commission rule
depends on it being unambiguous.

Existing installations are migrated: each tenant's `login` becomes one account
row with `is_agency_account=False`. The migration is idempotent, so a retried
upgrade does not double the accounts.

#### Opening the customer's system — `liquidity_bridge`

Step 1 offers a login link, which is honest but means typing a password, and
the customer's log then shows "someone from the firm". The bridge closes that:

```
firm                          customer's Odoo (liquidity_bridge)
 |                                      |
 |  POST /liquidity_bridge/request  ->  | validates the bridge key,
 |                                      | issues a single-use token
 |  <-  token + url                     |
 |                                      |
 browser -----------------------------> | GET /liquidity_bridge/open?t=...
                                        | writes the access log FIRST,
                                        | then opens the session
```

**No session without a log line.** The access log row is written before the
session exists, in the same transaction. If the write fails the request fails.
An unlogged session is exactly what the customer cannot forgive.

The bridge is a separate module (`liquidity_bridge`), installed at the customer,
with restrictive defaults: `enabled=0`, `allow_agency_accounts=0`,
`allow_impersonation=0`. A bridge installed and forgotten must not be a bridge
that grants access. The firm's button appears only when the customer's bridge
answers and allows impersonation; otherwise the plain login link from step 1
remains, because a button that cannot work is worse than no button.

The three reasons a bridge is unusable are kept apart, because they need
different actions: no bridge installed, switched off at the customer, or
impersonation refused.

#### Agency commission

A firm that brings in a customer is paid for it. The model is Spiris' two-part
split, with the ending written down:

| When | What |
|---|---|
| At onboarding | 30 % of the first subscription period |
| Year 1+ | 10 % at ten customers, 20 % at twenty |
| When it ends | six-month ramp, then zero |

**Four rules, each closing a way to game the model:**

1. Only an *active* agreement on a *paying* customer earns anything. A customer
   who stopped paying still exists in the portfolio; the commission must not.
2. **Vertel's own customers cannot be claimed** — no signup, no recurring bonus,
   no back door. Allowing it would give the agency an incentive to convert
   Vertel customers.
3. **A returning customer is not a new customer.** `brought_in_at` survives the
   round trip, and `signup_paid_at` records that the signup was earned, so a
   reopened agreement cannot earn it twice.
4. **Ending is a ramp, not a cliff.** Six months, then zero.

`recurring_rate` is deliberately **not stored**: it depends on how many
agreements the agency has, which changes when *any other* agreement is created
— and Odoo's dependency system cannot express "any record of this agency". A
stored compute would silently go stale at the first agreement.

The agreement computes the basis. It does not invoice and does not pay out.

#### Not yet

Mirrored chat and the customer-initiated work order flow are separate changes.
See `openspec/changes/portfolio-steg2-kundbrygga` and `byra-provision` in the
planning repository.

### `liquidity_work` — work orders from a product's plan (step 2, landed)

Step 1 answers *which client needs me today*. This module answers *what do we do
about it* — and makes the answer visible.

**The flow is the one Odoo already uses for services:**

```
product.template.liquidity_tracking = 'work_plan'
product.template.liquidity_template_id -> liquidity.work.template

sale order confirmed -> a plan is created (a copy of the template)
nightly cron         -> work orders are created from the plan's lines
```

**Why a copy and not a reference.** The template is a record and the plan is its
own record with its own lines. Editing the template afterwards never changes a
plan already sold — the customer bought the plan as it was, not as it later
became.

**Why cron and not confirmation.** A plan running for a year cannot create
twelve work orders at once: they would sit in the calendar and nobody would know
which were current. The plan exists the moment the order is confirmed, so the
seller sees the effect of what they sold; the orders appear as their periods
approach.

**The one rule that must hold.** Cron runs every night. If it created a work
order per night the calendar would be useless within a week. The guard is a
unique constraint on `(plan_id, plan_line_id, period_start)` — stronger than
trusting the code to always check first.

**Missed periods are never skipped.** A period that passed without being ticked
off stays visible as overdue. Skipping it silently would hide that the work was
not done.

**Four views.** List (what is left), kanban (the flow), calendar (load over
time), form (where the work is done). The calendar is what makes a plan
meaningful: a plan without a calendar is just a list of intervals.

**HTML descriptions, with the consequence handled.** A description is rich text
because "reconcile the bank" often needs a checklist or a link. But HTML in a
list view shows `<ul><li>` to the user, so the list shows
`description_excerpt` — plain text, truncated.

**Time reporting is underlying data, not invoicing.** No product, no price, no
invoice link. Turning it into billing is a separate decision.

Tests:

```bash
checkmodule -d <db> -m liquidity_work -t -l test --test-tags /liquidity_work
```

#### Not in this module

Customer-initiated requests, chat mirroring and Fortnox/Visma drivers are
separate changes (`portfolio-steg2-kundbrygga`, `portfolio-steg2-externa-drivers`).
