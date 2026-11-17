# -*- coding: utf-8 -*-
"""The bridge's configuration, read from system parameters.

Everything here is a *decision the customer made*, not a default we chose. The
defaults are therefore the restrictive ones: no agency, no impersonation, no
outgoing events. A bridge that is installed and forgotten must not be a bridge
that grants access.
"""
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

#: Which firm this installation belongs to. Free text: it is a label written
#: into the access log, so the customer can see who opened what.
PARAM_AGENCY = "liquidity_bridge.agency_name"

#: The firm's bridge key, compared against the ``X-Bridge-Key`` header. Stored
#: as a parameter because it is a shared secret between two installations, not
#: a per-user credential. Rotating it is a deliberate act, not a user action.
PARAM_BRIDGE_KEY = "liquidity_bridge.bridge_key"

#: Master switch. When off, every endpoint answers 403 and says why. This is
#: the switch a customer flips when they want the firm out of their system
#: without uninstalling anything.
PARAM_ENABLED = "liquidity_bridge.enabled"

#: Whether agency accounts may exist here at all. Separate from ``enabled``:
#: a customer may allow the firm to read and to receive events, but not to have
#: accounts in their Odoo.
PARAM_ALLOW_AGENCY_ACCOUNTS = "liquidity_bridge.allow_agency_accounts"

#: Whether impersonation is allowed at all. Off by default -- this is the one
#: setting that turns a read-only relationship into a logged-in one.
PARAM_ALLOW_IMPERSONATION = "liquidity_bridge.allow_impersonation"

#: How long an impersonation token lives, in seconds. Short on purpose: the
#: token is redeemed by a browser redirect within seconds of being issued.
PARAM_TOKEN_TTL = "liquidity_bridge.token_ttl_seconds"
DEFAULT_TOKEN_TTL = 60

#: Where the firm receives events. Empty means "queue them and wait", which is
#: a valid configuration: the firm may poll instead of being pushed to.
PARAM_AGENCY_URL = "liquidity_bridge.agency_url"


class BridgeConfig(models.AbstractModel):
    """Typed access to the parameters, with the restrictive defaults."""

    _name = "liquidity.bridge.config"
    _description = "Bridge configuration"

    @api.model
    def _get(self, key, default=None):
        return self.env["ir.config_parameter"].sudo().get_param(key, default)

    @api.model
    def _get_bool(self, key, default=False):
        value = self._get(key)
        if value is None:
            return default
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    @api.model
    def is_enabled(self):
        """The master switch. Off means every endpoint refuses."""
        return self._get_bool(PARAM_ENABLED, False)

    @api.model
    def agency_name(self):
        return self._get(PARAM_AGENCY, "")

    @api.model
    def bridge_key(self):
        return self._get(PARAM_BRIDGE_KEY, "")

    @api.model
    def allows_agency_accounts(self):
        return self._get_bool(PARAM_ALLOW_AGENCY_ACCOUNTS, False)

    @api.model
    def allows_impersonation(self):
        return self._get_bool(PARAM_ALLOW_IMPERSONATION, False)

    @api.model
    def token_ttl(self):
        try:
            return int(self._get(PARAM_TOKEN_TTL, DEFAULT_TOKEN_TTL))
        except (TypeError, ValueError):
            # A malformed parameter must not make tokens live forever.
            return DEFAULT_TOKEN_TTL

    @api.model
    def agency_url(self):
        return self._get(PARAM_AGENCY_URL, "")

    @api.model
    def status(self):
        """One dict for the settings screen and for the firm's connection test."""
        return {
            "enabled": self.is_enabled(),
            "agency": self.agency_name(),
            "allows_agency_accounts": self.allows_agency_accounts(),
            "allows_impersonation": self.allows_impersonation(),
            "token_ttl": self.token_ttl(),
            "has_key": bool(self.bridge_key()),
            "agency_url": self.agency_url(),
        }
