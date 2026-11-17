# -*- coding: utf-8 -*-
{
    "name": "Liquidity Portfolio",
    "version": "18.0.1.1.0",
    "category": "Accounting/Accounting",
    "summary": "One liquidity overview across many Odoo instances and companies",
    "description": """Liquidity Portfolio
===================

An accounting firm with many Odoo customers has one question that no single
database can answer: *which client needs me today?* This module answers it.

The unit of analysis is ``res.company``, not the instance. One customer Odoo
may hold four companies; the portfolio shows four rows, because that is what
the firm actually works on.

Reading, never recomputing
--------------------------

The source Odoo always has ``liquidity_forecast`` installed. This module reads
that module's finished figures over JSON-RPC and shows them as they are. It
does not compute a single KPI of its own: if the portfolio and the customer's
own screen ever disagreed, the login link would prove the portfolio wrong.

A consequence worth stating plainly: ``days_to_crisis = 0`` means *no crisis
within the horizon*, not *crisis today*. A negative opening balance gives 1.
Rendering 0 as an alarm would cry wolf on every healthy customer.

No cache
--------

There is no snapshot table and no nightly copy. Every figure is fetched when
the view opens, in parallel, with a hard timeout per source. A source that does
not answer in time is shown as "not answering" — never as a blank row. A hole
in the list is the most dangerous wrong signal this module could send, because
it looks like the company is gone.

Degrade the numbers, never the row
----------------------------------

A row is created when a source is connected and its companies imported. From
then on it exists regardless of whether the source answers. Two distinct states
are shown: the instance is down, or the instance answers but the company is no
longer in its list.

Driven sources: Fortnox and Visma
---------------------------------

A firm's customers are often mostly in Fortnox or Visma, where no
``liquidity_forecast`` exists. There is nothing to be identical *to*, so the
requirement changes: for a driven row the figures must be **derivable from the
customer's own bookkeeping and shown with their underlag** -- not identical to a
screen in another system.

That shift is visible rather than silent. Each row carries a ``truth_kind``:
``read`` (the customer's own figure, verified by logging in) or ``derived``
(computed here, verified by its entries). Blending the two in one list without
marking them would be the worst possible answer.

A driver fetches underlag, never KPIs. ``capabilities()`` is the gate: a source
that does not expose invoice dates gets no receivables ageing, and the row says
*stods inte av kallan* rather than showing a zero -- a zero is a claim about the
customer's money. Visma is several products with different APIs, so the driver
takes a product profile per connection and supports nothing until one is
declared.

Three failure modes are kept apart: an expired token is *"the connection needs
renewing"*, not *"not answering"*; a rate limit is *"delayed (quota)"*, retried
and not alarmed; and a partial answer is half a row with a sentence naming the
missing part. Collapsing them sends the reader looking for the wrong problem.

Amounts are counted in the source's own currency. No conversion is done --
converting needs a rate, a rate date and a rate source, and each of those
changes the number.

Step 1 is read-only
-------------------

No work orders, no customer chat, no SSO, no Fortnox or Visma. Those are
separate changes. The credential is an API key belonging to the same user who
bookkeeps for the customer, stored in keykeep — never in a plain field.

Security note: an Odoo API key is not a permission model of its own. It
authenticates as its user and inherits that user's rights. This module only
calls read methods, but the key itself is as broad as the user it belongs to.
""",
    "author": "Vertel AB",
    "website": "https://vertel.se",
    "license": "LGPL-3",
    # ``account`` is not here for its models. The security groups imply
    # ``account.group_account_readonly`` and the menu hangs under
    # ``account.menu_finance``, so the module cannot be installed without it.
    # An external dependency on the *source* (liquidity_forecast) is a
    # different thing and is documented in the description, not declared here.
    "depends": [
        "base",
        "mail",
        "web",
        "account",
    ],
    "external_dependencies": {
        "python": ["requests"],
    },
    "data": [
        "security/liquidity_portfolio_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/liquidity_tenant_views.xml",
        "views/liquidity_agency_agreement_views.xml",
        "views/liquidity_agency_commission_basis_views.xml",
        "views/liquidity_tenant_company_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
