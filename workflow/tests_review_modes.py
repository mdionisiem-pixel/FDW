"""Review modes: a real piece is judged against itself, a concept against its intent.

The reviewer check showed the reviewer answering a different question from the
founder: strict on a concept's centimetre targets, lenient on a real piece's
exact red. These tests hold the two modes apart and check that an approved
concept image's departures from the specification are carried onto the piece.
"""
from decimal import Decimal

from django.test import TestCase, override_settings

from . import prompts, schemas, tests_reviewer, visuals
from .models import ModelCall, PieceVisuals
from .tests_visuals import MEDIA, run_all, sourced_cycle


def review_with(results):
    return {"checklist_results": results, "realism_issues": [], "verdict": "pass", "correction_note": ""}


class SpecUpdateGradingTests(TestCase):
    def test_a_spec_update_is_recorded_and_never_fails_the_image(self):
        data = schemas.validate_image_review(review_with([
            {"fact": "Lattice band about 5 cm tall", "observed": "about 7 cm tall", "result": "spec_update"},
            {"fact": "Main colour Espresso", "observed": "a lighter chocolate brown", "result": "spec_update"},
            {"fact": "Lattice band about 5 cm tall", "observed": "as specified", "result": "match"},
        ]), max_minor=0)
        self.assertEqual(data["verdict"], "pass")
        self.assertEqual(data["minor_count"], 0)
        self.assertEqual(data["spec_updates"], [
            "Lattice band about 5 cm tall: the image shows about 7 cm tall",
            "Main colour Espresso: the image shows a lighter chocolate brown",
        ])

    def test_a_fact_marked_critical_cannot_be_waved_through_as_a_spec_update(self):
        data = schemas.validate_image_review(review_with([
            {"fact": "Critical: two cream buttons", "observed": "three buttons", "result": "spec_update"},
        ]), max_minor=2)
        self.assertEqual(data["verdict"], "fail")
        self.assertEqual(data["critical_count"], 1)
        self.assertEqual(data["spec_updates"], [])


class ReviewPromptTests(TestCase):
    def prompt(self, mode):
        return visuals.review_prompt({"name": "Hat"}, [], ["Deep cherry red"], "A", [], mode=mode)

    def test_concept_mode_treats_targets_as_targets(self):
        text = self.prompt("concept")
        self.assertIn("REVIEW MODE: CONCEPT", text)
        self.assertIn("spec_update", text)
        self.assertNotIn("REVIEW MODE: REAL PIECE", text)

    def test_real_mode_makes_a_shade_shift_critical(self):
        text = self.prompt("real")
        self.assertIn("REVIEW MODE: REAL PIECE", text)
        self.assertIn("crimson, brick or wine is critical", text)
        self.assertNotIn("REVIEW MODE: CONCEPT", text)

    def test_stage_4_defaults_to_concept(self):
        self.assertIn("REVIEW MODE: CONCEPT", visuals.review_prompt({}, [], [], "A", []))

    def test_changing_either_mode_changes_the_reviewer_version(self):
        before = visuals.reviewer_version()
        if True:
            original = prompts.REVIEW_MODE_REAL
            try:
                prompts.REVIEW_MODE_REAL = original + " More."
                self.assertNotEqual(visuals.reviewer_version(), before)
            finally:
                prompts.REVIEW_MODE_REAL = original


class ReviewerCheckModeTests(tests_reviewer.ReviewerCheckTests):
    """The image test photographs a real piece, so the check judges it in real mode."""

    def test_image_test_images_are_reviewed_as_a_real_piece(self):
        client = tests_reviewer.VerdictClient({})
        self.run_check(client)
        self.assertTrue(client.prompts)
        for prompt in client.prompts:
            self.assertIn("REVIEW MODE: REAL PIECE", prompt)


# Keep the inherited tests from running twice under this class.
for _name in [n for n in dir(tests_reviewer.ReviewerCheckTests) if n.startswith("test_")]:
    setattr(ReviewerCheckModeTests, _name, None)


@override_settings(MEDIA_ROOT=MEDIA, FDW_DRY_RUN=True, FDW_VISUALS_LIMIT_USD=Decimal("15"), FDW_MAX_SHOT_ATTEMPTS=3)
class BuildToImageTests(TestCase):
    def give_updates(self, piece_set, shot, *updates):
        for attempt in piece_set.attempts.filter(shot=shot):
            review = dict(attempt.review or {})
            review["spec_updates"] = list(updates)
            attempt.review = review
            attempt.save(update_fields=["review"])

    @override_settings(FDW_FRONT_SHOT_GATE=False)
    def test_approving_records_where_the_images_depart_from_the_specification(self):
        cycle = sourced_cycle()
        visuals.start_stage4(cycle)
        run_all(cycle)
        piece_set = PieceVisuals.objects.filter(cycle=cycle).first()
        self.give_updates(piece_set, "A", "Band about 5 cm tall: the image shows about 7 cm tall")
        self.give_updates(piece_set, "C", "Band about 5 cm tall: the image shows about 7 cm tall")
        visuals.decide(piece_set, "approve", user="founder")
        piece_set.refresh_from_db()
        self.assertEqual(piece_set.build_to_image, [
            "Shot A: Band about 5 cm tall: the image shows about 7 cm tall",
            "Shot C: Band about 5 cm tall: the image shows about 7 cm tall",
        ])

    @override_settings(FDW_FRONT_SHOT_GATE=True)
    def test_after_the_front_check_later_shots_are_reviewed_against_shot_a_as_approved(self):
        cycle = sourced_cycle()
        visuals.start_stage4(cycle)
        run_all(cycle)
        piece_set = PieceVisuals.objects.filter(cycle=cycle).first()
        self.give_updates(piece_set, "A", "Main colour Espresso: the image shows a lighter chocolate brown")
        visuals.decide(piece_set, "continue", user="founder")
        piece_set.refresh_from_db()
        self.assertEqual(piece_set.build_to_image,
                         ["Shot A: Main colour Espresso: the image shows a lighter chocolate brown"])
        run_all(cycle)
        later = ModelCall.objects.filter(cycle=cycle, step__startswith="visual_review",
                                         prompt__contains=f"{piece_set.piece.piece_id}").exclude(
            prompt__contains="<shot_type>A")
        self.assertTrue(later.exists())
        for call in later:
            self.assertIn("a lighter chocolate brown", call.prompt)

    @override_settings(FDW_FRONT_SHOT_GATE=True)
    def test_requesting_changes_clears_them(self):
        cycle = sourced_cycle()
        visuals.start_stage4(cycle)
        run_all(cycle)
        piece_set = PieceVisuals.objects.filter(cycle=cycle).first()
        self.give_updates(piece_set, "A", "Band: the image shows 7 cm")
        visuals.decide(piece_set, "continue", user="founder")
        run_all(cycle)
        piece_set.refresh_from_db()
        visuals.decide(piece_set, "changes", "Make the band narrower.", user="founder")
        piece_set.refresh_from_db()
        self.assertEqual(piece_set.build_to_image, [])
