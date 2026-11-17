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
                if not tenant.db_name or not tenant.login:
                    raise ValidationError(
                        _("An Odoo source needs both a database and a login.")
                    )
            elif tenant.db_name or tenant.login:
                raise ValidationError(
                    _("A driven source (%(type)s) has no Odoo database or "
                      "login. Clear those fields.",
                      type=tenant.source_type)
                )

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
        return super().write(vals)

    # -- credentials -------------------------------------------------------

    def _get_api_key(self):
        """Fetch the API key from keykeep.

        There is no plain field to fall back to, by design. The key is read
        through keykeep's integration path (``system=True``), which writes a
        ``system_read`` entry to the credential's access log — so every read
        this module makes is auditable at the credential.
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
            _logger.warning(
                "keykeep credential %s not found", self.keykeep_ref
            )
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

    def _connect(self):
        """Authenticate against the source and return (client, result)."""
        self.ensure_one()
        api_key = self._get_api_key()
        if not api_key:
            return None, rpc_client.RpcResult(
                rpc_client.AUTH_ERROR,
                message=_("No API key available for this source."),
            )
        return rpc_client.connect(
            self.base_url,
            self.db_name,
            self.login,
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
