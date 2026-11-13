"""Tests for running independent model calls in parallel, and for lighter review effort."""
import threading
import time

from django.test import TestCase, TransactionTestCase, override_settings

from . import stages
from .llm import DryRunClient, StageError
from .models import Concept, Cycle, ModelCall
from .tests import ScriptedClient, approved_cycle


class SlowClient(DryRunClient):
    """Sample responses that take a moment, recording how many calls overlap."""

    def __init__(self, delay=0.3, fail_step=None):
        self.delay = delay
        self.fail_step = fail_step
        self.lock = threading.Lock()
        self.running = 0
        self.most_at_once = 0

    def complete(self, step, prompt, model, max_tokens, web_search_max=0, images=None, effort=""):
        with self.lock:
            self.running += 1
            self.most_at_once = max(self.most_at_once, self.running)
        try:
            time.sleep(self.delay)
            if step == self.fail_step:
                raise StageError(f"Simulated failure in {step}.")
            return super().complete(step, prompt, model, max_tokens, web_search_max, images, effort)
        finally:
            with self.lock:
                self.running -= 1


@override_settings(FDW_PARALLEL_CALLS=3)
class ParallelStage2Tests(TransactionTestCase):
    def test_three_collections_are_worked_on_at_the_same_time(self):
        cycle = approved_cycle()
        client = SlowClient(delay=0.3)
        started = time.monotonic()
        concepts = stages.run_stage2(cycle, client=client)
        elapsed = time.monotonic() - started
        cycle.refresh_from_db()
        self.assertEqual(len(concepts), 3)
        self.assertEqual(cycle.status, Cycle.Status.CONCEPTS_PENDING)
        self.assertEqual(client.most_at_once, 3)
        # 1 plan + 3 details + 3 reviews. One after another that is 7 x 0.3 s;
        # in parallel it is about 3 x 0.3 s.
        stage2_steps = ("plan_concepts", "detail_concept", "review_concept")
        self.assertEqual(ModelCall.objects.filter(cycle=cycle, step__in=stage2_steps).count(), 7)
        self.assertLess(elapsed, 7 * 0.3)

    def test_a_failure_in_one_collection_saves_nothing(self):
        cycle = approved_cycle()
        with self.assertRaises(StageError):
            stages.run_stage2(cycle, client=SlowClient(delay=0.05, fail_step="review_concept"))
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.BRIEF_APPROVED)
        self.assertFalse(Concept.objects.filter(cycle=cycle).exists())


class ReviewEffortTests(TestCase):
    @override_settings(FDW_REVIEW_EFFORT="medium")
    def test_reviews_use_lighter_reasoning_and_design_steps_keep_the_default(self):
        cycle = approved_cycle()
        client = ScriptedClient()
        stages.run_stage2(cycle, client=client)
        efforts = {}
        for step, effort in client.efforts:
            efforts.setdefault(step, set()).add(effort)
        self.assertEqual(efforts["review_concept"], {"medium"})
        self.assertEqual(efforts["plan_concepts"], {""})
        self.assertEqual(efforts["detail_concept"], {""})


class ProgressTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user("founder", password="pw-for-tests-only")

    def test_progress_lists_only_calls_finished_after_the_page_loaded(self):
        from django.urls import reverse
        cycle = Cycle.objects.create(name="Progress")
        before = ModelCall.objects.create(cycle=cycle, step="plan_concepts", model="m", prompt="p")
        ModelCall.objects.create(cycle=cycle, step="detail_concept", model="m", prompt="p")
        ModelCall.objects.create(cycle=cycle, step="review_concept", model="m", prompt="p", ok=False)
        self.client.force_login(self.user)
        data = self.client.get(reverse("cycle_progress", args=[cycle.pk]), {"after": before.id}).json()
        self.assertEqual([c["step"] for c in data["calls"]], ["detail_concept", "review_concept"])
        self.assertEqual([c["ok"] for c in data["calls"]], [True, False])

    def test_cycle_page_tells_the_overlay_where_to_start(self):
        from django.urls import reverse
        cycle = Cycle.objects.create(name="Progress")
        call = ModelCall.objects.create(cycle=cycle, step="plan_concepts", model="m", prompt="p")
        self.client.force_login(self.user)
        page = self.client.get(reverse("cycle_detail", args=[cycle.pk]))
        self.assertContains(page, f'data-progress-after="{call.id}"')

    def test_progress_needs_a_signed_in_user(self):
        from django.urls import reverse
        cycle = Cycle.objects.create(name="Progress")
        response = self.client.get(reverse("cycle_progress", args=[cycle.pk]))
        self.assertEqual(response.status_code, 302)
