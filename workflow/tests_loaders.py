"""Guard: every place that waits on a model shows a working loader.

A new model-calling form or loop added without one fails here, rather than
leaving the founder looking at a page that seems frozen.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

TEMPLATES = Path(__file__).parent / "templates" / "workflow"
# Forms whose submit waits for a model or an outside service: they need data-working.
FORM_ROUTES = ("run_stage1", "run_stage2", "run_stage3", "bakeoff_describe", "bakeoff_check_models",
               "brand_interview_start", "brand_interview_retry", "brand_interview_draft",
               "brand_suggest_options", "bakeoff_brief_test_start")
# Panels the page drives one request at a time: they need an in-panel loader.
LOOP_ROUTES = ("visuals_next", "bakeoff_make_next", "bakeoff_reviewer_next")


class LoaderCoverageTests(SimpleTestCase):
    def sources(self):
        return {p.name: p.read_text() for p in TEMPLATES.rglob("*.html")}

    def test_every_model_form_shows_the_working_overlay(self):
        found = set()
        for name, text in self.sources().items():
            for route in FORM_ROUTES:
                for match in re.finditer(r"<(form|button)[^>]*\{% url '" + route + r"'[^>]*>", text):
                    found.add(route)
                    self.assertIn("data-working=", match.group(0), f"{name}: {route} has no working loader")
        self.assertEqual(found, set(FORM_ROUTES))

    def test_every_model_loop_has_an_in_panel_loader(self):
        found = set()
        for name, text in self.sources().items():
            for route in LOOP_ROUTES:
                for match in re.finditer(r"<div[^>]*data-url=\"\{% url '" + route + r"'[^\"]*\"[^>]*>(.{0,400})", text, re.S):
                    found.add(route)
                    self.assertIn('includes/work_card.html', match.group(1), f"{name}: {route} has no work card")
        self.assertEqual(found, set(LOOP_ROUTES))

    def test_the_interview_answer_form_shows_the_analyst_thinking(self):
        text = (TEMPLATES / "brand_interview.html").read_text()
        match = re.search(r'<form[^>]*id="answer-form"[^>]*>(.{0,300})', text, re.S)
        self.assertIsNotNone(match)
        self.assertIn("includes/thinking_card.html", match.group(1))

    def test_work_card_has_the_parts_the_script_fills(self):
        card = (TEMPLATES / "includes" / "work_card.html").read_text()
        for part in ('class="inline-working', 'class="loaders inline-loaders"', 'class="working-quip"',
                     'data-stat="elapsed"', 'data-stat="done"', 'data-stat="pace"', 'data-stat="eta"', 'class="work-feed"'):
            self.assertIn(part, card)

    def test_overlay_has_fixed_stats_not_a_free_text_line(self):
        base = (TEMPLATES / "base.html").read_text()
        self.assertIn('id="working-stats"', base)
        self.assertIn('id="working-feed"', base)
        self.assertNotIn('id="working-progress"', base)
