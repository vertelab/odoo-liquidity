# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase


class WorkCommon(TransactionCase):
    """Shared fixtures: a bookkeeping, a template with lines, a product."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env["liquidity.tenant"].create({
            "name": "Testbyrån",
            "base_url": "https://example.test",
            "db_name": "db",
            "login": "user",
            "keykeep_ref": "k",
        })
        cls.bookkeeping = cls.env["liquidity.tenant.company"].create({
            "tenant_id": cls.tenant.id,
            "company_ref": 1,
            "name": "Alfa AB",
            "currency_name": "SEK",
        })
        cls.template = cls.env["liquidity.work.template"].create({
            "name": "Månadsbokföring",
            "company_id": cls.bookkeeping.id,
        })
        cls.line_month = cls.env["liquidity.work.template.line"].create({
            "template_id": cls.template.id,
            "name": "Avstämning bank",
            "description": (
                "<p>Stäm av <strong>banken</strong></p>"
                "<ul><li>konto 1930</li></ul>"
            ),
            "interval_number": 1,
            "interval_type": "month",
            "offset_days": 4,
        })
        cls.line_quarter = cls.env["liquidity.work.template.line"].create({
            "template_id": cls.template.id,
            "name": "Momsredovisning",
            "interval_number": 3,
            "interval_type": "month",
            "offset_days": 10,
        })
