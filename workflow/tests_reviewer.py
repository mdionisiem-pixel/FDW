"""Tests for the reviewer check: the stage 4 reviewer measured against the founder's own scores."""
import io
import json
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from . import bakeoff, reviewer_check, visuals
from .llm import LLMResult, StageError
from .models import BakeoffImage, BakeoffPiece, ReviewerResult

MEDIA = tempfile.mkdtemp(prefix="fdw-test-reviewer-")


def png():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 50), (196, 30, 42)).save(buffer, "PNG")
    return buffer.getvalue()


class VerdictClient:
    """Answers the reviewer with a fixed verdict per image code; records what it was shown."""

    dry_run = False

    def __init__(self, verdicts, fail_with=None):
        self.verdicts = verdicts
        self.fail_with = fail_with
        self.prompts, self.image_counts = [], []

    def complete(self, step, prompt, model, max_tokens, web_search_max=0, images=None, effort=""):
        if self.fail_with:
            raise StageError(self.fail_with)
        self.prompts.append(prompt)
        self.image_counts.append(len(images or []))
        code = prompt.split("CODE:")[1][:3].strip() if "CODE:" in prompt else ""
        verdict = self.verdicts.get(code, "pass")
        return LLMResult(json.dumps({
            "checklist_results": [{"fact": "Deep cherry red", "observed": "red",
                                   "result": "match" if verdict == "pass" else "mismatch", "severity": "critical"}],
            "realism_issues": [], "verdict": verdict,
            "correction_note": "" if verdict == "pass" else "The red must be deeper.",
        }), 3000, 900)


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=False, FDW_BAKEOFF_LIMIT_USD=Decimal("10"),
                   FDW_REVIEWER_ACCEPT_SCORE=4, FDW_REVIEWER_MIN_REJECTED=2,
                   FDW_REVIEWER_TARGET_CATCH=Decimal("0.9"))
class ReviewerCheckTests(TestCase):
    def setUp(self):
        self.piece = BakeoffPiece.objects.create(
            name="Knitted Hat", notes="CODE: n/a",
            spec={"description": "A red slouchy beanie.", "checklist": ["Deep cherry red", "Cream button"],
                  "prompts": {"A": "a", "C": "c", "D": "d"}},
        )
        self.piece.photo_front.save("front.png", ContentFile(png()))
        # (code, shot, model, four scores)
        rows = [
            ("AA", "A", "m1", (5, 5, 5, 5)),  # you accept
            ("BB", "C", "m1", (5, 5, 3, 5)),  # you reject: colour 3
            ("CC", "A", "m2", (2, 3, 2, 3)),  # you reject
            ("DD", "D", "m2", (4, 4, 4, 4)),  # you accept
        ]
        self.images = {}
        for code, shot, model, scores in rows:
            image = BakeoffImage(piece=self.piece, model=model, shot=shot, code=code,
                                 status=BakeoffImage.Status.DONE, prompt=f"CODE: {code}")
            image.matches_piece, image.material_realism, image.colour_accuracy, image.photo_realism = scores
            image.file.save(f"{code}.png", ContentFile(png()), save=False)
            image.save()
            self.images[code] = image
        # An unscored image is left out of the check.
        BakeoffImage.objects.create(piece=self.piece, model="m3", shot="A", code="EE",
                                    status=BakeoffImage.Status.DONE)

    def run_check(self, client):
        run = reviewer_check.start_run()
        # The piece notes reach the prompt, so they carry the image code for the stand-in to answer by.
        while True:
            pending = run.results.filter(status=ReviewerResult.Status.WAITING).order_by("id").first()
            if pending is None:
                break
            self.piece.notes = f"CODE: {pending.image.code}"
            self.piece.save(update_fields=["notes"])
            reviewer_check.review_next(run, client)
        return run

    def test_only_scored_images_are_checked_and_your_call_is_frozen(self):
        run = reviewer_check.start_run()
        self.assertEqual(run.results.count(), 4)
        calls = {r.image.code: r.founder_accepts for r in run.results.all()}
        self.assertEqual(calls, {"AA": True, "BB": False, "CC": False, "DD": True})
        self.assertEqual(run.version, visuals.reviewer_version())

    def test_catching_every_rejected_image_makes_the_reviewer_ready(self):
        client = VerdictClient({"BB": "fail", "CC": "fail"})
        run = self.run_check(client)
        info = reviewer_check.summary(run)
        self.assertEqual((info["caught"], info["rejected"]), (2, 2))
        self.assertEqual(info["state"], "ready")
        self.assertEqual(info["extra_fails"], [])

    def test_a_missed_flawed_image_means_not_ready(self):
        client = VerdictClient({"CC": "fail", "DD": "fail"})
        run = self.run_check(client)
        info = reviewer_check.summary(run)
        self.assertEqual(info["state"], "not_ready")
        self.assertEqual([r.image.code for r in info["missed"]], ["BB"])
        self.assertEqual([r.image.code for r in info["extra_fails"]], ["DD"])

    @override_settings(FDW_REVIEWER_MIN_REJECTED=15)
    def test_too_few_rejected_images_says_so(self):
        run = self.run_check(VerdictClient({"BB": "fail", "CC": "fail"}))
        self.assertEqual(reviewer_check.summary(run)["state"], "not_enough")

    def test_shots_c_and_d_are_shown_with_the_models_own_front_packshot(self):
        client = VerdictClient({})
        self.run_check(client)
        # AA (shot A): 1 image. BB (shot C, model m1): itself plus m1's A. CC (A): 1. DD (D, m2): itself plus m2's A.
        self.assertEqual(sorted(client.image_counts), [1, 1, 2, 2])
        self.assertTrue(any("next image is the approved reference image" in p for p in client.prompts))

    def test_uses_the_same_prompt_as_stage_4(self):
        client = VerdictClient({})
        self.run_check(client)
        self.assertTrue(all("Colour. In testing" in p for p in client.prompts))

    def test_cost_counts_toward_the_shared_tools_limit(self):
        before = bakeoff.total_spend()
        self.run_check(VerdictClient({}))
        self.assertGreater(bakeoff.total_spend(), before)
        self.assertEqual(bakeoff.total_spend() - before, reviewer_check.total_cost())

    @override_settings(FDW_BAKEOFF_LIMIT_USD=Decimal("0"))
    def test_stops_at_the_limit(self):
        with self.assertRaises(bakeoff.BakeoffError):
            reviewer_check.start_run()

    def test_a_model_error_keeps_the_image_waiting(self):
        run = reviewer_check.start_run()
        with self.assertRaises(bakeoff.BakeoffError):
            reviewer_check.review_next(run, VerdictClient({}, fail_with="overloaded"))
        self.assertEqual(run.results.filter(status=ReviewerResult.Status.WAITING).count(), 4)

    def test_a_changed_reviewer_marks_the_check_out_of_date(self):
        run = self.run_check(VerdictClient({"BB": "fail", "CC": "fail"}))
        run.version = "older"
        run.save()
        self.assertFalse(reviewer_check.summary(run)["current"])
        self.assertEqual(reviewer_check.status_line()["state"], "stale")


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=True, FDW_BAKEOFF_LIMIT_USD=Decimal("10"))
class ReviewerScreenTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("owner", password="pw")
        self.client.force_login(user)
        piece = BakeoffPiece.objects.create(
            name="Knitted Hat", spec={"description": "d", "checklist": ["f"], "prompts": {}})
        image = BakeoffImage(piece=piece, model="m1", shot="A", code="AA", status=BakeoffImage.Status.DONE,
                             matches_piece=2, material_realism=3, colour_accuracy=2, photo_realism=3)
        image.file.save("a.png", ContentFile(png()), save=False)
        image.save()

    def test_page_runs_the_check_and_shows_the_verdict(self):
        page = self.client.get(reverse("bakeoff_reviewer"))
        self.assertContains(page, "Start the check")
        self.client.post(reverse("bakeoff_reviewer_start"))
        body = self.client.post(reverse("bakeoff_reviewer_next")).json()
        self.assertTrue(body["finished"])
        # The activity feed line leads with whether the reviewer agrees with the founder.
        self.assertRegex(body["did"], r"^(Agrees|Disagrees) with you: Knitted Hat, front packshot, (passed|failed)\.$")
        page = self.client.get(reverse("bakeoff_reviewer"))
        self.assertContains(page, "Not enough evidence yet")
        self.assertContains(page, "Image by image")

    def test_menu_says_tools_and_both_tabs_are_there(self):
        page = self.client.get(reverse("bakeoff_home"))
        self.assertContains(page, ">Tools</a>")
        self.assertContains(page, "Reviewer check")
        self.assertContains(page, "Image model test")
