"""Tests for the rules that must hold: gates, validation, retries, spend limit."""
import copy
import json
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import dryrun, prompts, schemas, stages
from .llm import AnthropicClient, DryRunClient, LLMResult, SpendLimitError, StageError, cost_of, parse_json
from .models import Approval, Concept, Cycle, DemandBrief, ModelCall, Piece, RawItem, Signal, TrendFinding


class ScriptedClient:
    """A stand-in model client. ``script`` maps a step to a list of responses."""

    dry_run = False

    def __init__(self, script=None, tokens=(1000, 500), source_urls=None, searches=3):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.tokens = tokens
        self.source_urls = dryrun.sample_source_urls() if source_urls is None else source_urls
        self.searches = searches
        self.calls = []
        self.search_calls = 0

    def complete(self, step, prompt, model, max_tokens, web_search_max=0):
        self.calls.append(step)
        queue = self.script.get(step)
        text = queue.pop(0) if queue else dryrun.respond(step, prompt)
        if web_search_max > 0:
            self.search_calls += 1
            return LLMResult(text, *self.tokens, web_searches=self.searches, source_urls=list(self.source_urls))
        return LLMResult(text, *self.tokens)


def make_cycle(items=("How much is the embroidered cardigan?", "Love this crochet top")):
    cycle = Cycle.objects.create(name="Test cycle")
    for text in items:
        RawItem.objects.create(cycle=cycle, source="dm", text=text)
    return cycle


def approved_cycle():
    cycle = make_cycle()
    brief = stages.run_stage1(cycle, client=DryRunClient())
    stages.decide_brief(brief, "approve", "Focus on knitwear", "founder")
    cycle.refresh_from_db()
    return cycle


class PromptTests(TestCase):
    def test_render_fills_slots_and_ignores_slots_inside_values(self):
        text = prompts.render("A {{raw_items}} B", raw_items="x {{brand_brief}} y")
        self.assertEqual(text, "A x {{brand_brief}} y B")

    def test_render_raises_on_missing_slot(self):
        with self.assertRaises(ValueError):
            prompts.render(prompts.DEMAND_BRIEF, brand_brief="b")


class ParsingTests(TestCase):
    def test_parse_json_handles_code_fence_and_prose(self):
        self.assertEqual(parse_json('Here you go:\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('[{"a": 1}]'), [{"a": 1}])

    def test_parse_json_rejects_text_without_json(self):
        with self.assertRaises(schemas.SchemaError):
            parse_json("no json here")

    def test_cost_arithmetic(self):
        # 1,000,000 input at $2 plus 100,000 output at $10 = $2 + $1
        self.assertEqual(cost_of("claude-sonnet-5-5", 1_000_000, 100_000), Decimal("3.0000"))

    def test_unknown_model_has_no_price(self):
        with self.assertRaises(StageError):
            cost_of("unknown-model", 1, 1)


class SchemaTests(TestCase):
    def setUp(self):
        self.data = json.loads(dryrun.respond("generate_concepts", ""))

    def test_sample_concepts_are_valid(self):
        schemas.validate_concepts(self.data)

    def test_two_concepts_are_rejected(self):
        self.data["concepts"].pop()
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_concepts(self.data)

    def test_missing_measurements_and_bad_hex_are_rejected(self):
        piece = self.data["concepts"][0]["pieces"][0]
        piece["dimensions_cm"] = {}
        piece["colours"][0]["hex"] = "red"
        with self.assertRaises(schemas.SchemaError) as ctx:
            schemas.validate_concepts(self.data)
        joined = " ".join(ctx.exception.problems)
        self.assertIn("dimensions_cm", joined)
        self.assertIn("hex", joined)

    def test_hand_technique_on_machine_route_is_a_blocking_issue(self):
        concept = self.data["concepts"][0]
        concept["pieces"][0]["production_route"] = "on_demand"
        issues = schemas.code_blocking_issues(concept)
        self.assertEqual(len(issues), 1)
        self.assertIn("on-demand", issues[0])

    def test_handmade_claim_outside_atelier_is_a_blocking_issue(self):
        concept = self.data["concepts"][2]
        concept["pieces"][0]["materials"] = "Hand-made feel wool blend"
        issues = schemas.code_blocking_issues(concept)
        self.assertTrue(any("handmade" in issue for issue in issues))


class Stage1Tests(TestCase):
    def test_stage1_runs_with_nothing_typed(self):
        cycle = Cycle.objects.create(name="Research only")
        brief = stages.run_stage1(cycle, client=DryRunClient())
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.BRIEF_PENDING)
        self.assertEqual(brief.status, DemandBrief.Status.PENDING)
        self.assertEqual(cycle.findings.filter(verified=True).count(), 4)
        self.assertEqual(cycle.signals.count(), 0)

    def test_stage1_writes_signals_and_pending_brief(self):
        cycle = make_cycle()
        brief = stages.run_stage1(cycle, client=DryRunClient())
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.BRIEF_PENDING)
        self.assertEqual(brief.status, DemandBrief.Status.PENDING)
        self.assertEqual(Signal.objects.filter(cycle=cycle).count(), 2)
        self.assertEqual(Signal.objects.filter(cycle=cycle, intent="purchase").count(), 1)

    def test_stage1_cannot_run_twice_without_a_decision(self):
        cycle = make_cycle()
        stages.run_stage1(cycle, client=DryRunClient())
        cycle.refresh_from_db()
        with self.assertRaises(stages.GateError):
            stages.run_stage1(cycle, client=DryRunClient())

    def test_invalid_json_is_retried_once_then_succeeds(self):
        cycle = make_cycle()
        client = ScriptedClient({"extract_signals": ["not json at all"]})
        stages.run_stage1(cycle, client=client)
        self.assertEqual(client.calls[:2], ["extract_signals", "extract_signals_retry"])

    def test_invalid_json_twice_stops_and_saves_nothing(self):
        cycle = make_cycle()
        client = ScriptedClient({"extract_signals": ["bad"], "extract_signals_retry": ["still bad"]})
        with self.assertRaises(StageError):
            stages.run_stage1(cycle, client=client)
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.NEW)
        self.assertEqual(cycle.signals.count(), 0)
        self.assertEqual(cycle.briefs.count(), 0)
        self.assertEqual(ModelCall.objects.filter(cycle=cycle).count(), 2)

    def test_rejecting_needs_a_reason_and_allows_a_rerun(self):
        cycle = make_cycle()
        brief = stages.run_stage1(cycle, client=DryRunClient())
        with self.assertRaises(StageError):
            stages.decide_brief(brief, "reject", "")
        stages.decide_brief(brief, "reject", "Too thin", "founder")
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.BRIEF_REJECTED)
        stages.run_stage1(cycle, client=DryRunClient())
        self.assertEqual(cycle.briefs.count(), 2)

    def test_a_brief_cannot_be_decided_twice(self):
        cycle = make_cycle()
        brief = stages.run_stage1(cycle, client=DryRunClient())
        stages.decide_brief(brief, "approve")
        with self.assertRaises(stages.GateError):
            stages.decide_brief(brief, "reject", "Changed my mind")


class ResearchTests(TestCase):
    def _findings(self, extra_url):
        data = json.loads(dryrun.respond("research_trends", ""))
        data["findings"].append({
            "theme": "Invented theme", "what_people_want": "Something nobody reported.",
            "evidence": "No real source.", "source_title": "Made up", "source_url": extra_url,
            "source_date": None, "relevance": "",
        })
        return json.dumps(data)

    def test_finding_with_unconfirmed_source_is_flagged_and_kept_out_of_the_brief(self):
        cycle = Cycle.objects.create(name="Check sources")
        fake_url = "https://not-in-results.example.org/report"
        client = ScriptedClient({"research_trends": [self._findings(fake_url)]})
        stages.run_stage1(cycle, client=client)
        self.assertEqual(cycle.findings.filter(verified=True).count(), 4)
        dropped = cycle.findings.get(verified=False)
        self.assertEqual(dropped.source_url, fake_url)
        brief_call = ModelCall.objects.get(cycle=cycle, step="demand_brief")
        self.assertNotIn(fake_url, brief_call.prompt)
        self.assertNotIn("Invented theme", brief_call.prompt)

    def test_no_confirmed_findings_and_no_other_input_stops_the_stage(self):
        cycle = Cycle.objects.create(name="Nothing confirmed")
        client = ScriptedClient(source_urls=[])
        with self.assertRaises(StageError):
            stages.run_stage1(cycle, client=client)
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.NEW)
        self.assertEqual(TrendFinding.objects.count(), 0)

    def test_direction_alone_is_enough_and_reaches_the_prompts(self):
        cycle = Cycle.objects.create(name="Direction", direction="Bold winter accessories")
        client = ScriptedClient(source_urls=[])
        brief = stages.run_stage1(cycle, client=client)
        research_call = ModelCall.objects.get(cycle=cycle, step="research_trends")
        self.assertIn("Bold winter accessories", research_call.prompt)
        stages.decide_brief(brief, "approve")
        cycle.refresh_from_db()
        stages.run_stage2(cycle, client=ScriptedClient())
        generator_call = ModelCall.objects.get(cycle=cycle, step="generate_concepts")
        self.assertIn("Bold winter accessories", generator_call.prompt)

    def test_search_cost_is_recorded(self):
        cycle = Cycle.objects.create(name="Cost")
        stages.run_stage1(cycle, client=ScriptedClient(tokens=(1000, 500), searches=3))
        call = ModelCall.objects.get(cycle=cycle, step="research_trends")
        self.assertEqual(call.web_searches, 3)
        # 1,000 input at $2/M + 500 output at $10/M + 3 searches at $0.01
        self.assertEqual(call.cost_usd, Decimal("0.0370"))

    def test_malformed_research_is_reformatted_without_searching_again(self):
        cycle = Cycle.objects.create(name="Reformat")
        good = dryrun.respond("research_trends", "")
        client = ScriptedClient({"research_trends": ["Here are my findings: " + good[:-1]]})
        with self.assertRaises(StageError):
            # The truncated JSON cannot be repaired by the sample reformatter, so the stage stops.
            stages.run_stage1(cycle, client=client)
        self.assertEqual(client.search_calls, 1)
        self.assertEqual(client.calls, ["research_trends", "reformat_findings"])

        cycle2 = Cycle.objects.create(name="Reformat ok")
        client2 = ScriptedClient({"research_trends": ["not json"], "reformat_findings": [good]})
        stages.run_stage1(cycle2, client=client2)
        self.assertEqual(client2.search_calls, 1)
        self.assertEqual(cycle2.findings.filter(verified=True).count(), 4)

    def test_urls_are_compared_without_scheme_or_trailing_slash(self):
        self.assertEqual(
            schemas.normalise_url("https://www.Example.com/Report/"),
            schemas.normalise_url("http://example.com/Report"),
        )
        self.assertNotEqual(
            schemas.normalise_url("https://example.com/a"), schemas.normalise_url("https://example.com/b")
        )


class AnthropicClientTests(TestCase):
    """The SDK is replaced with a fake, so these check our handling, not the API."""

    def _message(self, blocks, stop_reason, searches=0):
        usage = SimpleNamespace(
            input_tokens=100, output_tokens=50,
            server_tool_use=SimpleNamespace(web_search_requests=searches),
        )
        return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage)

    def _client(self, responses):
        test = self
        calls = []

        class FakeStream:
            def __init__(self, message):
                self.message = message

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return self.message

        class FakeMessages:
            def stream(self, **kwargs):
                calls.append(kwargs)
                return FakeStream(responses.pop(0))

        client = AnthropicClient.__new__(AnthropicClient)
        client._anthropic = SimpleNamespace(APIError=RuntimeError)
        client._client = SimpleNamespace(messages=FakeMessages())
        return client, calls

    def test_paused_search_turn_is_continued_and_results_are_combined(self):
        result_block = SimpleNamespace(
            type="web_search_tool_result",
            content=[SimpleNamespace(url="https://example.com/a"), SimpleNamespace(url="https://example.com/b")],
        )
        first = self._message([SimpleNamespace(type="text", text="Searching. "), result_block], "pause_turn", 2)
        second = self._message([SimpleNamespace(type="text", text='{"findings": []}')], "end_turn", 1)
        client, calls = self._client([first, second])
        result = client.complete("research_trends", "prompt", "claude-sonnet-5-5", 1000, web_search_max=5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["tools"][0]["max_uses"], 5)
        self.assertEqual(calls[1]["messages"][-1]["role"], "assistant")
        self.assertEqual(result.web_searches, 3)
        self.assertEqual(result.source_urls, ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(result.input_tokens, 200)
        self.assertTrue(result.text.endswith('{"findings": []}'))

    def test_no_tools_are_sent_without_web_search(self):
        client, calls = self._client([self._message([SimpleNamespace(type="text", text="ok")], "end_turn")])
        client.complete("demand_brief", "prompt", "claude-sonnet-5-5", 1000)
        self.assertNotIn("tools", calls[0])

    def test_search_error_result_is_ignored_safely(self):
        error_block = SimpleNamespace(
            type="web_search_tool_result",
            content=SimpleNamespace(type="web_search_tool_result_error", error_code="unavailable"),
        )
        client, _ = self._client([self._message([error_block, SimpleNamespace(type="text", text="{}")], "end_turn")])
        result = client.complete("research_trends", "prompt", "claude-sonnet-5-5", 1000, web_search_max=5)
        self.assertEqual(result.source_urls, [])

    def test_cut_off_answer_raises(self):
        client, _ = self._client([self._message([SimpleNamespace(type="text", text="{")], "max_tokens")])
        with self.assertRaises(StageError):
            client.complete("generate_concepts", "prompt", "claude-sonnet-5-5", 10)


class Stage2Tests(TestCase):
    def test_stage2_refuses_without_an_approved_brief(self):
        cycle = make_cycle()
        with self.assertRaises(stages.GateError):
            stages.run_stage2(cycle, client=DryRunClient())
        stages.run_stage1(cycle, client=DryRunClient())
        cycle.refresh_from_db()
        with self.assertRaises(stages.GateError):
            stages.run_stage2(cycle, client=DryRunClient())
        self.assertEqual(Concept.objects.count(), 0)

    def test_stage2_refuses_after_a_rejected_brief(self):
        cycle = make_cycle()
        brief = stages.run_stage1(cycle, client=DryRunClient())
        stages.decide_brief(brief, "reject", "No")
        cycle.refresh_from_db()
        with self.assertRaises(stages.GateError):
            stages.run_stage2(cycle, client=DryRunClient())

    def test_stage2_creates_three_reviewed_concepts(self):
        cycle = approved_cycle()
        concepts = stages.run_stage2(cycle, client=DryRunClient())
        cycle.refresh_from_db()
        self.assertEqual(len(concepts), 3)
        self.assertEqual(cycle.status, Cycle.Status.CONCEPTS_PENDING)
        for concept in concepts:
            self.assertIsNotNone(concept.latest_review)
            self.assertEqual(concept.status, Concept.Status.PROPOSED)

    def test_den_notes_reach_the_generator_prompt(self):
        cycle = approved_cycle()
        stages.run_stage2(cycle, client=DryRunClient())
        call = ModelCall.objects.filter(cycle=cycle, step="generate_concepts").first()
        self.assertIn("Focus on knitwear", call.prompt)

    def _concepts_with_bad_route(self):
        data = json.loads(dryrun.respond("generate_concepts", ""))
        data["concepts"][0]["pieces"][0]["production_route"] = "on_demand"
        return data

    def test_code_rule_triggers_one_revision_that_fixes_it(self):
        cycle = approved_cycle()
        client = ScriptedClient({"generate_concepts": [json.dumps(self._concepts_with_bad_route())]})
        concepts = stages.run_stage2(cycle, client=client)
        first = concepts[0]
        self.assertEqual(client.calls.count("revise_concept"), 1)
        self.assertEqual(first.revision_count, 1)
        self.assertEqual(first.status, Concept.Status.PROPOSED)
        self.assertEqual(first.data["pieces"][0]["production_route"], "atelier")
        self.assertEqual(first.reviews.count(), 2)

    def test_issue_that_survives_revision_is_marked_unresolved(self):
        cycle = approved_cycle()
        bad = self._concepts_with_bad_route()
        still_bad = copy.deepcopy(bad["concepts"][0])
        client = ScriptedClient({
            "generate_concepts": [json.dumps(bad)],
            "revise_concept": [json.dumps(still_bad)],
        })
        concepts = stages.run_stage2(cycle, client=client)
        self.assertEqual(concepts[0].status, Concept.Status.UNRESOLVED)
        self.assertEqual(client.calls.count("revise_concept"), 1)  # never loops
        self.assertTrue(concepts[0].latest_review.data["blocking_issues"])
        self.assertNotEqual(concepts[0].latest_review.overall, "recommend")

    def test_selecting_one_concept_creates_its_pieces(self):
        cycle = approved_cycle()
        concepts = stages.run_stage2(cycle, client=DryRunClient())
        pieces = stages.select_concepts(cycle, [concepts[0].pk], "Best fit", "founder")
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.CONCEPTS_SELECTED)
        self.assertEqual(len(pieces), 5)
        self.assertEqual(Piece.objects.count(), 5)
        self.assertEqual(Concept.objects.filter(status=Concept.Status.NOT_SELECTED).count(), 2)
        self.assertEqual(Approval.objects.filter(cycle=cycle, stage=2).count(), 1)

    def test_selecting_none_or_three_is_refused(self):
        cycle = approved_cycle()
        concepts = stages.run_stage2(cycle, client=DryRunClient())
        with self.assertRaises(StageError):
            stages.select_concepts(cycle, [])
        with self.assertRaises(StageError):
            stages.select_concepts(cycle, [c.pk for c in concepts])
        self.assertEqual(Piece.objects.count(), 0)

    def test_concepts_from_another_cycle_cannot_be_selected(self):
        cycle = approved_cycle()
        stages.run_stage2(cycle, client=DryRunClient())
        other = approved_cycle()
        other_concepts = stages.run_stage2(other, client=DryRunClient())
        with self.assertRaises(StageError):
            stages.select_concepts(cycle, [other_concepts[0].pk])


class SpendTests(TestCase):
    @override_settings(FDW_CYCLE_LIMIT_USD=Decimal("0.01"))
    def test_calls_stop_at_the_spend_limit(self):
        cycle = make_cycle()
        # 1,000,000 input tokens on the extractor model costs $0.10, over the limit.
        client = ScriptedClient(tokens=(1_000_000, 0))
        with self.assertRaises(SpendLimitError):
            stages.run_stage1(cycle, client=client)
        self.assertEqual(client.calls, ["extract_signals"])  # the brief call never happened
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.NEW)

    def test_dry_run_records_zero_cost(self):
        cycle = make_cycle()
        stages.run_stage1(cycle, client=DryRunClient())
        self.assertEqual(cycle.spend_usd, Decimal("0"))
        self.assertTrue(all(call.dry_run for call in cycle.model_calls.all()))


@override_settings(FDW_DRY_RUN=True)
class ScreenTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("founder", password="a-long-test-password")

    def test_screens_require_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_full_flow_through_the_screens(self):
        self.client.force_login(self.user)
        self.client.post(reverse("create_cycle"), {"name": "Winter"})
        cycle = Cycle.objects.get()
        url = reverse("cycle_detail", args=[cycle.pk])

        self.client.post(reverse("add_items", args=[cycle.pk]), {
            "source": "dm",
            "items": "How much is the green hat?\n\nDo you sell the cardigan?\n",
        })
        self.assertEqual(cycle.raw_items.count(), 2)

        # Stage 2 cannot be forced before gate 1.
        self.client.post(reverse("run_stage2", args=[cycle.pk]))
        self.assertEqual(cycle.concepts.count(), 0)

        self.client.post(reverse("run_stage1", args=[cycle.pk]))
        self.assertContains(self.client.get(url), "Gate 1: your decision")

        self.client.post(reverse("decide_brief", args=[cycle.pk]), {"decision": "approve", "notes": "Hats first"})
        self.client.post(reverse("run_stage2", args=[cycle.pk]))
        page = self.client.get(url)
        self.assertContains(page, "Gate 2: your decision")
        self.assertContains(page, "Sample: Hedgerow")

        concept = cycle.concepts.first()
        self.client.post(reverse("select_concepts", args=[cycle.pk]), {"concepts": [concept.pk], "notes": "Go"})
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.CONCEPTS_SELECTED)
        self.assertContains(self.client.get(url), "Approved pieces")

    def test_items_are_locked_after_the_brief_is_written(self):
        self.client.force_login(self.user)
        cycle = make_cycle()
        stages.run_stage1(cycle, client=DryRunClient())
        self.client.post(reverse("add_items", args=[cycle.pk]), {"source": "dm", "items": "late message"})
        self.assertEqual(cycle.raw_items.count(), 2)

    def test_flow_starts_with_nothing_typed(self):
        self.client.force_login(self.user)
        cycle = Cycle.objects.create(name="No input")
        self.client.post(reverse("run_stage1", args=[cycle.pk]))
        page = self.client.get(reverse("cycle_detail", args=[cycle.pk]))
        self.assertContains(page, "What the research found")
        self.assertContains(page, "Gate 1: your decision")

    def test_direction_is_saved_then_locked(self):
        self.client.force_login(self.user)
        cycle = Cycle.objects.create(name="Direction")
        self.client.post(reverse("save_direction", args=[cycle.pk]), {"direction": "Hats and gloves"})
        cycle.refresh_from_db()
        self.assertEqual(cycle.direction, "Hats and gloves")
        stages.run_stage1(cycle, client=DryRunClient())
        self.client.post(reverse("save_direction", args=[cycle.pk]), {"direction": "Changed"})
        cycle.refresh_from_db()
        self.assertEqual(cycle.direction, "Hats and gloves")

    def test_stage_endpoints_reject_get(self):
        self.client.force_login(self.user)
        cycle = make_cycle()
        self.assertEqual(self.client.get(reverse("run_stage1", args=[cycle.pk])).status_code, 405)
