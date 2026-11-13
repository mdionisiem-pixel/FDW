"""Tests for stage 4: shot order, references, review and retries, the spend cap, and gate 4."""
import io
import json
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from . import checks, dryrun, schemas, stages, visuals
from .bakeoff import BakeoffError, SampleImages
from .llm import DryRunClient, LLMResult, SpendLimitError, StageError
from .models import Approval, Cycle, ModelCall, PieceVisuals, ShotAttempt, SourcingResult
from .tests_sourcing import selected_cycle

MEDIA = tempfile.mkdtemp(prefix="fdw-test-visuals-")


def png_bytes(colour=(200, 120, 140)):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 80), colour).save(buffer, "PNG")
    return buffer.getvalue()


class FakeImages:
    """A paid image model stand-in: returns a real PNG and a reported cost, or raises the queued errors."""

    dry_run = False

    def __init__(self, cost=Decimal("0.1358"), errors=()):
        self.cost = cost
        self.errors = list(errors)
        self.calls = []

    def generate(self, model, prompt, references):
        self.calls.append({"model": model, "prompt": prompt, "references": len(references)})
        if self.errors:
            raise BakeoffError(self.errors.pop(0))
        return png_bytes(), ("image/png", "png"), self.cost


class ReviewClient:
    """A model stand-in that accepts images. ``verdicts`` maps a shot to a list of pass/fail answers."""

    dry_run = False

    def __init__(self, verdicts=None):
        self.verdicts = {k: list(v) for k, v in (verdicts or {}).items()}
        self.images_seen = []
        self.prompts = []

    def complete(self, step, prompt, model, max_tokens, web_search_max=0, images=None, effort=""):
        self.prompts.append((step, prompt))
        if step.startswith("visual_review"):
            self.images_seen.append(len(images or []))
            shot = prompt.split("<shot_type>")[1][0]
            queue = self.verdicts.get(shot)
            verdict = queue.pop(0) if queue else "pass"
            text = json.dumps({
                "checklist_results": [{"fact": "Colour is Raspberry #B5305A", "observed": "as specified",
                                       "result": "match" if verdict == "pass" else "mismatch"}],
                "realism_issues": [],
                "verdict": verdict,
                "correction_note": "" if verdict == "pass" else f"Fix {shot}: the colour must be a deep raspberry.",
            })
        else:
            text = dryrun.respond(step, prompt)
        return LLMResult(text, 2000, 800)


def sourced_cycle(drop_first=False):
    """A cycle whose sourcing sheet was approved at gate 3."""
    cycle = selected_cycle()
    check = stages.run_stage3(cycle, client=DryRunClient())
    decisions = {}
    for index, result in enumerate(check.results.order_by("piece__piece_id")):
        if drop_first and index == 0:
            decisions[result.pk] = "dropped"
        elif result.verdict == SourcingResult.Verdict.NEEDS_SOURCING:
            decisions[result.pk] = "accept_risk"
        else:
            decisions[result.pk] = "approved"
    stages.decide_sourcing(check, decisions)
    cycle.refresh_from_db()
    return cycle


def run_all(cycle, client=None, images=None, limit=400):
    for _ in range(limit):
        result = visuals.work_next(cycle, client=client or DryRunClient(), image_client=images or SampleImages())
        if result["state"] != "worked":
            return result
    raise AssertionError("stage 4 did not finish")


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=True, FDW_VISUALS_LIMIT_USD=Decimal("15"), FDW_MAX_SHOT_ATTEMPTS=3)
class StartTests(TestCase):
    def test_refuses_before_gate_3(self):
        cycle = selected_cycle()
        with self.assertRaises(visuals.VisualsError):
            visuals.start_stage4(cycle)
        self.assertFalse(PieceVisuals.objects.exists())

    def test_creates_one_set_per_kept_piece_and_skips_dropped_pieces(self):
        cycle = sourced_cycle(drop_first=True)
        check = visuals.approved_sourcing(cycle)
        kept = [r for r in check.results.all() if r.continues]
        dropped = [r for r in check.results.all() if r.decision == "dropped"]
        self.assertTrue(dropped)
        count = visuals.start_stage4(cycle)
        cycle.refresh_from_db()
        self.assertEqual(count, len(kept))
        self.assertEqual(cycle.status, Cycle.Status.VISUALS_IN_PROGRESS)
        self.assertEqual(cycle.stage_number, 4)
        self.assertFalse(PieceVisuals.objects.filter(piece=dropped[0].piece).exists())
        for result in kept:
            piece_set = PieceVisuals.objects.get(piece=result.piece)
            self.assertEqual(piece_set.accepted_changes, result.changes)
            self.assertEqual(piece_set.waitlist_only, result.waitlist_only)

    @override_settings(FDW_DRY_RUN=False, OPENROUTER_API_KEY="")
    def test_refuses_without_an_image_key_and_changes_nothing(self):
        cycle = sourced_cycle()
        with self.assertRaises(StageError):
            visuals.start_stage4(cycle)
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.SOURCING_APPROVED)


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=True, FDW_VISUALS_LIMIT_USD=Decimal("15"), FDW_MAX_SHOT_ATTEMPTS=3)
class DryRunFlowTests(TestCase):
    def setUp(self):
        self.cycle = sourced_cycle()
        visuals.start_stage4(self.cycle)

    def test_every_piece_reaches_gate_4_with_five_shots(self):
        self.assertEqual(run_all(self.cycle)["state"], "finished")
        for piece_set in PieceVisuals.objects.filter(cycle=self.cycle):
            self.assertEqual(piece_set.status, PieceVisuals.Status.READY)
            view = visuals.piece_view(piece_set)
            self.assertEqual([s["state"] for s in view["shots"]], ["passed"] * 5)
            # The sample reviewer fails the first detail macro, so C needed a second attempt.
            self.assertEqual(piece_set.attempts.filter(shot="C").count(), 2)

    def test_references_follow_the_shot_order(self):
        run_all(self.cycle)
        piece_set = PieceVisuals.objects.filter(cycle=self.cycle).first()
        passed = {a.shot: a for a in piece_set.attempts.filter(status=ShotAttempt.Status.PASSED)}
        expected = {"A": [], "B": ["A"], "C": ["A"], "D": ["A", "B"], "E": ["A", "D"]}
        for shot, refs in expected.items():
            self.assertEqual(sorted(passed[r].pk for r in refs), sorted(passed[shot].references), shot)

    def test_retry_prompt_carries_the_correction_note(self):
        run_all(self.cycle)
        second = ShotAttempt.objects.filter(visuals__cycle=self.cycle, shot="C", attempt=2).first()
        self.assertIn("Corrections from earlier attempts", second.prompt)
        self.assertIn("readable one by one", second.prompt)

    def test_stage_4_calls_do_not_count_toward_the_research_budget(self):
        before = self.cycle.spend_usd
        ModelCall.objects.create(cycle=self.cycle, step="visual_review", model="m", prompt="p",
                                 cost_usd=Decimal("3"))
        self.assertEqual(self.cycle.spend_usd, before)
        self.assertEqual(self.cycle.visuals_spend_usd, Decimal("3"))


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=False, OPENROUTER_API_KEY="k",
                   FDW_VISUALS_LIMIT_USD=Decimal("15"), FDW_MAX_SHOT_ATTEMPTS=3,
                   FDW_IMAGE_MODEL="google/gemini-3-pro-image")
class PaidFlowTests(TestCase):
    def setUp(self):
        self.cycle = sourced_cycle()
        visuals.start_stage4(self.cycle)
        # Work on one piece only, to keep the counts simple.
        sets = list(PieceVisuals.objects.filter(cycle=self.cycle))
        self.piece_set = sets[0]
        PieceVisuals.objects.filter(pk__in=[s.pk for s in sets[1:]]).update(status=PieceVisuals.Status.REJECTED)

    def test_images_go_to_the_chosen_model_with_references_and_cost_is_recorded(self):
        images, client = FakeImages(), ReviewClient()
        run_all(self.cycle, client, images)
        self.assertEqual(len(images.calls), 5)
        self.assertEqual({c["model"] for c in images.calls}, {"google/gemini-3-pro-image"})
        self.assertEqual([c["references"] for c in images.calls], [0, 1, 1, 2, 2])
        # The reviewer sees the image itself plus each reference.
        self.assertEqual(client.images_seen, [1, 2, 2, 3, 3])
        spent = ShotAttempt.objects.filter(visuals=self.piece_set).values_list("cost_usd", flat=True)
        self.assertEqual(sum(spent), Decimal("0.1358") * 5)

    def test_a_failed_image_is_redrawn_and_stops_after_three_attempts(self):
        images, client = FakeImages(), ReviewClient({"B": ["fail", "fail", "fail"]})
        run_all(self.cycle, client, images)
        self.piece_set.refresh_from_db()
        b = list(self.piece_set.attempts.filter(shot="B"))
        self.assertEqual(len(b), 3)
        self.assertEqual(visuals.shot_state(b), "could_not_meet")
        self.assertEqual(self.piece_set.status, PieceVisuals.Status.READY)
        # D is drawn from A only, because B never passed.
        d = self.piece_set.attempts.get(shot="D", attempt=1)
        a = self.piece_set.attempts.get(shot="A", status=ShotAttempt.Status.PASSED)
        self.assertEqual(d.references, [a.pk])
        # The reviewer of the third attempt was told what failed before.
        third_review = [p for s, p in client.prompts if s == "visual_review" and "<shot_type>B" in p][-1]
        self.assertIn("Earlier attempts at this shot failed", third_review)

    def test_a_stuck_front_packshot_stops_the_piece_and_can_be_kept(self):
        images, client = FakeImages(), ReviewClient({"A": ["fail", "fail", "fail"]})
        run_all(self.cycle, client, images)
        self.piece_set.refresh_from_db()
        self.assertEqual(self.piece_set.status, PieceVisuals.Status.READY)
        self.assertEqual(self.piece_set.attempts.exclude(shot="A").count(), 0)
        with self.assertRaises(visuals.VisualsError):
            visuals.decide(self.piece_set, "approve")
        visuals.decide(self.piece_set, "continue", user="founder")
        run_all(self.cycle, client, images)
        self.piece_set.refresh_from_db()
        self.assertEqual(self.piece_set.status, PieceVisuals.Status.READY)
        kept = self.piece_set.attempts.get(shot="A", accepted=True)
        self.assertEqual(self.piece_set.attempts.get(shot="B").references, [kept.pk])

    def test_spend_cap_stops_before_the_image_and_keeps_it_waiting(self):
        with override_settings(FDW_VISUALS_LIMIT_USD=Decimal("0.30")):
            images, client = FakeImages(), ReviewClient()
            with self.assertRaises(SpendLimitError):
                run_all(self.cycle, client, images)
        # The cap set aside the next image before making it, so the total never passed the limit.
        self.assertLessEqual(self.cycle.visuals_spend_usd, Decimal("0.30"))
        self.assertFalse(ShotAttempt.objects.filter(status=ShotAttempt.Status.RUNNING).exists())
        self.assertTrue(ShotAttempt.objects.filter(status=ShotAttempt.Status.WAITING).exists())

    def test_account_errors_never_use_up_an_attempt(self):
        images = FakeImages(errors=["OpenRouter returned 402: insufficient credits"] * 3)
        client = ReviewClient()
        visuals.work_next(self.cycle, client=client, image_client=images)  # prompts
        for _ in range(3):
            with self.assertRaises(BakeoffError):
                visuals.work_next(self.cycle, client=client, image_client=images)
        a = self.piece_set.attempts.get(shot="A", attempt=1)
        self.assertEqual(a.status, ShotAttempt.Status.WAITING)
        self.assertEqual(a.error_count, 0)

    def test_a_prompt_the_model_refuses_twice_counts_as_a_failed_attempt(self):
        images = FakeImages(errors=["google/gemini-3-pro-image returned no image."] * 2)
        client = ReviewClient()
        for _ in range(3):  # prompts, then two refusals
            try:
                visuals.work_next(self.cycle, client=client, image_client=images)
            except BakeoffError:
                pass
        first = self.piece_set.attempts.get(shot="A", attempt=1)
        self.assertEqual(first.status, ShotAttempt.Status.FAILED)
        self.assertTrue(self.piece_set.attempts.filter(shot="A", attempt=2, status="waiting").exists())

    def test_a_review_failure_keeps_the_image_and_only_repeats_the_review(self):
        images, client = FakeImages(), ReviewClient()
        visuals.work_next(self.cycle, client=client, image_client=images)  # prompts
        with mock.patch.object(visuals, "review_attempt", side_effect=StageError("review down")):
            with self.assertRaises(StageError):
                visuals.work_next(self.cycle, client=client, image_client=images)
        a = self.piece_set.attempts.get(shot="A", attempt=1)
        self.assertTrue(a.file)
        self.assertEqual(a.status, ShotAttempt.Status.WAITING)
        visuals.work_next(self.cycle, client=client, image_client=images)
        self.assertEqual(len(images.calls), 1)  # not paid for twice

    def test_two_workers_never_claim_the_same_image(self):
        visuals.work_next(self.cycle, client=ReviewClient(), image_client=FakeImages())  # prompts; A is queued
        first = visuals._claim(self.cycle)
        self.assertEqual(first[0], "image")
        self.assertIsNone(visuals._claim(self.cycle))
        self.assertEqual(visuals.work_next(self.cycle, client=ReviewClient(), image_client=FakeImages())["state"],
                         "waiting")


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=True, FDW_VISUALS_LIMIT_USD=Decimal("15"), FDW_MAX_SHOT_ATTEMPTS=3)
class GateTests(TestCase):
    def setUp(self):
        self.cycle = sourced_cycle()
        visuals.start_stage4(self.cycle)
        run_all(self.cycle)
        self.sets = list(PieceVisuals.objects.filter(cycle=self.cycle))

    def test_changes_need_notes_and_start_a_new_round_that_reads_them(self):
        piece_set = self.sets[0]
        with self.assertRaises(visuals.VisualsError):
            visuals.decide(piece_set, "changes", "")
        visuals.decide(piece_set, "changes", "Make the motif 2 cm larger.", user="founder")
        piece_set.refresh_from_db()
        self.assertEqual(piece_set.round, 2)
        self.assertEqual(piece_set.status, PieceVisuals.Status.WRITING)
        run_all(self.cycle)
        prompt = ModelCall.objects.filter(cycle=self.cycle, step="visual_art_direction").first().prompt
        self.assertIn("Make the motif 2 cm larger.", prompt)
        self.assertEqual(piece_set.attempts.filter(round=1).count(), 6)  # round 1 is kept in the record

    def test_approving_every_piece_completes_the_stage(self):
        for piece_set in self.sets[:-1]:
            visuals.decide(piece_set, "approve", user="founder")
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, Cycle.Status.VISUALS_IN_PROGRESS)
        visuals.decide(self.sets[-1], "reject", user="founder")
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, Cycle.Status.VISUALS_APPROVED)
        self.assertEqual(Approval.objects.filter(cycle=self.cycle, stage=4).count(), len(self.sets) + 1)

    def test_a_decided_piece_cannot_be_decided_again(self):
        visuals.decide(self.sets[0], "approve")
        with self.assertRaises(visuals.VisualsError):
            visuals.decide(self.sets[0], "reject")

    def test_below_standard_shots_go_forward_only_when_kept(self):
        piece_set = self.sets[0]
        c_attempts = list(piece_set.attempts.filter(shot="C"))
        ShotAttempt.objects.filter(pk__in=[a.pk for a in c_attempts]).update(status=ShotAttempt.Status.FAILED)
        ShotAttempt.objects.create(visuals=piece_set, round=1, shot="C", attempt=3, prompt="p",
                                   status=ShotAttempt.Status.FAILED)
        visuals.decide(piece_set, "approve", keep=["C"], user="founder")
        self.assertTrue(piece_set.attempts.filter(shot="C", accepted=True).exists())
        self.assertIn("Kept below standard: shot C", Approval.objects.filter(stage=4).first().notes)


class ReviewSchemaTests(TestCase):
    def test_a_mismatch_fails_the_image_even_if_the_reviewer_passed_it(self):
        data = schemas.validate_image_review({
            "checklist_results": [{"fact": "Colour raspberry", "observed": "pink", "result": "mismatch"}],
            "realism_issues": [], "verdict": "pass", "correction_note": "",
        })
        self.assertEqual(data["verdict"], "fail")
        self.assertTrue(data["verdict_overridden"])
        self.assertIn("Colour raspberry", data["correction_note"])

    def test_a_fail_without_a_note_or_mismatch_is_sent_back(self):
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_image_review({
                "checklist_results": [{"fact": "f", "observed": "o", "result": "match"}],
                "realism_issues": ["melted stitches"], "verdict": "fail", "correction_note": "",
            })

    def test_art_direction_needs_all_five_full_prompts(self):
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_art_direction({"prompts": {"A": "x" * 300}, "reviewer_checklist": ["a"] * 6})


class StartupCheckTests(TestCase):
    def test_a_missing_required_library_is_an_error_with_the_fix(self):
        real = checks.importlib.util.find_spec
        with mock.patch.object(checks.importlib.util, "find_spec",
                               side_effect=lambda name: None if name == "PIL" else real(name)):
            problems = checks.libraries_installed(None)
        self.assertEqual([p.id for p in problems], ["workflow.E001"])
        self.assertIn("pip install -r requirements.txt", problems[0].hint)

    def test_nothing_is_reported_when_everything_is_installed(self):
        self.assertEqual(checks.libraries_installed(None), [])


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=True, FDW_VISUALS_LIMIT_USD=Decimal("15"))
class ScreenTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("owner", password="pw")
        self.client.force_login(self.user)
        self.cycle = sourced_cycle()

    def test_start_then_make_through_the_page_endpoints(self):
        page = self.client.get(reverse("cycle_detail", args=[self.cycle.pk]))
        self.assertContains(page, "Set up visual prototypes")
        self.client.post(reverse("start_stage4", args=[self.cycle.pk]))
        page = self.client.get(reverse("cycle_detail", args=[self.cycle.pk]))
        self.assertContains(page, "Make images")
        for _ in range(400):
            body = self.client.post(reverse("visuals_next", args=[self.cycle.pk])).json()
            if body["state"] != "worked":
                break
        self.assertEqual(body["state"], "finished")
        page = self.client.get(reverse("cycle_detail", args=[self.cycle.pk]))
        self.assertContains(page, "Gate 4:")
        self.assertContains(page, visuals.CONCEPT_LABEL)
        attempt = ShotAttempt.objects.filter(visuals__cycle=self.cycle).exclude(file="").first()
        image = self.client.get(reverse("visual_image", args=[self.cycle.pk, attempt.pk]))
        self.assertEqual(image.status_code, 200)

    def test_decision_form_records_gate_4(self):
        visuals.start_stage4(self.cycle)
        run_all(self.cycle)
        piece_set = PieceVisuals.objects.filter(cycle=self.cycle).first()
        self.client.post(reverse("decide_visuals", args=[self.cycle.pk, piece_set.pk]), {"decision": "approve"})
        piece_set.refresh_from_db()
        self.assertEqual(piece_set.status, PieceVisuals.Status.APPROVED)
        self.assertEqual(Approval.objects.get(stage=4).decided_by, "owner")


class SlowSampleImages(SampleImages):
    def generate(self, model, prompt, references):
        import time
        time.sleep(0.05)
        return super().generate(model, prompt, references)


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=True, FDW_VISUALS_LIMIT_USD=Decimal("15"), FDW_MAX_SHOT_ATTEMPTS=3)
class ParallelWorkerTests(TransactionTestCase):
    """Three page workers at once, as in the browser, against the real SQLite file."""

    def test_parallel_workers_finish_without_lock_errors_or_duplicates(self):
        from concurrent.futures import ThreadPoolExecutor

        from django.db import connection

        cycle = sourced_cycle()
        visuals.start_stage4(cycle)
        errors = []

        def worker():
            try:
                for _ in range(200):
                    result = visuals.work_next(Cycle.objects.get(pk=cycle.pk), client=DryRunClient(),
                                               image_client=SlowSampleImages())
                    if result["state"] == "finished":
                        return
                    if result["state"] == "waiting":
                        import time
                        time.sleep(0.05)
            except Exception as exc:  # noqa: BLE001  any error is the failure being tested for
                errors.append(repr(exc))
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=3) as pool:
            for future in [pool.submit(worker) for _ in range(3)]:
                future.result()
        self.assertEqual(errors, [])
        sets = PieceVisuals.objects.filter(cycle=cycle)
        self.assertTrue(all(s.status == PieceVisuals.Status.READY for s in sets))
        # Exactly one row per shot and attempt: five shots plus the one retry of C, per piece.
        self.assertEqual(ShotAttempt.objects.filter(visuals__cycle=cycle).count(), 6 * sets.count())
        self.assertEqual(ModelCall.objects.filter(cycle=cycle, step="visual_art_direction").count(), sets.count())
