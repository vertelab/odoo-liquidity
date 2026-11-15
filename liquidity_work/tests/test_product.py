# -*- coding: utf-8 -*-
from odoo.exceptions import ValidationError

from .common import WorkCommon


class TestProduct(WorkCommon):

    def test_product_without_flag_needs_no_template(self):
        product = self.env["product.template"].create({
            "name": "Bokföring utan plan",
            "type": "service",
        })
        self.assertEqual(product.liquidity_tracking, "no")
        self.assertFalse(product.liquidity_template_id)

    def test_flagged_product_without_template_is_refused(self):
        """A flag without a template is a half-finished setting that would
        otherwise be discovered at the first sale."""
        with self.assertRaises(ValidationError):
            self.env["product.template"].create({
                "name": "Ofullständig",
                "type": "service",
                "liquidity_tracking": "work_plan",
            })

    def test_flag_with_template_is_accepted(self):
        product = self.env["product.template"].create({
            "name": "Månadsbokföring",
            "type": "service",
            "liquidity_tracking": "work_plan",
            "liquidity_template_id": self.template.id,
        })
        self.assertEqual(product.liquidity_template_id, self.template)

    def test_template_on_product_without_flag_is_refused(self):
        with self.assertRaises(ValidationError):
            self.env["product.template"].create({
                "name": "Motsägelsefull",
                "type": "service",
                "liquidity_tracking": "no",
                "liquidity_template_id": self.template.id,
            })
