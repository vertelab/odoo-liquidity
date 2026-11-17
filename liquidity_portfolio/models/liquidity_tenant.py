# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import kpi_reader, rpc_client

_logger = logging.getLogger(__name__)


class LiquidityTenant(models.Model):
    """One connection to an external Odoo instance.

    The instance is a *container*, not the unit of analysis: one tenant holds
    many companies, and the portfolio shows one row per company.
    """

    _name = "liquidity.tenant"
    _description = "Liquidity source instance"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(string="Name", required=True, tracking=True)
    source_type = fields.Selection(
        selection=[
            ("odoo", "Odoo (liquidity_forecast)"),
            ("fortnox", "Fortnox"),
            ("visma", "Visma"),
        ],
        string="Source type",
        default="odoo",
        required=True,
        tracking=True,
        help="What kind of system this connection reads. An Odoo source has "
             "liquidity_forecast installed and its figures are read as they "
             "are. A driven source has no such module: the portfolio fetches "
             "underlag and derives the figures itself, and says so in the view.",
    )
    source = fields.Selection(
        selection=[
            ("vertel", "Vertel's customer"),
            ("agency", "Brought in by an agency"),
        ],
        string="Customer of",
        default="vertel",
        required=True,
        tracking=True,
        help="Who the customer belongs to. Set at onboarding and never "
             "changed: a customer is either Vertel's or an agency's, and the "
             "commission rule depends on it being unambiguous.",
    )
    owning_agency_id = fields.Many2one(
        "res.partner",
        string="Owning agency",
        help="The agency that brought this customer in. Empty when the "
             "customer is Vertel's own.",
    )
    account_ids = fields.One2many(
        "liquidity.tenant.account",
        "tenant_id",
        string="Accounts",
        help="One account per consultant. A firm with eight consultants has "
             "eight rows here, not one shared login -- the log has to name a "
             "person.",
    )
    account_count = fields.Integer(
        string="Accounts",
        compute="_compute_account_count",
    )
    visma_product = fields.Selection(
        selection=[
            ("eekonomi", "Visma eEkonomi"),
            ("administration", "Visma Administration"),
        ],
        string="Visma product",
        help="Visma is several products with different APIs and different "
             "coverage. Without a declared product the driver supports "
             "nothing, and the row says so instead of guessing.",
    )
    base_url = fields.Char(
        string="Base URL",
        required=True,
        help="For example https://kund.vertel.se. No trailing slash.",
    )
    db_name = fields.Char(
        string="Database",
        help="The Odoo database name in the source instance. Required for an "
             "Odoo source; empty for a driven source, which has no database to "
             "name.",
    )
    login = fields.Char(
        string="Login",
        help="The Odoo user the API key belongs to. Use the same user who "
             "bookkeeps for this customer, so the key is tied to a real account "
             "and can be revoked when that person leaves. Empty for a driven "
             "source, which authenticates with OAuth instead.",
    )
    bridge_keykeep_ref = fields.Char(
        string="Bridge key reference",
        help="Keykeep entry for the bridge key, which is separate from the "
             "read key on purpose: it cannot read the ledger, only ask for a "
             "session and deliver messages. Revoking one must not revoke the "
             "other.",
    )
    keykeep_ref = fields.Char(
        string="Keykeep reference",
        help="Reference to the API key in keykeep. The key itself is never "
             "stored in this field.",
    )
    state = fields.Selection(
        selection=[
            ("new", "Not connected"),
            ("ok", "Connected"),
            ("error", "Not answering"),
            ("auth_error", "Key refused"),
            ("no_liquidity", "liquidity_forecast missing"),
            ("auth_expired", "Connection needs renewing"),
            ("rate_limited", "Delayed (quota)"),
        ],
        string="Status",
        default="new",
        required=True,
        readonly=True,
        tracking=True,
    )
    last_connected_at = fields.Datetime(
        string="Last successful connection",
        readonly=True,
        help="Kept when a later connection fails, so a temporary outage does "
             "not erase the fact that the instance worked yesterday.",
    )
    last_error = fields.Text(string="Last error", readonly=True)
    liquidity_version = fields.Char(
        string="liquidity_forecast version",
        readonly=True,
        help="Read from the source at connection time. Shown so a definition "
             "change in one customer does not silently make comparisons false.",
    )
    request_timeout = fields.Integer(
        string="Timeout (seconds)",
        default=10,
        required=True,
        help="Hard limit per call in the overview. A source that does not "
             "answer in time shows as not answering — the view never waits "
             "longer than this.",
    )
    company_ids = fields.One2many(
        "liquidity.tenant.company",
        "tenant_id",
        string="Companies",
    )
    company_count = fields.Integer(
        string="Company count",
        compute="_compute_company_count",
    )

    _sql_constraints = [
        ("name_uniq", "unique(name)", "A source with this name already exists."),
    ]

    @api.constrains("source_type", "db_name", "login", "base_url")
    def _check_odoo_fields(self):
        """An Odoo source needs a database and a login; a driven one must not.

        Enforced here rather than as ``required=True`` because the requirement
        depends on the source type -- and a driven source carrying an Odoo
        login would be a contradiction the user cannot see.
        """
        for tenant in self:
            if tenant.source_type == "odoo":
                # A login may live on an account row instead of on the tenant,
                # so the check accepts either.
                has_login = bool(tenant.login) or bool(
                    tenant.account_ids.filtered("active")
                )
                if not tenant.db_name or not has_login:
                    raise ValidationError(
                        _("An Odoo source needs a database and at least one "
                          "login -- on the source or on an account.")
                    )
            elif tenant.db_name or tenant.login:
                raise ValidationError(
                    _("A driven source (%(type)s) has no Odoo database or "
                      "login. Clear those fields.",
                      type=tenant.source_type)
                )

    @api.depends("account_ids")
    def _compute_account_count(self):
        for tenant in self:
            tenant.account_count = len(tenant.account_ids)

    @api.depends("company_ids")
    def _compute_company_count(self):
        for tenant in self:
            tenant.company_count = len(tenant.company_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("base_url"):
                vals["base_url"] = vals["base_url"].rstrip("/")
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("base_url"):
            vals["base_url"] = vals["base_url"].rstrip("/")
        if "source" in vals:
            # A customer is either Vertel's or an agency's, and the commission
            # rule depends on that being unambiguous. Changing it later would
            # let a customer be re-labelled to create a claim that did not
            # exist when they were onboarded.
            for tenant in self:
                if tenant.source and tenant.source != vals["source"]:
                    raise ValidationError(
                        _("Who the customer belongs to is set at onboarding "
                          "and cannot be changed. %(name)s is %(current)s.",
                          name=tenant.name, current=tenant.source)
                    )
        return super().write(vals)

    # -- credentials -------------------------------------------------------

    def _get_account_for(self, user=None):
        """The account a given consultant uses against this source.

        Falls back to the tenant's own ``keykeep_ref`` when no account rows
        exist, so an installation that has not been migrated yet keeps working.
        That fallback is deliberately visible in the code: it is the one place
        the old one-account-per-customer model survives.
        """
        self.ensure_one()
        accounts = self.account_ids.filtered("active")
        if not accounts:
            return self.browse()
        if user:
            mine = accounts.filtered(lambda a: a.consultant_id == user)
            if mine:
                return mine[:1]
        # No consultant given, or none of the accounts is theirs: use the
        # first active one. The caller records which, so the read is not
        # anonymous even when it is not personal.
        return accounts[:1]

    def _get_api_key(self, account=None):
        """Fetch the API key from keykeep.

        There is no plain field to fall back to, by design. The key is read
        through keykeep's integration path (``system=True``), which writes a
        ``system_read`` entry to the credential's access log — so every read
        this module makes is auditable at the credential.
        """
        self.ensure_one()
        reference = (account.keykeep_ref if account else False) or self.keykeep_ref
        if not reference:
            return None
        Credential = self.env.get("keykeep.credential")
        if Credential is None:
            _logger.warning(
                "keykeep is not installed; cannot resolve %s", reference
            )
            return None
        credential = Credential.sudo().search(
            [("name", "=", reference)], limit=1
        )
        if not credential:
            _logger.warning("keykeep credential %s not found", reference)
            return None
        return credential._read_encrypted("key_value", system=True)

    def _get_driver_credential(self):
        """Resolve a driver credential (OAuth tokens) from keykeep as a dict.

        A driver has no database handle on purpose, so the secret is passed as
        plain values and never written to a model field. Returns None when
        keykeep has nothing usable, which the row reports as "not configured".
        """
        self.ensure_one()
        if not self.keykeep_ref:
            return None
        Credential = self.env.get("keykeep.credential")
        if Credential is None:
            _logger.warning(
                "keykeep is not installed; cannot resolve %s", self.keykeep_ref
            )
            return None
        credential = Credential.sudo().search(
            [("name", "=", self.keykeep_ref)], limit=1
        )
        if not credential:
            _logger.warning("keykeep credential %s not found", self.keykeep_ref)
            return None
        blob = credential._read_encrypted("key_value", system=True)
        if not blob:
            return None
        try:
            import json
            return json.loads(blob)
        except (TypeError, ValueError):
            _logger.warning(
                "keykeep credential %s is not a JSON credential blob",
                self.keykeep_ref,
            )
            return None

    def _store_driver_credential(self, values):
        """Write a rotated token blob back to keykeep. Best effort by design."""
        self.ensure_one()
        if not self.keykeep_ref:
            return
        Credential = self.env.get("keykeep.credential")
        if Credential is None:
            return
        credential = Credential.sudo().search(
            [("name", "=", self.keykeep_ref)], limit=1
        )
        if not credential:
            return
        import json
        # keykeep exposes no public write helper; the ciphertext column is the
        # documented storage and ``_encrypt_with`` is its documented inverse.
        credential.write({
            "key_value_cipher": credential._encrypt_with(
                json.dumps(values), credential._get_credential_key()
            ),
        })

    def _connect(self, account=None):
        """Authenticate against the source and return (client, result)."""
        self.ensure_one()
        account = account or self._get_account_for()
        api_key = self._get_api_key(account)
        if not api_key:
            return None, rpc_client.RpcResult(
                rpc_client.AUTH_ERROR,
                message=_("No API key available for this source."),
            )
        return rpc_client.connect(
            self.base_url,
            self.db_name,
            (account.login if account else False) or self.login,
            api_key,
            timeout=self.request_timeout or rpc_client.DEFAULT_TIMEOUT,
        )

    # -- actions -----------------------------------------------------------

    def action_test_connection(self):
        """Check that the source answers and the credential works.

        Two paths, because the two source kinds authenticate differently: an
        Odoo source with an API key over JSON-RPC, a driven source with OAuth
        over its own API. Sharing one path would mean pretending a Fortnox
        token is an Odoo key.
        """
        for tenant in self:
            if tenant.source_type != "odoo":
                tenant._test_driver_connection()
                continue
            client, result = tenant._connect()
            if not result.ok:
                tenant._record_failure(result)
                continue

            version = rpc_client.module_version(client)
            if not version.ok:
                tenant.write({
                    "state": "no_liquidity",
                    "last_error": version.message,
                })
                continue

            tenant.write({
                "state": "ok",
                "last_connected_at": fields.Datetime.now(),
                "last_error": False,
                "liquidity_version": version.value,
            })
        return True

    def _test_driver_connection(self):
        """Test a driven source through its own driver. Never raises."""
        self.ensure_one()
        driver_class = None
        # Imported here rather than at module level: the driver table lives
        # with the bookkeeping row, and importing it at the top would make the
        # tenant model depend on the company model's module.
        from .liquidity_tenant_company_driver import DRIVERS

        driver_class = DRIVERS.get(self.source_type)
        if not driver_class:
            self.write({
                "state": "error",
                "last_error": _("No driver is configured for %s")
                % self.source_type,
            })
            return

        credential = self._get_driver_credential()
        if not credential:
            self.write({
                "state": "auth_error",
                "last_error": _(
                    "No credential in keykeep for this source. The driver needs "
                    "client_id, client_secret and a refresh token."
                ),
            })
            return
        if self.source_type == "visma":
            credential = dict(credential)
            credential.setdefault("product", self.visma_product)

        try:
            driver = driver_class(credential)
            result = driver.test_connection()
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            _logger.exception("Driver connection test failed for %s", self.name)
            self.write({"state": "error", "last_error": str(exc)[:500]})
            return

        if result.ok:
            self.write({
                "state": "ok",
                "last_connected_at": fields.Datetime.now(),
                "last_error": False,
                "liquidity_version": False,
            })
            return

        state = {
            "auth_expired": "auth_expired",
            "rate_limited": "rate_limited",
            "unknown_product": "error",
            "unreachable": "error",
        }.get(result.kind, "error")
        self.write({"state": state, "last_error": result.message})

    def _record_failure(self, result):
        """Store a failed connection without erasing the last good timestamp."""
        self.ensure_one()
        state = {
            rpc_client.UNREACHABLE: "error",
            rpc_client.AUTH_ERROR: "auth_error",
            rpc_client.NO_LIQUIDITY: "no_liquidity",
        }.get(result.outcome, "error")
        self.write({"state": state, "last_error": result.message})

    def action_import_companies(self):
        """Fetch the company list and create or update rows.

        Option (c): import everything, let the firm hide what it does not work
        with. Hiding is reversible; deleting would lose the local notes and the
        selection.

        A driven source has no company list to import -- one connection is one
        customer's bookkeeping. A single row is created instead, so the
        portfolio treats both kinds the same way downstream.
        """
        self.ensure_one()
        if self.source_type != "odoo":
            return self._import_driver_company()
        client, result = self._connect()
        if not result.ok:
            self._record_failure(result)
            raise UserError(
                _("Could not reach %(name)s: %(msg)s")
                % {"name": self.name, "msg": result.message}
            )

        fetched = kpi_reader.companies(client)
        if not fetched.ok:
            self._record_failure(fetched)
            raise UserError(
                _("Could not read the company list: %s") % fetched.message
            )

        Company = self.env["liquidity.tenant.company"]
        seen_refs = set()
        now = fields.Datetime.now()

        for row in fetched.value or []:
            company_ref = row["id"]
            seen_refs.add(company_ref)
            currency = row.get("currency_id")
            currency_name = (
                currency[1] if isinstance(currency, (list, tuple)) else False
            )

            # active_test=False is essential: the unique constraint covers
            # archived rows too. A hidden company that search() cannot see
            # would be created again and hit the constraint.
            existing = Company.with_context(active_test=False).search(
                [("tenant_id", "=", self.id), ("company_ref", "=", company_ref)],
                limit=1,
            )
            values = {
                "name": row.get("name") or _("Company %s") % company_ref,
                "currency_name": currency_name,
                "last_seen_at": now,
                "missing_since": False,
            }
            if existing:
                existing.write(values)
            else:
                values.update({
                    "tenant_id": self.id,
                    "company_ref": company_ref,
                    "first_seen_at": now,
                })
                Company.create(values)

        # Companies that used to be here but are gone from the answer. Marked,
        # never deleted: the row may carry notes the firm still needs.
        gone = Company.with_context(active_test=False).search([
            ("tenant_id", "=", self.id),
            ("company_ref", "not in", list(seen_refs) or [0]),
            ("missing_since", "=", False),
        ])
        gone.write({"missing_since": now})

        self.write({
            "state": "ok",
            "last_connected_at": now,
            "last_error": False,
        })
        return True

    @api.model
    def _migrate_single_account_to_accounts(self):
        """Turn each tenant's own login into an account row.

        Idempotent: it only creates a row when the tenant has a ``login`` and
        no account row with that login yet. Running it twice is harmless,
        which matters because it runs from a migration script.

        ``is_agency_account=False`` on the migrated row is deliberate: the old
        model could not tell whose account it was, so the migration must not
        claim it was the firm's.
        """
        Account = self.env["liquidity.tenant.account"].sudo()
        created = Account.browse()
        for tenant in self.sudo().search([("login", "!=", False)]):
            existing = Account.with_context(active_test=False).search([
                ("tenant_id", "=", tenant.id),
                ("login", "=", tenant.login),
            ], limit=1)
            if existing:
                continue
            created |= Account.create({
                "tenant_id": tenant.id,
                "login": tenant.login,
                "keykeep_ref": tenant.keykeep_ref or "",
                "is_agency_account": False,
                "agency_partner_id": tenant.owning_agency_id.id or False,
            })
        _logger.info(
            "Migrated %s tenant logins into account rows", len(created)
        )
        return created

    # -- bridge at the customer -------------------------------------------

    @api.model
    def _bridge_key(self):
        """The bridge key for this source. Separate from the read key.

        The bridge key is narrower than the read key: it cannot read the
        ledger, only ask for a session and deliver messages. Keeping it in its
        own keykeep entry means revoking one does not revoke the other.
        """
        self.ensure_one()
        reference = self.bridge_keykeep_ref
        if not reference:
            return None
        Credential = self.env.get("keykeep.credential")
        if Credential is None:
            return None
        credential = Credential.sudo().search(
            [("name", "=", reference)], limit=1
        )
        if not credential:
            return None
        return credential._read_encrypted("key_value", system=True)

    def _bridge_call(self, path, payload=None, method="GET"):
        """One call to the customer's bridge. Returns a dict, never raises."""
        self.ensure_one()
        import json
        import requests

        key = self._bridge_key()
        if not key:
            return {"ok": False, "reason": "no bridge key configured"}
        url = "%s%s" % ((self.base_url or "").rstrip("/"), path)
        try:
            response = requests.request(
                method,
                url,
                data=json.dumps(payload) if payload is not None else None,
                headers={
                    "Content-Type": "application/json",
                    "X-Bridge-Key": key,
                },
                timeout=self.request_timeout or 10,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return {"ok": False, "reason": str(exc)[:200]}

        if response.status_code == 401:
            return {"ok": False, "reason": "the bridge key was refused"}
        if response.status_code == 403:
            try:
                body = response.json()
            except ValueError:
                body = {}
            return {
                "ok": False,
                "reason": body.get("message") or body.get("error")
                          or "the bridge refused",
            }
        if response.status_code >= 400:
            return {"ok": False, "reason": "HTTP %s" % response.status_code}
        try:
            return {"ok": True, "data": response.json()}
        except ValueError:
            return {"ok": False, "reason": "the bridge answered with non-JSON"}

    #: The bridge versions this module knows how to talk to. A tuple of
    #: (major, minor) -- patch releases are compatible by definition.
    #:
    #: Kept here rather than read from the bridge: the *firm* decides what it
    #: supports, and a bridge that claims compatibility should not be believed
    #: without the firm agreeing.
    SUPPORTED_BRIDGE_VERSIONS = ((18, 0),)

    def _bridge_version_supported(self, version_string):
        """Whether a bridge version is one this module can talk to.

        Returns (ok, reason). An unparseable version is *not* supported: a
        bridge that cannot say what it is cannot be trusted to behave like
        anything in particular.
        """
        if not version_string:
            return False, "the bridge did not report its version"
        try:
            parts = str(version_string).split(".")
            major, minor = int(parts[0]), int(parts[1])
        except (IndexError, TypeError, ValueError):
            return False, "the bridge reported an unreadable version (%s)" % (
                version_string,
            )
        if (major, minor) not in self.SUPPORTED_BRIDGE_VERSIONS:
            return False, (
                "the customer's bridge is version %s, this module supports %s"
                % (version_string, ", ".join(
                    "%s.%s" % v for v in self.SUPPORTED_BRIDGE_VERSIONS
                ))
            )
        return True, ""

    def _bridge_status(self):
        """Is the bridge usable, and if not, why.

        The three reasons are kept apart because they need different actions:
        install the module, ask the customer to allow impersonation, or fix
        the key.
        """
        self.ensure_one()
        if self.source_type != "odoo":
            return {"available": False, "reason": "not an Odoo source"}
        if not self.bridge_keykeep_ref:
            return {"available": False,
                    "reason": "no bridge key — the customer has no bridge, "
                              "or it has not been connected"}
        result = self._bridge_call("/liquidity_bridge/status")
        if not result["ok"]:
            return {"available": False, "reason": result["reason"]}
        data = result["data"] or {}
        version_ok, version_reason = self._bridge_version_supported(
            data.get("version")
        )
        if not version_ok:
            # A version mismatch is its own reason: the fix is an upgrade on
            # one side, not a setting. Saying "does not allow" would send the
            # reader to the wrong screen.
            return {"available": False, "reason": version_reason}
        if not data.get("enabled"):
            return {"available": False,
                    "reason": "the bridge is switched off at the customer"}
        if not data.get("allows_impersonation"):
            return {"available": False,
                    "reason": "the customer does not allow the firm to open "
                              "their system"}
        return {"available": True, "reason": ""}

    def _request_bridge_session(self, company_ref=None, menu_xmlid=None):
        """Ask for a token. Returns {'url': ..., 'reason': ...}."""
        self.ensure_one()
        account = self._get_account_for(self.env.user)
        login = (account.login if account else False) or self.login
        if not login:
            return {"url": None, "reason": "no login to open as"}

        result = self._bridge_call(
            "/liquidity_bridge/request",
            payload={
                "login": login,
                "company_ref": company_ref or 0,
                "menu_xmlid": menu_xmlid or "",
            },
            method="POST",
        )
        if not result["ok"]:
            return {"url": None, "reason": result["reason"]}
        data = result["data"] or {}
        if not data.get("url"):
            return {"url": None, "reason": "the bridge returned no url"}
        if account:
            account.write({"last_used_at": fields.Datetime.now()})
        return {"url": data["url"], "reason": ""}

    def _import_driver_company(self):
        """Create the single bookkeeping row for a driven source."""
        self.ensure_one()
        Company = self.env["liquidity.tenant.company"]
        now = fields.Datetime.now()
        existing = Company.with_context(active_test=False).search(
            [("tenant_id", "=", self.id)], limit=1
        )
        values = {
            "name": self.name,
            "last_seen_at": now,
            "missing_since": False,
        }
        if existing:
            existing.write(values)
        else:
            values.update({
                "tenant_id": self.id,
                # No company id exists in the source; the tenant itself is the
                # bookkeeping. 0 is used rather than a fake id, and the unique
                # constraint on (tenant, ref) then holds for one row.
                "company_ref": 0,
                "first_seen_at": now,
            })
            Company.create(values)
        self.write({
            "state": "ok",
            "last_connected_at": now,
            "last_error": False,
        })
        return True

    def action_connect_and_import(self):
        """One button for the normal path: connect, then import."""
        self.ensure_one()
        self.action_test_connection()
        if self.state != "ok":
            return True
        return self.action_import_companies()
