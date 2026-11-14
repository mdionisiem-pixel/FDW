"""Graded review, stalled retries, the front-shot check, and checking the reviewer on stage 4 images."""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import reviewer_check, schemas, visuals
from .llm import LLMResult
from .models import PieceVisuals, ReviewerResult, ShotAttempt
from .tests_visuals import MEDIA, FakeImages, ReviewClient, run_all, sourced_cycle


def review(mismatches=(), issues=(), verdict="fail"):
    results = [{"fact": "Shape", "observed": "as specified", "result": "match"}]
    for fact, severity in mismatches:
        results.append({"fact": fact, "observed": "off", "result": "mismatch", "severity": severity})
    return {"checklist_results": results, "realism_issues": list(issues), "verdict": verdict, "correction_note": ""}


class GradingTests(TestCase):
    def test_up_to_two_minor_problems_pass(self):
        data = schemas.validate_image_review(
            review([("Detail: shade", "minor"), ("Detail: collar height", "minor")]), max_minor=2)
        self.assertEqual(data["verdict"], "pass")
        self.assertTrue(data["verdict_overridden"])

    def test_three_minor_problems_fail(self):
        data = schemas.validate_image_review(
            review([("a", "minor"), ("b", "minor"), ("c", "minor")]), max_minor=2)
        self.assertEqual(data["verdict"], "fail")
        self.assertEqual((data["critical_count"], data["minor_count"]), (0, 3))

    def test_a_fact_marked_critical_is_critical_whatever_the_reviewer_said(self):
        data = schemas.validate_image_review(review([("Critical: one cream button", "minor")]), max_minor=2)
        self.assertEqual(data["verdict"], "fail")
        self.assertEqual(data["critical_count"], 1)

    def test_plain_text_realism_notes_count_as_minor(self):
        data = schemas.validate_image_review(review(issues=["slightly soft focus"], verdict="pass"), max_minor=2)
        self.assertEqual(data["verdict"], "pass")
        self.assertEqual(data["realism_issues"], ["Minor: slightly soft focus"])


class GradedClient(ReviewClient):
    """Answers with a sequence of (critical, minor) counts per shot."""

    def __init__(self, plan):
        super().__init__()
        self.plan = {k: list(v) for k, v in plan.items()}

    def complete(self, step, prompt, model, max_tokens, web_search_max=0, images=None, effort=""):
        if not step.startswith(("visual_review", "reviewer_check")):
            return super().complete(step, prompt, model, max_tokens, web_search_max, images, effort)
        shot = prompt.split("<shot_type>")[1][0]
        critical, minor = (self.plan.get(shot) or [(0, 0)]).pop(0) if self.plan.get(shot) else (0, 0)
        mismatches = [("Critical: c%d" % i, "critical") for i in range(critical)]
        mismatches += [("Detail: m%d" % i, "minor") for i in range(minor)]
        return LLMResult(json.dumps(review(mismatches, verdict="fail" if mismatches else "pass")), 2000, 800)


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=False, OPENROUTER_API_KEY="k", FDW_VISUALS_LIMIT_USD=Decimal("15"),
                   FDW_MAX_SHOT_ATTEMPTS=3, FDW_MAX_MINOR_MISMATCHES=2)
class FlowTests(TestCase):
    def setUp(self):
        self.cycle = sourced_cycle()
        visuals.start_stage4(self.cycle)
        sets = list(PieceVisuals.objects.filter(cycle=self.cycle))
        self.piece_set = sets[0]
        PieceVisuals.objects.filter(pk__in=[s.pk for s in sets[1:]]).update(status=PieceVisuals.Status.REJECTED)

    @override_settings(FDW_FRONT_SHOT_GATE=False)
    def test_an_improving_shot_gets_its_third_attempt(self):
        run_all(self.cycle, GradedClient({"B": [(2, 0), (1, 0), (0, 0)]}), FakeImages())
        self.assertEqual(self.piece_set.attempts.filter(shot="B").count(), 3)
        self.assertTrue(self.piece_set.attempts.filter(shot="B", attempt=3, status="passed").exists())

    @override_settings(FDW_FRONT_SHOT_GATE=False)
    def test_a_shot_that_gets_no_closer_stops_at_two(self):
        run_all(self.cycle, GradedClient({"C": [(1, 1), (1, 2)]}), FakeImages())
        self.assertEqual(self.piece_set.attempts.filter(shot="C").count(), 2)

    @override_settings(FDW_FRONT_SHOT_GATE=True)
    def test_the_piece_pauses_after_the_front_shot(self):
        images = FakeImages()
        run_all(self.cycle, GradedClient({}), images)
        self.piece_set.refresh_from_db()
        self.assertEqual(self.piece_set.status, PieceVisuals.Status.READY)
        self.assertFalse(self.piece_set.front_approved)
        self.assertEqual(len(images.calls), 1)  # only shot A was paid for
        self.assertTrue(visuals.piece_view(self.piece_set)["front_check"])
        visuals.decide(self.piece_set, "continue", user="founder")
        run_all(self.cycle, GradedClient({}), images)
        self.piece_set.refresh_from_db()
        self.assertEqual(self.piece_set.attempts.count(), 5)
        self.assertEqual(self.piece_set.status, PieceVisuals.Status.READY)
        self.assertFalse(visuals.piece_view(self.piece_set)["front_check"])

    @override_settings(FDW_FRONT_SHOT_GATE=True)
    def test_approving_at_the_front_check_keeps_only_the_front_shot(self):
        run_all(self.cycle, GradedClient({"A": [(1, 0), (1, 0)]}), FakeImages())
        self.piece_set.refresh_from_db()
        visuals.decide(self.piece_set, "approve", user="founder")
        self.piece_set.refresh_from_db()
        self.assertEqual(self.piece_set.status, PieceVisuals.Status.APPROVED)
        self.assertTrue(self.piece_set.attempts.filter(shot="A", accepted=True).exists())

    @override_settings(FDW_FRONT_SHOT_GATE=True)
    def test_requesting_changes_brings_the_front_check_back(self):
        run_all(self.cycle, GradedClient({}), FakeImages())
        self.piece_set.refresh_from_db()
        visuals.decide(self.piece_set, "continue")
        run_all(self.cycle, GradedClient({}), FakeImages())
        self.piece_set.refresh_from_db()
        visuals.decide(self.piece_set, "changes", "Darker brown.")
        self.piece_set.refresh_from_db()
        self.assertFalse(self.piece_set.front_approved)


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=False, OPENROUTER_API_KEY="k", FDW_VISUALS_LIMIT_USD=Decimal("15"),
                   FDW_BAKEOFF_LIMIT_USD=Decimal("10"), FDW_FRONT_SHOT_GATE=False, FDW_REVIEWER_MIN_REJECTED=1)
class Stage4CalibrationTests(TestCase):
    def setUp(self):
        self.cycle = sourced_cycle()
        visuals.start_stage4(self.cycle)
        sets = list(PieceVisuals.objects.filter(cycle=self.cycle))
        PieceVisuals.objects.filter(pk__in=[s.pk for s in sets[1:]]).update(status=PieceVisuals.Status.REJECTED)
        run_all(self.cycle, GradedClient({}), FakeImages())
        self.attempts = list(ShotAttempt.objects.filter(visuals__cycle=self.cycle).order_by("shot"))

    def test_marked_images_are_checked_with_the_stage_4_prompt_and_compared_with_you(self):
        self.attempts[0].founder_verdict = "accept"
        self.attempts[0].save()
        self.attempts[1].founder_verdict = "reject"
        self.attempts[1].save()
        run = reviewer_check.start_run()
        self.assertEqual(run.results.filter(attempt__isnull=False).count(), 2)
        client = GradedClient({"B": [(1, 0)]})  # the reviewer fails B, which you rejected
        while reviewer_check.review_next(run, client):
            pass
        info = reviewer_check.summary(run)
        self.assertEqual((info["caught"], info["rejected"]), (1, 1))
        self.assertEqual(info["state"], "ready")
        b = run.results.get(attempt=self.attempts[1])
        self.assertEqual(b.verdict, "fail")
        self.assertIn("shot B", b.label)

    def test_marking_screen_saves_and_clears_your_call(self):
        user = get_user_model().objects.create_user("owner", password="pw")
        self.client.force_login(user)
        page = self.client.get(reverse("bakeoff_reviewer"))
        self.assertContains(page, "Your calls on stage 4 images")
        url = reverse("bakeoff_mark_attempt", args=[self.attempts[0].pk])
        self.assertEqual(self.client.post(url, {"verdict": "reject"}).json()["reject"], 1)
        self.attempts[0].refresh_from_db()
        self.assertEqual(self.attempts[0].founder_verdict, "reject")
        self.client.post(url, {"verdict": ""})
        self.attempts[0].refresh_from_db()
        self.assertEqual(self.attempts[0].founder_verdict, "")
        self.assertEqual(self.client.post(url, {"verdict": "maybe"}).status_code, 400)
