# -*- coding: utf-8 -*-
{
    "name": "Liquidity Bridge",
    "version": "18.0.1.0.0",
    "category": "Accounting/Accounting",
    "summary": "Installed at the customer: lets the accounting firm send and open, not only read",
    "description": """Liquidity Bridge
===============

Step 1 reads *from* the customer's Odoo. Everything that goes the other way --
the customer flags an entry for review, or the firm opens the customer's system
in a logged-in session -- needs something running **at the customer**. That is
this module.

One bridge, three functions
---------------------------

1. **The customer flags something.** A button on an invoice or a journal entry
   puts it in an outgoing queue. The firm sees it as a work order. No email, no
   phone call.
2. **The firm opens the customer's system.** A short-lived, single-use token
   issued by the customer and redeemed here. The firm's consultant lands in the
   customer's own screen, as the consultant -- not as a shared account.
3. **Mirrored chat.** A message thread that exists at both parties, so the
   answer is where the question was asked.

The queue survives the firm being down
--------------------------------------

``bridge.outbox`` holds events until they are delivered, with a retry counter
and a visible status per event. A bridge that drops an event because the firm's
server was restarting is a bridge that loses the customer's question, and the
customer has no way of knowing.

Nothing is trusted by default
-----------------------------

* The firm authenticates with a **bridge key** -- narrower than the read key
  from step 1, and revocable on its own.
* Impersonation tokens are single-use, short-lived and bound to one user and
  one company. Redeeming one writes an access log row **before** the session is
  created; if the write fails, there is no session.
* The customer can refuse agency accounts and refuse impersonation, globally or
  per user. Refusing is the default for impersonation.
* Every endpoint call is logged here, at the customer, with who, what, when and
  the outcome.

This module is the security boundary. It is the only place where the firm's
credentials are accepted, and it is installed by the customer, on the
customer's terms.
""",
    "author": "Vertel AB",
    "website": "https://vertel.se",
    "license": "LGPL-3",
    "depends": [
        "base",
        "account",
        "mail",
    ],
    "data": [
        "security/liquidity_bridge_security.xml",
        "security/ir.model.access.csv",
        "data/ir_config_parameter.xml",
        "data/ir_cron.xml",
        "views/bridge_outbox_views.xml",
        "views/bridge_token_views.xml",
        "views/bridge_access_log_views.xml",
        "views/bridge_incoming_views.xml",
        "views/account_move_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
