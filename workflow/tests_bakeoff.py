"""Tests for the image model bake-off."""
import base64
import json
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from . import bakeoff
from .llm import DryRunClient, LLMResult
from .models import BakeoffImage, BakeoffPiece

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MEDIA = tempfile.mkdtemp(prefix="fdw-test-media-")


def upload(name="front.png", data=PNG):
    return SimpleUploadedFile(name, data, content_type="image/png")


class FakeImages:
    """Records calls; fails for models listed in ``fail``."""

    dry_run = False

    def __init__(self, fail=(), cost="0.0300", data=PNG):
        self.fail, self.cost, self.data = set(fail), cost, data
        self.calls = []

    def generate(self, model, prompt, references):
        self.calls.append((model, prompt, len(references)))
        if model in self.fail:
            raise bakeoff.BakeoffError(f"{model} is down")
        return self.data, ("image/png", "png"), Decimal(self.cost) if self.cost else None


class RecordingClient(DryRunClient):
    def __init__(self):
        self.images_seen = None

    def complete(self, step, prompt, model, max_tokens, web_search_max=0, images=None):
        self.images_seen = images
        return super().complete(step, prompt, model, max_tokens)


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=True, FDW_BAKEOFF_LIMIT_USD=Decimal("10"))
class BakeoffTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def piece(self, all_photos=True):
        p = BakeoffPiece(name="Rosehip cardigan")
        p.photo_front = bakeoff.check_photo(upload())
        if all_photos:
            p.photo_detail = bakeoff.check_photo(upload("detail.jpg", JPEG))
            p.photo_worn = bakeoff.check_photo(upload("worn.png"))
        p.save()
        return p

    def test_photos_are_checked_by_content(self):
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.check_photo(upload("fake.png", b"<html>not an image</html>"))
        big = SimpleUploadedFile("big.png", PNG + b"\x00" * (5 * 1024 * 1024), content_type="image/png")
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.check_photo(big)
        stored = bakeoff.check_photo(upload("anything.gif", JPEG))
        self.assertTrue(stored.name.endswith(".jpg"))

    def test_claude_sees_the_photos_and_writes_three_prompts(self):
        p = self.piece()
        client = RecordingClient()
        spec = bakeoff.describe_piece(p, client=client)
        self.assertEqual(len(client.images_seen), 3)
        self.assertEqual(client.images_seen[0][0], "image/png")
        self.assertEqual(set(spec["prompts"]), {"A", "C", "D"})

    def test_queue_makes_one_job_per_model_and_shot_with_blind_codes(self):
        p = self.piece()
        bakeoff.describe_piece(p, client=DryRunClient())
        added = bakeoff.queue_images(p, ["m/one", "m/two", "m/one"])
        self.assertEqual(added, 6)
        self.assertEqual(bakeoff.queue_images(p, ["m/one"]), 0)
        for shot in "ACD":
            codes = list(p.images.filter(shot=shot).values_list("code", flat=True))
            self.assertEqual(len(set(codes)), 2)
        self.assertNotIn("m/one", "".join(p.images.values_list("code", flat=True)))

    def test_shots_without_a_real_photo_are_left_out(self):
        p = self.piece(all_photos=False)
        bakeoff.describe_piece(p, client=DryRunClient())
        bakeoff.queue_images(p, ["m/one"])
        self.assertEqual(list(p.images.values_list("shot", flat=True)), ["A"])

    def test_front_shot_first_then_others_use_it_as_reference(self):
        p = self.piece()
        bakeoff.describe_piece(p, client=DryRunClient())
        bakeoff.queue_images(p, ["m/one"])
        client = FakeImages()
        while bakeoff.make_next_image(p, client=client):
            pass
        self.assertEqual([c[2] for c in client.calls], [0, 1, 1])
        self.assertEqual(p.images.filter(status="done").count(), 3)
        self.assertEqual(bakeoff.total_spend(), Decimal("0.0900"))

    def test_failed_front_shot_fails_the_shots_that_need_it(self):
        p = self.piece()
        bakeoff.describe_piece(p, client=DryRunClient())
        bakeoff.queue_images(p, ["m/bad", "m/good"])
        client = FakeImages(fail={"m/bad"})
        while bakeoff.make_next_image(p, client=client):
            pass
        bad = p.images.filter(model="m/bad")
        self.assertEqual(set(bad.values_list("status", flat=True)), {"failed"})
        self.assertEqual(p.images.filter(model="m/good", status="done").count(), 3)
        self.assertEqual(sum(1 for c in client.calls if c[0] == "m/bad"), 1)
        self.assertEqual(bakeoff.retry_failed(p), 3)

    @override_settings(FDW_BAKEOFF_LIMIT_USD=Decimal("0.05"))
    def test_generation_stops_at_the_spend_limit(self):
        p = self.piece()
        bakeoff.describe_piece(p, client=DryRunClient())
        bakeoff.queue_images(p, ["m/one"])
        client = FakeImages(cost="0.0300")
        bakeoff.make_next_image(p, client=client)
        bakeoff.make_next_image(p, client=client)
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.make_next_image(p, client=client)
        self.assertEqual(len(client.calls), 2)

    def test_scores_are_validated_and_results_rank_by_match(self):
        p = self.piece(all_photos=False)
        bakeoff.describe_piece(p, client=DryRunClient())
        bakeoff.queue_images(p, ["m/one", "m/two"])
        while bakeoff.make_next_image(p, client=FakeImages()):
            pass
        one, two = p.images.get(model="m/one"), p.images.get(model="m/two")
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.save_scores(one, {"matches_piece": "6", "material_realism": "3", "colour_accuracy": "3", "photo_realism": "3"})
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.save_scores(one, {"matches_piece": "3"})
        bakeoff.save_scores(one, {"matches_piece": "2", "material_realism": "5", "colour_accuracy": "5", "photo_realism": "5"})
        bakeoff.save_scores(two, {"matches_piece": "4", "material_realism": "2", "colour_accuracy": "2", "photo_realism": "2"})
        rows = bakeoff.results()
        self.assertEqual(rows[0]["model"], "m/two")
        self.assertTrue(rows[0].get("leader"))
        self.assertEqual(rows[1]["overall"], 4.25)
        self.assertEqual(rows[0]["cost_per_image"], Decimal("0.0300"))


@override_settings(OPENROUTER_API_KEY="test-key", FDW_IMAGE_RESOLUTION="2K")
class OpenRouterTests(TestCase):
    def client_with(self, responses):
        client = bakeoff.OpenRouterImages()
        sent = []

        def fake_request(method, path, payload=None, timeout=None):
            sent.append(dict(payload or {}))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        client._request = fake_request
        return client, sent

    def test_image_and_cost_are_read_from_the_response(self):
        body = {"data": [{"b64_json": base64.b64encode(PNG).decode()}], "usage": {"cost": 0.0412}}
        client, sent = self.client_with([body])
        data, kind, cost = client.generate("m/one", "prompt", ["data:image/png;base64,AAAA"])
        self.assertEqual(data, PNG)
        self.assertEqual(kind, ("image/png", "png"))
        self.assertEqual(cost, Decimal("0.0412"))
        self.assertEqual(sent[0]["input_references"][0]["image_url"]["url"], "data:image/png;base64,AAAA")

    def test_rejected_size_falls_back_to_provider_defaults(self):
        body = {"data": [{"b64_json": base64.b64encode(JPEG).decode()}]}
        client, sent = self.client_with([bakeoff.BakeoffError("OpenRouter returned 400: bad size"), body])
        _, kind, cost = client.generate("m/one", "prompt", [])
        self.assertIn("resolution", sent[0])
        self.assertNotIn("resolution", sent[1])
        self.assertIsNone(cost)
        self.assertEqual(kind[0], "image/jpeg")

    def test_non_image_data_is_refused(self):
        body = {"data": [{"b64_json": base64.b64encode(b"<svg onload=alert(1)>").decode()}]}
        client, _ = self.client_with([body])
        with self.assertRaises(bakeoff.BakeoffError):
            client.generate("m/one", "prompt", [])

    def test_requests_use_a_verified_certificate_bundle(self):
        import ssl
        import certifi

        client = bakeoff.OpenRouterImages()
        self.assertEqual(client.tls.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(client.tls.check_hostname)
        self.assertGreater(len(client.tls.get_ca_certs()), 0)
        self.assertTrue(certifi.where())

    def test_certificate_failure_gets_a_clear_message(self):
        import ssl
        import urllib.error
        from unittest import mock

        client = bakeoff.OpenRouterImages()
        error = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(bakeoff.BakeoffError) as ctx:
                client.list_models()
        self.assertIn("certifi", str(ctx.exception))

    @override_settings(OPENROUTER_API_KEY="")
    def test_missing_key_is_explained(self):
        with self.assertRaises(bakeoff.BakeoffError):
            bakeoff.OpenRouterImages()


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=True)
class BakeoffScreenTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("founder", password="a-long-test-password")

    def test_files_need_sign_in_and_cannot_run_scripts(self):
        p = BakeoffPiece(name="Hat")
        p.photo_front = bakeoff.check_photo(upload())
        p.save()
        url = reverse("bakeoff_photo", args=[p.pk, "photo_front"])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])
        self.assertEqual(self.client.get(reverse("bakeoff_photo", args=[p.pk, "notes"])).status_code, 404)

    def test_full_flow_through_the_screens(self):
        self.client.force_login(self.user)
        self.client.post(reverse("bakeoff_create"), {
            "name": "Rosehip cardigan", "photo_front": upload(), "photo_detail": upload("d.png"),
        })
        p = BakeoffPiece.objects.get()
        self.client.post(reverse("bakeoff_describe", args=[p.pk]))
        self.client.post(reverse("bakeoff_queue", args=[p.pk]), {"models": ["m/one", "m/two"]})
        self.assertEqual(p.images.count(), 4)
        for _ in range(10):
            body = self.client.post(reverse("bakeoff_make_next", args=[p.pk])).json()
            if body["finished"]:
                break
        self.assertEqual(p.images.filter(status="done").count(), 4)
        page = self.client.get(reverse("bakeoff_piece", args=[p.pk])).content.decode()
        self.assertNotIn("m/one", page.split("Make the images")[1].split("Image ")[1])
        image = p.images.first()
        response = self.client.post(
            reverse("bakeoff_score", args=[p.pk, image.pk]),
            {"matches_piece": 4, "material_realism": 3, "colour_accuracy": 5, "photo_realism": 4},
            HTTP_X_REQUESTED_WITH="fetch",
        )
        self.assertTrue(response.json()["ok"])
        results = self.client.get(reverse("bakeoff_results"))
        self.assertContains(results, "m/one")
        self.assertContains(results, "Not every image is scored yet")

    def test_front_photo_is_required(self):
        self.client.force_login(self.user)
        self.client.post(reverse("bakeoff_create"), {"name": "No photo"})
        self.assertEqual(BakeoffPiece.objects.count(), 0)
