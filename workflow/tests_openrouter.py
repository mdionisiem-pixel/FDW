"""OpenRouter's 402 answers: out of credit stops with a plain message; a full in-flight budget waits and retries."""
import io
import json
import tempfile
import urllib.error
from decimal import Decimal
from email.message import Message
from unittest import mock

from django.test import TestCase, override_settings

from . import bakeoff, visuals
from .models import PieceVisuals, ShotAttempt
from .tests_visuals import FakeImages, ReviewClient, sourced_cycle


def http_402(reason="", source="", retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    body = json.dumps({"error": {"code": 402, "message": "x",
                                 "metadata": {"reason": reason, "limit_source": source}}}).encode()
    return urllib.error.HTTPError("https://openrouter.ai/api/v1/images", 402, "Payment Required", headers, io.BytesIO(body))


@override_settings(OPENROUTER_API_KEY="k")
class PaymentErrorTests(TestCase):
    def raised(self, error):
        client = bakeoff.OpenRouterImages()
        with mock.patch("urllib.request.urlopen", side_effect=error):
            try:
                client._request("POST", "/images", {})
            except bakeoff.BakeoffError as exc:
                return exc
        raise AssertionError("no error raised")

    def test_full_in_flight_budget_is_a_wait_not_a_failure(self):
        exc = self.raised(http_402("in_flight_budget_exhausted", "openrouter_in_flight_budget", 7))
        self.assertIsInstance(exc, bakeoff.BudgetBusy)
        self.assertEqual(exc.retry_after, 7)
        self.assertIn("Nothing was charged", str(exc))

    def test_low_balance_says_to_add_credit(self):
        exc = self.raised(http_402("insufficient_credits", "openrouter_credits"))
        self.assertIsInstance(exc, bakeoff.OutOfCredit)
        self.assertIn("openrouter.ai/settings/credits", str(exc))

    def test_key_limit_points_to_the_key_settings(self):
        exc = self.raised(http_402("", "openrouter_key_limit"))
        self.assertIsInstance(exc, bakeoff.OutOfCredit)
        self.assertIn("settings/keys", str(exc))

    def test_image_request_waits_out_a_busy_budget_then_succeeds(self):
        client = bakeoff.OpenRouterImages()
        answers = [bakeoff.BudgetBusy("busy", retry_after=3), {"data": [{"b64_json": "x"}]}]

        def fake(method, path, payload=None, timeout=None):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch.object(client, "_request", side_effect=fake), \
                mock.patch.object(bakeoff, "_sleep") as sleep:
            self.assertEqual(client._send_image_request({}), {"data": [{"b64_json": "x"}]})
        sleep.assert_called_once_with(3)

    def test_image_request_gives_up_after_a_few_waits(self):
        client = bakeoff.OpenRouterImages()
        with mock.patch.object(client, "_request", side_effect=bakeoff.BudgetBusy("busy", 2)), \
                mock.patch.object(bakeoff, "_sleep") as sleep:
            with self.assertRaises(bakeoff.BudgetBusy):
                client._send_image_request({})
        self.assertEqual(sleep.call_count, bakeoff.BUDGET_RETRIES)


MEDIA = tempfile.mkdtemp(prefix="fdw-test-402-")


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=False, OPENROUTER_API_KEY="k",
                   FDW_VISUALS_LIMIT_USD=Decimal("15"), FDW_MAX_SHOT_ATTEMPTS=3)
class Stage4PaymentTests(TestCase):
    def setUp(self):
        self.cycle = sourced_cycle()
        visuals.start_stage4(self.cycle)
        sets = list(PieceVisuals.objects.filter(cycle=self.cycle))
        PieceVisuals.objects.filter(pk__in=[v.pk for v in sets[1:]]).update(status=PieceVisuals.Status.REJECTED)
        visuals.work_next(self.cycle, client=ReviewClient(), image_client=FakeImages())  # prompts for one piece

    def test_busy_budget_puts_the_image_back_and_the_worker_waits(self):
        images = FakeImages()
        images.errors = []
        with mock.patch.object(images, "generate", side_effect=bakeoff.BudgetBusy("busy")):
            result = visuals.work_next(self.cycle, client=ReviewClient(), image_client=images)
        self.assertEqual(result["state"], "waiting")
        self.assertIn("Nothing was charged", result["did"])
        attempt = ShotAttempt.objects.get(visuals__cycle=self.cycle, shot="A", attempt=1)
        self.assertEqual(attempt.status, ShotAttempt.Status.WAITING)
        self.assertEqual(attempt.error_count, 0)
        self.assertFalse(attempt.file)
        # When the budget frees up, the same image is made normally.
        result = visuals.work_next(self.cycle, client=ReviewClient(), image_client=FakeImages())
        self.assertEqual(result["state"], "worked")

    def test_out_of_credit_stops_without_using_an_attempt(self):
        images = FakeImages()
        with mock.patch.object(images, "generate", side_effect=bakeoff.OutOfCredit(
                "OpenRouter returned 402. Your OpenRouter balance cannot cover the next image.")):
            with self.assertRaises(bakeoff.OutOfCredit):
                visuals.work_next(self.cycle, client=ReviewClient(), image_client=images)
        attempt = ShotAttempt.objects.get(visuals__cycle=self.cycle, shot="A", attempt=1)
        self.assertEqual(attempt.status, ShotAttempt.Status.WAITING)
        self.assertEqual(attempt.error_count, 0)
        self.assertEqual(PieceVisuals.objects.filter(cycle=self.cycle, status="making").count(), 1)


@override_settings(OPENROUTER_API_KEY="k")
class TooLargeTests(TestCase):
    def test_weight_exceeds_budget_is_its_own_error(self):
        client = bakeoff.OpenRouterImages()
        with mock.patch("urllib.request.urlopen",
                        side_effect=http_402("weight_exceeds_budget", "openrouter_credits")):
            with self.assertRaises(bakeoff.TooLargeForBudget) as caught:
                client._request("POST", "/images", {})
        self.assertIn("Nothing was charged", str(caught.exception))


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=False, OPENROUTER_API_KEY="k",
                   FDW_VISUALS_LIMIT_USD=Decimal("15"), FDW_MAX_SHOT_ATTEMPTS=3,
                   FDW_REFERENCE_EDGE=1024, FDW_REFERENCE_FALLBACK_EDGE=640)
class ReferenceSizeTests(TestCase):
    def setUp(self):
        self.cycle = sourced_cycle()
        visuals.start_stage4(self.cycle)
        sets = list(PieceVisuals.objects.filter(cycle=self.cycle))
        PieceVisuals.objects.filter(pk__in=[v.pk for v in sets[1:]]).update(status=PieceVisuals.Status.REJECTED)
        visuals.work_next(self.cycle, client=ReviewClient(), image_client=FakeImages())  # prompts
        visuals.work_next(self.cycle, client=ReviewClient(), image_client=FakeImages())  # shot A, passes

    def test_references_are_small_jpegs(self):
        seen = []
        images = FakeImages()
        original = images.generate

        def spy(model, prompt, references):
            seen.extend(references)
            return original(model, prompt, references)

        images.generate = spy
        visuals.work_next(self.cycle, client=ReviewClient(), image_client=images)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("data:image/jpeg;base64,"))

    def test_too_large_is_retried_once_with_smaller_references(self):
        calls = []
        images = FakeImages()
        original = images.generate

        def first_too_large(model, prompt, references):
            calls.append(len(references[0]))
            if len(calls) == 1:
                raise bakeoff.TooLargeForBudget("too large")
            return original(model, prompt, references)

        images.generate = first_too_large
        result = visuals.work_next(self.cycle, client=ReviewClient(), image_client=images)
        self.assertEqual(result["state"], "worked")
        self.assertEqual(len(calls), 2)
        self.assertLessEqual(calls[1], calls[0])

    def test_still_too_large_stops_without_using_an_attempt(self):
        images = FakeImages()
        with mock.patch.object(images, "generate", side_effect=bakeoff.TooLargeForBudget("too large")):
            with self.assertRaises(bakeoff.TooLargeForBudget):
                visuals.work_next(self.cycle, client=ReviewClient(), image_client=images)
        b = ShotAttempt.objects.get(visuals__cycle=self.cycle, shot="B", attempt=1)
        self.assertEqual((b.status, b.error_count), (ShotAttempt.Status.WAITING, 0))
