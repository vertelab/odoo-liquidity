# -*- coding: utf-8 -*-
from .common import WorkCommon


class TestTemplate(WorkCommon):

    def test_copy_brings_all_lines(self):
        """A plan is a copy, so copy() must carry the lines."""
        copy = self.template.copy({"name": "Kopia"})
        self.assertEqual(len(copy.line_ids), 2)
        self.assertEqual(
            sorted(copy.line_ids.mapped("name")),
            ["Avstämning bank", "Momsredovisning"],
        )

    def test_copy_does_not_touch_the_template(self):
        before = len(self.template.line_ids)
        self.template.copy({"name": "Kopia"})
        self.assertEqual(len(self.template.line_ids), before)

    def test_description_excerpt_strips_html(self):
        """A list view showing raw HTML would show <ul><li> to the user."""
        excerpt = self.line_month.description_excerpt
        self.assertNotIn("<", excerpt)
        self.assertNotIn(">", excerpt)
        self.assertIn("Avstämning" if False else "Stäm av", excerpt)
        self.assertIn("konto 1930", excerpt)

    def test_excerpt_is_truncated(self):
        line = self.env["liquidity.work.template.line"].create({
            "template_id": self.template.id,
            "name": "Lång",
            "description": "<p>%s</p>" % ("ord " * 100),
        })
        self.assertLessEqual(len(line.description_excerpt), 120)
        self.assertTrue(line.description_excerpt.endswith("…"))

    def test_empty_description_gives_no_excerpt(self):
        line = self.env["liquidity.work.template.line"].create({
            "template_id": self.template.id,
            "name": "Tom",
        })
        self.assertFalse(line.description_excerpt)
