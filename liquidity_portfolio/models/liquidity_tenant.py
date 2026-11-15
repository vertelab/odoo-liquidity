# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

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
    base_url = fields.Char(
        string="Base URL",
        required=True,
        help="For example https://kund.vertel.se. No trailing slash.",
    )
    db_name = fields.Char(
        string="Database",
        required=True,
        help="The Odoo database name in the source instance.",
    )
    login = fields.Char(
        string="Login",
        required=True,
        help="The Odoo user the API key belongs to. Use the same user who "
             "bookkeeps for this customer, so the key is tied to a real account "
             "and can be revoked when that person leaves.",
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
        """Check that the source answers, the key works and the module exists."""
        for tenant in self:
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
        """
        self.ensure_one()
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

    def action_connect_and_import(self):
        """One button for the normal path: connect, then import."""
        self.ensure_one()
        self.action_test_connection()
        if self.state != "ok":
            return True
        return self.action_import_companies()
