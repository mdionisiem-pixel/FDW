"""Stage 4 preparation: web research on real products, brand-name guard, persona acceptance stories, image size."""
import io
import json
from decimal import Decimal

from django.test import TestCase, override_settings

from . import dryrun, schemas, visuals
from .llm import LLMResult
from .models import PieceVisuals, ShotAttempt
from .tests_visuals import MEDIA, FakeImages, ReviewClient, run_all, sourced_cycle


class Recorder(ReviewClient):
    """Records every step's prompt and search allowance; can script answers per step."""

    def __init__(self, script=None):
        super().__init__()
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.seen = []

    def complete(self, step, prompt, model, max_tokens, web_search_max=0, images=None, effort=""):
        self.seen.append((step, web_search_max, prompt))
        if self.script.get(step):
            return LLMResult(self.script[step].pop(0), 1000, 500,
                             source_urls=dryrun.sample_source_urls() if web_search_max else [])
        result = super().complete(step, prompt, model, max_tokens, web_search_max, images, effort)
        if web_search_max:
            result.source_urls = dryrun.sample_source_urls()
        return result

    def prompts_for(self, step):
        return [p for s, _, p in self.seen if s == step]


def art_direction(text_in_prompts=""):
    body = ("A long espresso hand-knit coat with an irregular hand-darned oxblood lattice on the front bands. "
            "Matte wool, heavy drape, raking light. " * 4)
    return json.dumps({"prompts": {s: body + text_in_prompts for s in "ABCDE"},
                       "assumptions": ["Four toggles, evenly spaced from collar to waist."]})


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=False, OPENROUTER_API_KEY="k", FDW_VISUALS_LIMIT_USD=Decimal("15"),
                   FDW_FRONT_SHOT_GATE=False, FDW_MAX_SHOT_ATTEMPTS=3, FDW_MIN_IMAGE_EDGE=2048,
                   FDW_VISUAL_RESEARCH_SEARCHES=5)
class PreparationTests(TestCase):
    def setUp(self):
        self.cycle = sourced_cycle()
        visuals.start_stage4(self.cycle)
        sets = list(PieceVisuals.objects.filter(cycle=self.cycle))
        self.piece_set = sets[0]
        PieceVisuals.objects.filter(pk__in=[s.pk for s in sets[1:]]).update(status=PieceVisuals.Status.REJECTED)

    def test_research_comes_first_with_web_search_and_feeds_the_art_director(self):
        client = Recorder()
        visuals.work_next(self.cycle, client=client, image_client=FakeImages())
        steps = [s for s, _, _ in client.seen]
        self.assertEqual(steps[:3], ["visual_research", "visual_art_direction", "visual_acceptance"])
        self.assertEqual(client.seen[0][1], 5)
        art = client.prompts_for("visual_art_direction")[0]
        self.assertIn("perfectly regular printed check", art)
        self.assertIn("names_to_avoid (never use any of these): Samplebrand", art)
        self.piece_set.refresh_from_db()
        self.assertTrue(self.piece_set.research["sources"][0]["verified"])

    def test_acceptance_stories_see_the_assumptions_but_not_the_prompts(self):
        client = Recorder({"visual_art_direction": [art_direction()]})
        visuals.work_next(self.cycle, client=client, image_client=FakeImages())
        acceptance = client.prompts_for("visual_acceptance")[0]
        self.assertIn("Four toggles, evenly spaced", acceptance)
        self.assertNotIn("raking light. A long espresso", acceptance)
        self.piece_set.refresh_from_db()
        self.assertTrue(self.piece_set.stories)
        self.assertTrue(all(c["shots"] for c in self.piece_set.criteria))

    def test_each_shot_is_reviewed_only_against_the_criteria_it_can_show(self):
        client = Recorder()
        run_all(self.cycle, client, FakeImages())
        review_b = [p for p in client.prompts_for("visual_review") if "<shot_type>B" in p][0]
        review_a = [p for p in client.prompts_for("visual_review") if "<shot_type>A" in p][0]
        self.assertIn("back construction is consistent", review_b)
        self.assertNotIn("back construction is consistent", review_a)
        self.assertIn("Critical: Sample criterion: the colour family", review_a)

    def test_a_brand_name_in_the_prompts_is_sent_back_then_stripped(self):
        client = Recorder({"visual_art_direction": [art_direction(" In the style of Samplebrand."),
                                                    art_direction(" Echoing Samplebrand.")],
                           "visual_art_direction_retry": [art_direction(" Like Samplebrand.")]})
        visuals.work_next(self.cycle, client=client, image_client=FakeImages())
        self.piece_set.refresh_from_db()
        for text in self.piece_set.prompts.values():
            self.assertNotIn("Samplebrand", text)
        self.assertTrue(any("Samplebrand" in a for a in self.piece_set.assumptions))
        retry = client.prompts_for("visual_art_direction_retry")
        self.assertTrue(retry and "never names or imitates" in retry[0])

    def test_an_image_below_2048_px_fails_in_code_without_a_review(self):
        from PIL import Image

        small = io.BytesIO()
        Image.new("RGB", (928, 1152), (60, 40, 30)).save(small, "PNG")

        class SmallFirst(FakeImages):
            def generate(self, model, prompt, references):
                self.calls.append(1)
                if len(self.calls) == 1:
                    return small.getvalue(), ("image/png", "png"), Decimal("0.14")
                return super().generate(model, prompt, references)

        client = Recorder()
        images = SmallFirst()
        visuals.work_next(self.cycle, client=client, image_client=images)  # preparation
        visuals.work_next(self.cycle, client=client, image_client=images)  # shot A, 1K
        first = ShotAttempt.objects.get(visuals=self.piece_set, shot="A", attempt=1)
        self.assertEqual(first.status, ShotAttempt.Status.FAILED)
        self.assertIn("928×1152", first.error)
        self.assertIsNone(first.review)
        self.assertEqual(client.prompts_for("visual_review"), [])
        self.assertTrue(ShotAttempt.objects.filter(visuals=self.piece_set, shot="A", attempt=2).exists())


class GuardTests(TestCase):
    def test_single_word_names_match_only_as_written(self):
        self.assertEqual(schemas.names_found("Next, add the cuff.", ["Next"]), ["Next"])
        self.assertEqual(schemas.names_found("the next stitch", ["Next"]), [])
        self.assertEqual(schemas.names_found("like Maison Example's coat", ["maison example"]), ["maison example"])

    def test_acceptance_must_cover_every_shot(self):
        data = {"stories": [{"persona": "Buyer", "story": "As a buyer...",
                             "criteria": [{"criterion": "Colour family right", "severity": "critical", "shots": ["A"]}]}]}
        with self.assertRaises(schemas.SchemaError) as caught:
            schemas.validate_acceptance(data)
        self.assertIn("B, C, D, E", str(caught.exception.problems))

    def test_research_without_sources_is_refused(self):
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_visual_research({"technique_traits": ["x"], "garment_traits": ["y"],
                                              "must_not_look_like": ["z"], "sources": []})
