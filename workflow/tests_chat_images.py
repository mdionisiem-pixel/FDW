"""Gemini images go through chat completions with a token cap, so OpenRouter's estimate fits a small balance.

The budget simulation below follows OpenRouter's documented rule (the
estimate's output part comes from max_tokens, or a fixed cap without it) with
two assumptions OpenRouter does not publish: the fixed cap is the model's
32,768-token maximum, and the in-flight budget is half the balance. With those,
it reproduces what the founder saw on 14 November 2026 at a $5.55 balance:
every /images request refused with weight_exceeds_budget, shot A included.
"""
import base64
import io
import json
import urllib.error
from decimal import Decimal
from email.message import Message
from unittest import mock

from django.test import TestCase, override_settings

from . import bakeoff

OUTPUT_PRICE = Decimal("0.00012")  # google/gemini-3-pro-image output, OpenRouter endpoints API, 14 Nov 2026
MODEL_MAX_OUTPUT = 32768


def png_b64():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 10), (196, 30, 42)).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class FakeOpenRouter:
    """Stands in for urlopen: applies the in-flight budget rule, then answers like OpenRouter."""

    def __init__(self, balance, finish_reason="stop", images=True):
        self.budget = Decimal(str(balance)) / 2
        self.finish_reason = finish_reason
        self.images = images
        self.requests = []

    def __call__(self, request, timeout=None, context=None):
        body = json.loads(request.data)
        path = request.full_url.split("/api/v1", 1)[1]
        self.requests.append((path, body))
        estimate = (body.get("max_tokens") or MODEL_MAX_OUTPUT) * OUTPUT_PRICE
        if estimate > self.budget:
            error = {"error": {"code": 402, "message": "x", "metadata": {
                "reason": "weight_exceeds_budget", "limit_source": "openrouter_credits"}}}
            raise urllib.error.HTTPError(request.full_url, 402, "Payment Required", Message(),
                                         io.BytesIO(json.dumps(error).encode()))
        if path == "/images":
            answer = {"data": [{"b64_json": png_b64()}], "usage": {"cost": 0.137}}
        else:
            message = {"role": "assistant", "content": ""}
            if self.images:
                message["images"] = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + png_b64()}}]
            answer = {"choices": [{"message": message, "finish_reason": self.finish_reason}],
                      "usage": {"completion_tokens": 1120, "cost": 0.137}}
        response = mock.MagicMock()
        response.read.return_value = json.dumps(answer).encode()
        response.__enter__.return_value = response
        return response


@override_settings(OPENROUTER_API_KEY="k", FDW_IMAGE_MAX_TOKENS=4000, FDW_IMAGE_RESOLUTION="2K",
                   FDW_CHAT_IMAGE_PREFIXES=("google/",))
class ChatImageTests(TestCase):
    def generate(self, fake, model="google/gemini-3-pro-image", refs=()):
        with mock.patch("urllib.request.urlopen", side_effect=fake), mock.patch.object(bakeoff, "_sleep"):
            return bakeoff.OpenRouterImages().generate(model, "A deep espresso knit coat.", list(refs))

    def test_the_failure_is_reproduced_on_the_old_route(self):
        fake = FakeOpenRouter(balance="5.55")
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            with self.assertRaises(bakeoff.TooLargeForBudget):
                bakeoff.OpenRouterImages()._send_image_request({"model": "google/gemini-3-pro-image", "prompt": "p"})

    def test_the_same_balance_is_enough_on_the_new_route(self):
        fake = FakeOpenRouter(balance="5.55")
        image, kind, cost = self.generate(fake, refs=["data:image/jpeg;base64,AAAA"])
        self.assertEqual(kind[0], "image/png")
        self.assertEqual(cost, Decimal("0.1370"))
        path, body = fake.requests[0]
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(body["max_tokens"], 4000)
        self.assertEqual(body["modalities"], ["image", "text"])
        self.assertEqual(body["image_config"], {"aspect_ratio": "4:5", "image_size": "2K"})
        self.assertEqual(body["messages"][0]["content"][1]["image_url"]["url"], "data:image/jpeg;base64,AAAA")

    def test_it_still_fits_at_a_one_dollar_balance(self):
        self.generate(FakeOpenRouter(balance="1.00"))

    def test_other_models_keep_the_image_api(self):
        fake = FakeOpenRouter(balance="100")
        self.generate(fake, model="openai/gpt-image-2")
        self.assertEqual(fake.requests[0][0], "/images")

    def test_hitting_the_token_cap_says_how_to_fix_it(self):
        with self.assertRaises(bakeoff.BakeoffError) as caught:
            self.generate(FakeOpenRouter(balance="5", finish_reason="length", images=False))
        self.assertIn("FDW_IMAGE_MAX_TOKENS", str(caught.exception))

    def test_402_messages_name_openrouters_reason(self):
        with self.assertRaises(bakeoff.TooLargeForBudget) as caught:
            self.generate(FakeOpenRouter(balance="0.20"))
        self.assertIn("weight_exceeds_budget", str(caught.exception))
