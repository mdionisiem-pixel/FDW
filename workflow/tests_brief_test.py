"""Brief test: stage 2 run twice on hidden copies, judged blind, then revealed."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import brief_test
from .llm import DryRunClient, SpendLimitError, check_spend
from .models import BrandBrief, BrandBriefVersion, BriefTest, Cycle, ModelCall
from .tests import approved_cycle


class CapturingClient(DryRunClient):
    """Dry-run answers, recording each prompt so the test can see which brand brief went in."""

    def __init__(self):
        self.prompts = []

    def complete(self, step, prompt, *args, **kwargs):
        self.prompts.append((step, prompt))
        return super().complete(step, prompt, *args, **kwargs)


def earlier_version():
    brief = BrandBrief.current()
    version = brief.snapshot(BrandBriefVersion.Source.BEFORE, "Before the analyst interview was approved")
    brief.vision = "VISION-B: every wardrobe holds one piece made to be handed on."
    brief.text = brief.text.replace("Colour palette (To confirm): named colours with hex values.",
                                    "Colour palette: Natural Ecru #E8DCC6, Hemp #CBB896, Walnut Husk #6E5138.")
    brief.save()
    return version


@override_settings(FDW_DRY_RUN=True, FDW_BRIEF_TEST_LIMIT_USD=Decimal("8"))
class BriefTestFlowTests(TestCase):
    def setUp(self):
        self.cycle = approved_cycle()
        self.version = earlier_version()

    def test_each_run_gets_its_own_brief_and_nothing_else_differs(self):
        client = CapturingClient()
        test = brief_test.create(self.cycle, self.version)
        brief_test.run(test, client)
        test.refresh_from_db()
        self.assertEqual(test.status, BriefTest.Status.DONE)
        plans = [p for step, p in client.prompts if step == "plan_concepts"]
        self.assertEqual(len(plans), 2)
        with_vision = [p for p in plans if "VISION-B" in p]
        self.assertEqual(len(with_vision), 1)
        # Strip the brand briefs: the rest of the two planning prompts is identical.
        stripped = [p.replace(test.brief_a, "").replace(test.brief_b, "") for p in plans]
        self.assertEqual(stripped[0], stripped[1])

    def test_the_copies_are_hidden_and_the_real_cycle_is_untouched(self):
        status_before = self.cycle.status
        test = brief_test.create(self.cycle, self.version)
        brief_test.run(test, DryRunClient())
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, status_before)
        self.assertEqual(self.cycle.concepts.count(), 0)
        self.assertTrue(test.cycle_a.is_test and test.cycle_b.is_test)
        self.assertEqual(test.cycle_a.approved_brief.text, self.cycle.approved_brief.text)
        self.assertFalse(ModelCall.objects.filter(cycle=self.cycle, step="plan_concepts").exists())
        self.assertEqual(test.cycle_a.concepts.count(), 3)
        self.assertEqual(test.cycle_b.concepts.count(), 3)

    def test_sets_are_shown_in_the_stored_random_order_and_revealed_after_judging(self):
        test = brief_test.create(self.cycle, self.version)
        brief_test.run(test, DryRunClient())
        test.first_is_a = False
        test.save(update_fields=["first_is_a"])
        shown = brief_test.sets(test)
        self.assertEqual([(s["number"], s["letter"]) for s in shown], [(1, "B"), (2, "A")])
        with self.assertRaises(brief_test.BriefTestError):
            brief_test.judge(test, {"brand": "1"})
        answers = {"brand": "1", "customer": "1", "goals": "same", "make": "2", "overall": "1"}
        brief_test.judge(test, answers, "Set 1 felt like us.")
        result = brief_test.outcome(test)
        self.assertEqual(result["score"], {"B": 3, "A": 1})
        self.assertEqual([r["winner"] for r in result["rows"]], ["B", "B", None, "A", "B"])

    def test_the_same_brief_on_both_sides_is_refused(self):
        same = BrandBrief.current().snapshot(BrandBriefVersion.Source.HAND, "copy")
        with self.assertRaisesMessage(brief_test.BriefTestError, "nothing to compare"):
            brief_test.create(self.cycle, same)

    def test_a_cost_above_the_limit_is_refused_before_anything_runs(self):
        with override_settings(FDW_BRIEF_TEST_LIMIT_USD=Decimal("1")):
            with self.assertRaisesMessage(brief_test.BriefTestError, "FDW_BRIEF_TEST_LIMIT_USD"):
                brief_test.create(self.cycle, self.version)
        self.assertFalse(BriefTest.objects.exists())

    def test_each_copy_stops_at_half_the_test_limit(self):
        test = brief_test.create(self.cycle, self.version)
        copy = brief_test._copy(test.source_cycle, "A")
        ModelCall.objects.create(cycle=copy, step="plan_concepts", model="m", prompt="p", cost_usd=Decimal("4.00"))
        with self.assertRaisesMessage(SpendLimitError, "FDW_BRIEF_TEST_LIMIT_USD"):
            check_spend(copy, "detail_concept")

    def test_a_failed_run_is_recorded_and_the_other_kept(self):
        class FailsForA(DryRunClient):
            def complete(self, step, prompt, *args, **kwargs):
                if step == "plan_concepts" and "VISION-B" not in prompt:
                    raise RuntimeError("provider down")
                return super().complete(step, prompt, *args, **kwargs)

        test = brief_test.create(self.cycle, self.version)
        brief_test.run(test, FailsForA())
        test.refresh_from_db()
        self.assertEqual(test.status, BriefTest.Status.FAILED)
        self.assertIn("Run A: provider down", test.error)
        self.assertEqual(test.cycle_b.concepts.count(), 3)


class CheckTests(TestCase):
    PALETTE = ["#E8DCC6", "#CBB896", "#6E5138"]

    def piece(self, **fields):
        base = {"name": "Coat", "colours": [], "materials": "100% wool", "production_route": "atelier",
                "target_price_eur": 300}
        base.update(fields)
        return base

    def test_palette_share_counts_colours_near_the_palette(self):
        concepts = [{"pieces": [self.piece(colours=[{"hex": "#E9DDC5"}, {"hex": "#B3262E"}])]}]
        self.assertEqual(brief_test.checks(concepts, self.PALETTE)["palette_share"], 50)

    def test_man_made_fibres_and_handmade_claims_are_found(self):
        concepts = [{"pieces": [
            self.piece(name="Top", materials="80% wool, 20% polyamide"),
            self.piece(name="Scarf", production_route="on_demand", technique="handmade crochet"),
            self.piece(name="Hat", production_route="atelier", technique="hand-made"),
        ]}]
        result = brief_test.checks(concepts, self.PALETTE)
        self.assertEqual(result["man_made"], ["Top: polyamide"])
        self.assertEqual(result["handmade_claims"], ["Scarf"])

    def test_the_palette_is_read_from_the_brief(self):
        text = "Intro\nColour palette: Natural Ecru #e8dcc6, Hemp #CBB896. No bright dyes.\nOther #123456"
        self.assertEqual(brief_test.palette_hexes(text), ["#E8DCC6", "#CBB896"])


@override_settings(FDW_DRY_RUN=True)
class BriefTestPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="pw"))
        self.cycle = approved_cycle()
        self.version = earlier_version()

    def test_run_judge_reveal_and_discard_through_the_pages(self):
        page = self.client.get(reverse("bakeoff_brief_test"))
        self.assertContains(page, "Run the test")
        self.assertContains(page, "Brief test")
        self.client.post(reverse("bakeoff_brief_test_start"), {"cycle": self.cycle.pk, "version": self.version.pk})
        test = BriefTest.objects.get()
        page = self.client.get(reverse("bakeoff_brief_test_detail", args=[test.pk])).content.decode()
        self.assertIn("Set 1", page)
        self.assertIn("Set 2", page)
        self.assertNotIn("current brief</span>", page)  # hidden until judged
        self.client.post(reverse("bakeoff_brief_test_judge", args=[test.pk]),
                         {"brand": "1", "customer": "2", "goals": "1", "make": "same", "overall": "1"})
        page = self.client.get(reverse("bakeoff_brief_test_detail", args=[test.pk])).content.decode()
        self.assertIn("current brief</span>", page)
        self.assertIn("Colours in your palette", page)
        self.client.post(reverse("bakeoff_brief_test_discard", args=[test.pk]))
        self.assertFalse(BriefTest.objects.exists())
        self.assertFalse(Cycle.objects.filter(is_test=True).exists())

    def test_test_copies_never_appear_among_your_cycles(self):
        test = brief_test.create(self.cycle, self.version)
        brief_test.run(test, DryRunClient())
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn("Brief test A", page)
        self.assertNotIn("Brief test B", page)


from django.test import TransactionTestCase  # noqa: E402


@override_settings(FDW_DRY_RUN=True, FDW_PARALLEL_CALLS=3, FDW_BRIEF_TEST_LIMIT_USD=Decimal("8"))
class ParallelBriefTestTests(TransactionTestCase):
    """Both runs at once, each with its own parallel workers, as in the running app."""

    def test_both_runs_finish_in_parallel_without_lock_errors(self):
        cycle = approved_cycle()
        version = earlier_version()
        test = brief_test.create(cycle, version)
        brief_test.run(test, DryRunClient())
        test.refresh_from_db()
        self.assertEqual(test.status, BriefTest.Status.DONE, test.error)
        self.assertEqual(test.cycle_a.concepts.count(), 3)
        self.assertEqual(test.cycle_b.concepts.count(), 3)


class EstimateTests(TestCase):
    def test_the_estimate_is_the_average_real_stage_2_run(self):
        cycle = Cycle.objects.create(name="Real")
        for step, cost in (("plan_concepts", "0.09"), ("detail_concept", "0.57"), ("review_concept", "0.99"),
                           ("revise_concept", "0.94"), ("research_trends", "3.00")):
            ModelCall.objects.create(cycle=cycle, step=step, model="m", prompt="p", cost_usd=Decimal(cost))
        self.assertEqual(brief_test.run_estimate(), Decimal("2.59"))

    def test_runs_that_cost_nothing_fall_back_to_the_default(self):
        cycle = Cycle.objects.create(name="Dry")
        ModelCall.objects.create(cycle=cycle, step="plan_concepts", model="m", prompt="p", cost_usd=Decimal("0"))
        self.assertEqual(brief_test.run_estimate(), brief_test.DEFAULT_RUN_ESTIMATE)
