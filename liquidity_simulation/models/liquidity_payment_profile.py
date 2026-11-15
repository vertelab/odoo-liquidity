# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models

from ..services import distributions

_logger = logging.getLogger(__name__)

#: How many payments a partner needs before the history is treated as a
#: distribution at all. Below this the profile is ``low`` and falls back.
CONFIDENCE_HIGH_MIN = 20
CONFIDENCE_MEDIUM_MIN = 5

#: A receivable older than this many days past maturity counts as defaulted
#: when ``p_default`` is computed.
DEFAULT_AGE_DAYS = 90

#: How many observed delays are kept per profile. Enough to bootstrap from,
#: small enough that the field stays readable.
MAX_OBSERVATIONS = 200


class LiquidityPaymentProfile(models.Model):
    _name = "liquidity.payment.profile"
    _description = "Partner Payment Behaviour Profile"
    _order = "partner_id"
    _rec_name = "partner_id"

    partner_id = fields.Many2one(
        "res.partner",
        string="Partner",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id",
        readonly=True,
    )
    direction = fields.Selection(
        selection=[
            ("receivable", "Customer (receivable)"),
            ("payable", "Vendor (payable)"),
        ],
        string="Direction",
        default="receivable",
        required=True,
        help="Payment behaviour as a customer is not the same as payment "
             "behaviour as a vendor. One profile per partner, company and "
             "direction — never one profile for both.",
    )
    active = fields.Boolean(default=True)

    sample_size = fields.Integer(
        string="Payments",
        readonly=True,
        help="Number of reconciled payments this profile was built from. "
             "Always reported, never omitted — a distribution without a "
             "stated sample size is a claim without evidence.",
    )
    mean_delay_days = fields.Float(
        string="Mean Delay (days)",
        readonly=True,
        help="Mean days between due date and actual payment date. May be "
             "negative — a customer who pays before the due date.",
    )
    stdev_delay_days = fields.Float(
        string="Delay Spread (days)",
        readonly=True,
    )
    p_default = fields.Float(
        string="Default Probability",
        readonly=True,
        help="Share of the partner's receivable amount that is older than "
             "the default age and still unreconciled. Used as the "
             "probability that an invoice is never paid inside the horizon.",
    )
    default_age_days = fields.Integer(
        string="Default Age (days)",
        default=DEFAULT_AGE_DAYS,
        help="Receivables older than this count as defaulted when "
             "p_default is computed.",
    )
    outlier_cap_days = fields.Float(
        string="Outlier Cap (days)",
        readonly=True,
        help="Delays are capped at this value when sampled. Default 3 x "
             "spread, never below 90 days. The cap is applied at sampling, "
             "so the observed history and sample_size stay unchanged.",
    )
    distribution = fields.Selection(
        selection=[
            ("empirical", "Empirical (own history)"),
            ("fallback", "Fallback (industry default)"),
        ],
        string="Distribution",
        default="fallback",
        readonly=True,
        help="empirical: sampled from this partner's own observed delays.\n"
             "fallback: too little history; a conservative default is used "
             "and confidence is low.",
    )
    confidence = fields.Selection(
        selection=[
            ("high", "High"),
            ("medium", "Medium"),
            ("low", "Low"),
        ],
        string="Confidence",
        default="low",
        readonly=True,
        help="high: 20 payments or more.\n"
             "medium: 5 to 19.\n"
             "low: fewer than 5 — the band is wide because the data is "
             "thin, and the UI says so in plain text.",
    )
    risk_score = fields.Integer(
        string="Customer Risk Score",
        readonly=True,
        help="Composite 0-100 score from mn_finance_insights "
             "(finance.customer.risk), used as input when the profile is "
             "low and a score exists. 0 is good, 100 is bad.",
    )
    observations = fields.Text(
        string="Observed Delays",
        readonly=True,
        help="The individual delays in days that the mean and spread were "
             "computed from, so the profile can be audited rather than "
             "trusted. Capped at %s values." % MAX_OBSERVATIONS,
    )
    last_refresh = fields.Datetime(
        string="Refreshed",
        readonly=True,
    )

    _sql_constraints = [
        (
            "profile_partner_company_direction_uniq",
            "unique(partner_id, company_id, direction)",
            "A partner can only have one payment profile per company and "
            "direction.",
        ),
    ]

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------
    def _get_observation_values(self):
        """Observed delays in days for this partner and direction.

        Taken from ``account.partial.reconcile``: the difference between the
        payment date and the invoice's due date. Only fully or partly
        reconciled receivable/payable lines carry a payment date at all, so
        the reconcile rows *are* the payment history — an invoice that was
        never paid contributes nothing, which is correct: it says nothing
        about how fast this partner pays when they do pay.
        """
        self.ensure_one()
        account_type = (
            "asset_receivable" if self.direction == "receivable" else "liability_payable"
        )
        # ``account_type`` is a computed field in Odoo 18 -- it is not a
        # column on ``account_move_line``. The type lives on
        # ``account_account``, so the join is where the filter belongs.
        self.env.cr.execute(
            """
            SELECT MAX(apr.max_date) AS paid_on,
                   aml.date_maturity
              FROM account_partial_reconcile apr
              JOIN account_move_line aml
                ON aml.id = apr.debit_move_id
              JOIN account_account acc
                ON acc.id = aml.account_id
             WHERE aml.partner_id = %s
               AND aml.company_id = %s
               AND acc.account_type = %s
               AND aml.date_maturity IS NOT NULL
               AND aml.parent_state = 'posted'
             GROUP BY aml.id, aml.date_maturity
            """,
            (self.partner_id.id, self.company_id.id, account_type),
        )
        values = []
        for paid_on, maturity in self.env.cr.fetchall():
            if not paid_on or not maturity:
                continue
            values.append((paid_on - maturity).days)
        return values

    def _compute_stats(self):
        """Rebuild mean, spread, observations and the outlier cap."""
        for profile in self:
            values = profile._get_observation_values()
            mean, stdev = distributions.mean_and_stdev(values)
            profile.sample_size = len(values)
            profile.mean_delay_days = mean
            profile.stdev_delay_days = stdev
            profile.outlier_cap_days = distributions.outlier_cap_days(stdev)
            profile.observations = ",".join(
                str(v) for v in values[:MAX_OBSERVATIONS]
            ) or False
            profile.distribution, profile.confidence = profile._classify_confidence(
                len(values)
            )
        return True

    @api.model
    def _classify_confidence(self, sample_size):
        """Map the sample size to (distribution, confidence).

        The thresholds are stated once, here, so the UI, the engine and the
        tests cannot drift apart.
        """
        if sample_size >= CONFIDENCE_HIGH_MIN:
            return "empirical", "high"
        if sample_size >= CONFIDENCE_MEDIUM_MIN:
            return "empirical", "medium"
        return "fallback", "low"

    def _compute_p_default(self):
        """Share of open receivable amount past the default age."""
        for profile in self:
            account_type = (
                "asset_receivable"
                if profile.direction == "receivable"
                else "liability_payable"
            )
            lines = (
                self.env["account.move.line"]
                .sudo()
                .search(
                    [
                        ("partner_id", "=", profile.partner_id.id),
                        ("company_id", "=", profile.company_id.id),
                        ("account_type", "=", account_type),
                        ("parent_state", "=", "posted"),
                        ("reconciled", "=", False),
                        ("amount_residual", "!=", 0.0),
                    ]
                )
            )
            total = sum(abs(line.amount_residual) for line in lines)
            if not total:
                profile.p_default = 0.0
                continue
            cutoff = fields.Date.context_today(self)
            overdue = sum(
                abs(line.amount_residual)
                for line in lines
                if line.date_maturity
                and (cutoff - line.date_maturity).days > profile.default_age_days
            )
            profile.p_default = overdue / total
        return True

    def _compute_risk_score(self):
        """Pull the composite customer risk score when the profile is thin.

        ``finance.customer.risk`` lives in ``mn_finance_insights`` and scores
        0-100 where higher is worse. It is used as an *input* to the fallback
        delay — never as a replacement for the profile's own numbers.
        """
        Risk = self.env.get("finance.customer.risk")
        for profile in self:
            profile.risk_score = 0
            if profile.confidence != "low" or not Risk:
                continue
            try:
                data = Risk.get_risk_data(
                    {
                        "company_ids": [profile.company_id.id],
                    }
                )
            except Exception:  # noqa: BLE001 - a missing score must not block
                _logger.info(
                    "Could not read customer risk score for partner %s",
                    profile.partner_id.id,
                )
                continue
            for row in data.get("rows", []):
                if row.get("partner_id") == profile.partner_id.id:
                    profile.risk_score = int(row.get("score") or 0)
                    break
        return True

    def action_refresh(self):
        """Rebuild one profile from scratch."""
        for profile in self:
            profile._compute_stats()
            profile._compute_p_default()
            profile._compute_risk_score()
            profile.last_refresh = fields.Datetime.now()
        return True

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------
    def _get_delay_distribution(self):
        """Return the distribution this profile should be sampled from."""
        self.ensure_one()
        observations = []
        if self.observations:
            for chunk in self.observations.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    observations.append(float(chunk))
                except ValueError:
                    continue
        mean = self.mean_delay_days
        stdev = self.stdev_delay_days
        if self.confidence == "low" and self.risk_score:
            # A bad risk score (higher = worse) pushes the fallback delay up.
            # Still conservative: the score can only make the assumption
            # gloomier, never rosier.
            mean = max(
                mean or 0.0,
                distributions.FALLBACK_PROFILE["mean_delay_days"]
                + (self.risk_score - 50) * 0.4,
            )
        return distributions.build_delay_dist(
            self.distribution,
            mean,
            stdev,
            observations=observations,
            cap_days=self.outlier_cap_days or None,
        )

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    @api.model
    def _cron_refresh_payment_profiles(self):
        """Rebuild every profile, weekly.

        Payment behaviour changes slowly, so a weekly rebuild is enough and
        keeps the nightly simulation cheap.
        """
        profiles = self.sudo().search([])
        _logger.info("Refreshing %s liquidity payment profiles", len(profiles))
        profiles.action_refresh()
        return True

    # ------------------------------------------------------------------
    # Helpers used by the engine
    # ------------------------------------------------------------------
    @api.model
    def _get_or_create_for_partner(self, partner, company, direction="receivable"):
        """Return the profile for a partner, creating a bare one if needed.

        A profile is created with ``fallback``/``low`` and no history; the
        weekly cron fills it in. Creating it here means a partner that
        appears in the ledger today is never silently absent from the
        simulation.
        """
        Profile = self.sudo()
        profile = Profile.search(
            [
                ("partner_id", "=", partner.id),
                ("company_id", "=", company.id),
                ("direction", "=", direction),
            ],
            limit=1,
        )
        if profile:
            return profile
        return Profile.create(
            {
                "partner_id": partner.id,
                "company_id": company.id,
                "direction": direction,
            }
        )
