"""Stage 2 design standard: the designer is briefed to it, and a creative shortfall is sent back."""
import json

from django.test import TestCase, override_settings

from . import prompts, stages
from .models import Concept
from .tests import ScriptedClient, approved_cycle


def weak_design_review(concept_id="C1"):
    return json.dumps({
        "concept_id": concept_id,
        "scores": {"demand_evidence": 4, "coherence": 4, "uniqueness": 4, "makeability": 4, "price_fit": 4,
                   "spec_completeness": 4, "desirability": 4, "range_variety": 2, "design_quality": 3},
        "reasons": {"range_variety": "The darned lattice sits at the same scale on all five pieces.",
                    "design_quality": "The roll-neck is a generic sweater with the motif added."},
        "blocking_issues": [], "risks": [], "overall": "recommend",
    })


@override_settings(FDW_DESIGN_MIN_SCORE=4)
class DesignStandardTests(TestCase):
    def test_every_design_step_is_briefed_with_the_standard(self):
        cycle = approved_cycle()
        seen = {}

        class Recording(ScriptedClient):
            def complete(self, step, prompt, *args, **kwargs):
                seen.setdefault(step, prompt)
                return super().complete(step, prompt, *args, **kwargs)

        stages.run_stage2(cycle, client=Recording())
        for step in ("plan_concepts", "detail_concept", "review_concept"):
            self.assertIn("DESIGN STANDARD", seen[step], step)
            self.assertIn("Never put the same motif at the same scale", seen[step], step)
        self.assertNotIn("Every piece must carry the signature detail", prompts.DETAIL_CONCEPT)

    def test_a_low_creative_score_sends_the_concept_back_once_with_the_reasons(self):
        cycle = approved_cycle()
        client = ScriptedClient({"review_concept": [weak_design_review()]})
        prompts_seen = []

        original = client.complete

        def spy(step, prompt, *args, **kwargs):
            if step == "revise_concept":
                prompts_seen.append(prompt)
            return original(step, prompt, *args, **kwargs)

        client.complete = spy
        concepts = stages.run_stage2(cycle, client=client)
        self.assertEqual(client.calls.count("revise_concept"), 1)
        self.assertIn("Design: range variety scored 2 of 5", prompts_seen[0])
        self.assertIn("same scale on all five pieces", prompts_seen[0])
        revised = [c for c in concepts if c.revision_count == 1]
        self.assertEqual(len(revised), 1)
        # Design shortfalls are not blocking: a revised concept still reaches the founder as proposed.
        self.assertEqual(revised[0].status, Concept.Status.PROPOSED)

    def test_a_recommend_with_a_low_creative_score_becomes_revise(self):
        review = stages.design_issues(json.loads(weak_design_review()))
        self.assertEqual(len(review), 2)

    def test_creative_scores_are_required_from_the_reviewer(self):
        from . import schemas

        data = json.loads(weak_design_review())
        del data["scores"]["design_quality"]
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_review(data)

    @override_settings(FDW_MODEL_DESIGNER="claude-opus-5-5")
    def test_the_designer_model_is_used_for_design_steps_only(self):
        cycle = approved_cycle()
        models = {}

        class Recording(ScriptedClient):
            def complete(self, step, prompt, model, *args, **kwargs):
                models.setdefault(step, model)
                return super().complete(step, prompt, model, *args, **kwargs)

        stages.run_stage2(cycle, client=Recording())
        self.assertEqual(models["plan_concepts"], "claude-opus-5-5")
        self.assertEqual(models["detail_concept"], "claude-opus-5-5")
        self.assertNotEqual(models["review_concept"], "claude-opus-5-5")
