"""Brand analyst interview: questions, draft, approval, and vision/mission/goals in every prompt."""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import analyst, bakeoff, prompts, stages
from .llm import DryRunClient
from .models import (AnalystCall, BrandBrief, BrandBriefVersion, BrandInterview, Cycle, InterviewTurn, OpenPointOptions,
                     ModelCall, RawItem)


def turn_json(question="What future do you want Malina to bring about?", area="vision", ready=False, **coverage):
    return json.dumps({
        "reflection": "", "question": question, "why": "It anchors every design decision.", "area": area,
        "examples": ["Clothes kept for decades", "Craft back in everyday wear"],
        "coverage": coverage, "ready_to_draft": ready,
    })


DRAFT = json.dumps({
    "vision": "Every wardrobe holds one piece made to be handed on.",
    "mission": "Malina designs embroidered and crochet pieces in small batches for people who buy fewer, better things.",
    "goals": [{"goal": "Sell out the winter pre-order", "measure": "40 pieces", "by_when": "March 2027", "horizon": "short"},
              {"goal": "Run a two-person atelier", "measure": "two makers", "by_when": "2030", "horizon": "long"}],
    "brief": "WHO BUYS\nWomen 30 to 50 who mend rather than replace.\n\nRULES\nNever claim handmade unless Atelier.",
    "changes": ["Added a customer section."],
    "to_confirm": ["Price band for knitwear (To confirm)"],
})


class ScriptedAnalyst:
    """Answers with the queued texts in order and records each call."""

    def __init__(self, *answers, cost=Decimal("0.0300")):
        self.answers = list(answers)
        self.calls = []
        self.cost = cost

    def complete(self, prompt, max_tokens, effort):
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens, "effort": effort})
        return self.answers.pop(0), self.cost


@override_settings(FDW_DRY_RUN=False, FDW_INTERVIEW_LIMIT_USD=Decimal("2"))
class InterviewFlowTests(TestCase):
    def setUp(self):
        brief = BrandBrief.current()
        brief.text = "BRAND BRIEF: MALINA\nRULES\n- Never claim a piece is handmade unless its tier is Atelier."
        brief.save()

    def test_the_first_question_builds_on_the_current_brief(self):
        client = ScriptedAnalyst(turn_json(vision=1))
        interview = analyst.start(client)
        turn = interview.turns.get()
        self.assertEqual((turn.role, turn.area), (InterviewTurn.Role.ANALYST, "vision"))
        self.assertEqual(turn.examples, ["Clothes kept for decades", "Craft back in everyday wear"])
        prompt = client.calls[0]["prompt"]
        self.assertIn("Never claim a piece is handmade unless its tier is Atelier.", prompt)
        self.assertIn("<questions_asked_so_far>0</questions_asked_so_far>", prompt)
        self.assertEqual(interview.coverage["vision"], 1)
        self.assertEqual(interview.cost_usd, Decimal("0.0300"))

    def test_starting_again_resumes_the_open_interview(self):
        first = analyst.start(ScriptedAnalyst(turn_json()))
        self.assertEqual(analyst.start(ScriptedAnalyst()), first)

    def test_each_answer_brings_the_next_question_with_the_conversation(self):
        interview = analyst.start(ScriptedAnalyst(turn_json()))
        client = ScriptedAnalyst(turn_json("Who is Malina for?", "mission", vision=2))
        turn = analyst.answer(interview, "A world where clothes are mended, not thrown away.", client)
        self.assertEqual(turn.text, "Who is Malina for?")
        prompt = client.calls[0]["prompt"]
        self.assertIn("Founder: A world where clothes are mended, not thrown away.", prompt)
        self.assertIn("<questions_asked_so_far>1</questions_asked_so_far>", prompt)
        self.assertEqual(client.calls[0]["effort"], "low")

    def test_empty_overlong_and_double_answers_are_refused_without_a_call(self):
        interview = analyst.start(ScriptedAnalyst(turn_json()))
        client = ScriptedAnalyst()
        for text in ("", "   ", "x" * (analyst.MAX_ANSWER_CHARS + 1)):
            with self.assertRaises(analyst.AnalystError):
                analyst.answer(interview, text, client)
        InterviewTurn.objects.create(interview=interview, role=InterviewTurn.Role.FOUNDER, text="first click")
        with self.assertRaisesMessage(analyst.AnalystError, "still working"):
            analyst.answer(interview, "second click", client)
        self.assertEqual(client.calls, [])

    def test_an_unusable_answer_is_retried_once_with_the_problems(self):
        interview = analyst.start(ScriptedAnalyst(turn_json()))
        client = ScriptedAnalyst('{"question": ""}', turn_json("Who buys?", "customer"))
        turn = analyst.answer(interview, "Skip", client)
        self.assertEqual(turn.text, "Who buys?")
        self.assertIn("Your previous answer could not be used", client.calls[1]["prompt"])
        interview.refresh_from_db()
        self.assertEqual(interview.cost_usd, Decimal("0.0900"))  # three calls in all, each counted

    def test_a_failed_question_keeps_the_answer_and_can_be_retried(self):
        interview = analyst.start(ScriptedAnalyst(turn_json()))

        class Broken:
            def complete(self, *args):
                raise bakeoff.OutOfCredit("OpenRouter returned 402 (payment_required).")

        with self.assertRaises(analyst.AnalystError):
            analyst.answer(interview, "My vision is slow fashion.", Broken())
        interview.refresh_from_db()
        self.assertEqual(interview.turns.last().text, "My vision is slow fashion.")
        self.assertIn("402", interview.error)
        turn = analyst.retry_question(interview, ScriptedAnalyst(turn_json("And the mission?", "mission")))
        self.assertEqual(turn.text, "And the mission?")

    def test_the_spend_limit_stops_the_next_call(self):
        interview = analyst.start(ScriptedAnalyst(turn_json(), cost=Decimal("2.10")))
        client = ScriptedAnalyst(turn_json())
        with self.assertRaisesMessage(analyst.AnalystError, "FDW_INTERVIEW_LIMIT_USD"):
            analyst.answer(interview, "An answer", client)
        self.assertEqual(client.calls, [])

    def test_draft_then_approve_replaces_the_brief_and_keeps_the_old_one(self):
        interview = analyst.start(ScriptedAnalyst(turn_json()))
        analyst.answer(interview, "Clothes handed on.", ScriptedAnalyst(turn_json("Next?", "mission")))
        client = ScriptedAnalyst(DRAFT)
        draft = analyst.draft(interview, client)
        self.assertEqual(client.calls[0]["effort"], "medium")
        self.assertIn("word for word", client.calls[0]["prompt"])
        self.assertIn("Founder: Clothes handed on.", client.calls[0]["prompt"])
        interview.refresh_from_db()
        self.assertEqual(interview.status, BrandInterview.Status.DRAFTED)
        self.assertEqual(draft["goals"][0]["measure"], "40 pieces")

        old_text = BrandBrief.current().text
        goals = [
            {"goal": "Sell out the winter pre-order", "measure": "40 pieces", "by_when": "March 2027", "horizon": "short"},
            {"goal": "Grow the waiting list", "measure": "", "by_when": "", "horizon": "short"},
            {"goal": "Run a two-person atelier", "measure": "two makers", "by_when": "2030", "horizon": "long"},
        ]
        analyst.approve(interview, draft["vision"], draft["mission"], goals, draft["brief"])
        brief = BrandBrief.current()
        self.assertEqual(brief.vision, "Every wardrobe holds one piece made to be handed on.")
        self.assertEqual(brief.goals, [
            {"goal": "Sell out the winter pre-order", "measure": "40 pieces", "by_when": "March 2027", "horizon": "short"},
            {"goal": "Grow the waiting list", "measure": "", "by_when": "", "horizon": "short"},
            {"goal": "Run a two-person atelier", "measure": "two makers", "by_when": "2030", "horizon": "long"},
        ])
        self.assertEqual([g["goal"] for g in brief.long_goals], ["Run a two-person atelier"])
        before, after = BrandBriefVersion.objects.order_by("id")
        self.assertEqual((before.source, before.text), (BrandBriefVersion.Source.BEFORE, old_text))
        self.assertEqual(after.source, BrandBriefVersion.Source.INTERVIEW)
        interview.refresh_from_db()
        self.assertEqual(interview.status, BrandInterview.Status.APPROVED)

    def test_a_draft_needs_at_least_one_answer(self):
        interview = analyst.start(ScriptedAnalyst(turn_json()))
        with self.assertRaisesMessage(analyst.AnalystError, "Answer at least one question"):
            analyst.draft(interview, ScriptedAnalyst(DRAFT))


class DryRunInterviewTests(TestCase):
    @override_settings(FDW_DRY_RUN=True)
    def test_the_dry_run_walks_every_area_and_drafts(self):
        interview = analyst.start()
        for _ in range(len(prompts.BRAND_AREAS) - 1):
            analyst.answer(interview, "A sample answer.")
        interview.refresh_from_db()
        self.assertTrue(interview.coverage.get("ready"))
        self.assertEqual({t.area for t in interview.turns.filter(role="analyst")}, set(prompts.BRAND_AREAS))
        analyst.draft(interview)
        interview.refresh_from_db()
        self.assertEqual(interview.status, BrandInterview.Status.DRAFTED)


class GoalFieldTests(TestCase):
    def form(self, **fields):
        from django.http import QueryDict
        data = QueryDict(mutable=True)
        for key, values in fields.items():
            data.setlist(key, values)
        return data

    def test_rows_become_goals_and_empty_rows_are_dropped(self):
        goals = analyst.goals_from_form(self.form(
            short_goal=["Sell 40 coats", "", "Grow the list"], short_measure=["40 pieces", "x", ""],
            short_by=["December (year not stated)", "", "2027"],
            long_goal=["Open a studio"], long_measure=["two makers; one shop"], long_by=["2030"]))
        self.assertEqual(goals, [
            {"goal": "Sell 40 coats", "measure": "40 pieces", "by_when": "December (year not stated)", "horizon": "short"},
            {"goal": "Grow the list", "measure": "", "by_when": "2027", "horizon": "short"},
            {"goal": "Open a studio", "measure": "two makers; one shop", "by_when": "2030", "horizon": "long"},
        ])


class GoalRepairTests(TestCase):
    """The goals saved from the first live interview, as they were stored."""

    def test_a_whole_line_left_in_the_goal_is_split_even_with_brackets_in_the_date(self):
        fixed = analyst.repair_goal({"goal": "Build the waiting list to 500 people (500-person waiting list; "
                                             "December (year not stated by founder))", "measure": "", "by_when": "",
                                     "horizon": "short"})
        self.assertEqual((fixed["goal"], fixed["measure"], fixed["by_when"]),
                         ("Build the waiting list to 500 people", "500-person waiting list",
                          "December (year not stated by founder)"))

    def test_a_measure_with_semicolons_moves_back_from_the_date(self):
        fixed = analyst.repair_goal({"goal": "Scale revenue while staying a two-person operation", "measure": "€250k revenue",
                                     "by_when": "founder plus one atelier assistant; direct online sales only; 2028",
                                     "horizon": "long"})
        self.assertEqual((fixed["measure"], fixed["by_when"]),
                         ("€250k revenue; founder plus one atelier assistant; direct online sales only", "2028"))
        self.assertEqual(fixed["horizon"], "long")

    def test_a_good_goal_is_left_alone(self):
        goal = {"goal": "Sell 40 coats", "measure": "40 pieces", "by_when": "March 2027", "horizon": "short"}
        self.assertEqual(analyst.repair_goal(goal), goal)

    def test_the_migration_repairs_the_brief(self):
        import importlib
        from django.apps import apps
        brief = BrandBrief.current()
        brief.goals = [{"goal": "Reach €50k revenue (€50k; December (year not stated))", "measure": "", "by_when": "",
                        "horizon": "short"}]
        brief.save()
        importlib.import_module("workflow.migrations.0015_repair_goals").repair(apps, None)
        brief.refresh_from_db()
        self.assertEqual(brief.goals[0]["by_when"], "December (year not stated)")


class PromptTextTests(TestCase):
    def test_vision_mission_and_goals_come_first_in_every_prompt(self):
        brief = BrandBrief.current()
        brief.vision, brief.mission = "A mended world.", "Small batches, proven demand."
        brief.goals = [{"goal": "Open a studio", "measure": "", "by_when": "2030", "horizon": "long"},
                       {"goal": "Sell 40 coats", "measure": "", "by_when": "March 2027", "horizon": "short"}]
        brief.save()
        text = brief.prompt_text
        self.assertTrue(text.startswith(
            "VISION\nA mended world.\n\nMISSION\nSmall batches, proven demand.\n\n"
            "SHORT-TERM GOALS (the next 12 months)\n- Sell 40 coats (March 2027)\n\n"
            "LONG-TERM GOALS (3 to 5 years)\n- Open a studio (2030)"))
        self.assertTrue(text.endswith(brief.text.strip()))

    def test_without_them_the_prompt_is_the_brief_alone(self):
        brief = BrandBrief.current()
        self.assertEqual(brief.prompt_text, brief.text.strip())

    def test_stage_1_research_receives_the_vision(self):
        brief = BrandBrief.current()
        brief.vision = "VISION-MARKER: every wardrobe holds one piece made to be handed on."
        brief.save()
        cycle = Cycle.objects.create(name="Test")
        RawItem.objects.create(cycle=cycle, source="dm", text="Do you make cardigans?")
        stages.run_stage1(cycle, client=DryRunClient())
        prompts_seen = ModelCall.objects.filter(cycle=cycle).values_list("prompt", flat=True)
        self.assertTrue(any("VISION-MARKER" in p for p in prompts_seen))

    def test_unconfirmed_lines_count_every_part(self):
        brief = BrandBrief.current()
        brief.vision = "A vision (To confirm)"
        brief.goals = [{"goal": "Sell 40 (To confirm)", "measure": "", "by_when": ""}]
        brief.text = "Brief"
        brief.save()
        self.assertEqual(brief.unconfirmed_lines, 2)


@override_settings(OPENROUTER_API_KEY="test-key", FDW_MODEL_ANALYST="moonshotai/kimi-k3")
class OpenRouterChatTests(TestCase):
    def test_request_shape_and_reported_cost(self):
        chat = analyst.OpenRouterChat()
        sent = []

        def fake(method, path, payload=None, timeout=None):
            sent.append((path, payload))
            return {"choices": [{"message": {"content": turn_json()}}], "usage": {"cost": 0.0123}}

        chat._request = fake
        reply = chat.complete("Prompt", 6000, "low")
        path, payload = sent[0]
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(payload["model"], "moonshotai/kimi-k3")
        self.assertEqual(payload["max_tokens"], 6000)
        self.assertEqual(payload["reasoning"], {"effort": "low", "exclude": True})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(reply.cost, Decimal("0.0123"))
        self.assertEqual(payload["provider"], {"data_collection": "deny", "zdr": True})

    def test_a_provider_that_rejects_json_mode_is_asked_again_without_it(self):
        chat = analyst.OpenRouterChat()
        sent = []

        def fake(method, path, payload=None, timeout=None):
            sent.append(dict(payload))
            if "response_format" in payload:
                raise bakeoff.BakeoffError("OpenRouter returned 400: response_format not supported")
            return {"choices": [{"message": {"content": turn_json()}}], "usage": {}}

        chat._request = fake
        reply = chat.complete("Prompt", 6000, "low")
        self.assertNotIn("response_format", sent[1])
        self.assertEqual(reply.cost, Decimal("0"))

    @override_settings(FDW_ANALYST_PRIVATE=False)
    def test_privacy_can_be_switched_off(self):
        chat = analyst.OpenRouterChat()
        sent = []
        chat._request = lambda m, p, payload=None, timeout=None: sent.append(payload) or {
            "choices": [{"message": {"content": "{}"}}]}
        chat.complete("Prompt", 100, "low")
        self.assertNotIn("provider", sent[0])

    def test_no_private_provider_says_what_to_do(self):
        chat = analyst.OpenRouterChat()

        def fake(method, path, payload=None, timeout=None):
            raise bakeoff.BakeoffError('OpenRouter returned 404: {"error": "No endpoints found matching your data policy"}')

        chat._request = fake
        with self.assertRaisesMessage(analyst.AnalystError, "FDW_ANALYST_PRIVATE=0"):
            chat.complete("Prompt", 100, "low")

    def test_reply_details_are_read(self):
        chat = analyst.OpenRouterChat()
        chat._request = lambda m, p, payload=None, timeout=None: {
            "provider": "Fireworks", "choices": [{"message": {"content": "{}"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 6000, "cost": "0.0942"}}
        reply = chat.complete("Prompt", 6000, "low")
        self.assertEqual((reply.provider, reply.finish_reason, reply.input_tokens, reply.output_tokens),
                         ("Fireworks", "length", 1200, 6000))

    @override_settings(OPENROUTER_API_KEY="", FDW_DRY_RUN=False)
    def test_no_key_says_what_to_do(self):
        with self.assertRaisesMessage(analyst.AnalystError, "OPENROUTER_API_KEY"):
            analyst.get_client()


@override_settings(FDW_DRY_RUN=True)
class PageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="pw"))

    def test_the_whole_journey_through_the_pages(self):
        page = self.client.get(reverse("brand_brief"))
        self.assertContains(page, "Start the interview")
        self.client.post(reverse("brand_interview_start"))
        interview = BrandInterview.objects.get()
        page = self.client.get(reverse("brand_interview"))
        self.assertContains(page, "Sample question 1")
        self.assertContains(page, "A different sample answer")
        response = self.client.post(reverse("brand_interview_answer", args=[interview.pk]), {"answer": "Mended clothes."})
        self.assertEqual(response.json()["area"], "mission")
        response = self.client.post(reverse("brand_interview_answer", args=[interview.pk]), {"answer": ""})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["saved"])
        self.client.post(reverse("brand_interview_draft", args=[interview.pk]))
        page = self.client.get(reverse("brand_interview"))
        self.assertContains(page, "Approve and use in every prompt")
        self.assertContains(page, 'value="Sell out the winter pre-order"')
        self.assertContains(page, 'value="Run Malina as a two-person atelier"')
        self.assertContains(page, "Line by line against the current brief")
        self.client.post(reverse("brand_interview_approve", args=[interview.pk]), {
            "vision": "Edited vision.", "mission": "Edited mission.", "short_goal": ["Goal one", ""],
            "short_measure": ["10", ""], "short_by": ["2027", ""], "long_goal": ["Goal two"],
            "long_measure": ["studio"], "long_by": ["2030"], "text": "Edited brief.",
        })
        brief = BrandBrief.current()
        self.assertEqual((brief.vision, brief.text), ("Edited vision.", "Edited brief."))
        page = self.client.get(reverse("brand_brief"))
        self.assertContains(page, "Edited vision.")
        self.assertContains(page, "Long-term goals")
        self.assertContains(page, "Goal two")
        self.assertContains(page, "Earlier versions")

    def test_editing_by_hand_saves_all_four_parts_and_can_be_restored(self):
        original = BrandBrief.current().text
        self.client.post(reverse("brand_brief"), {"vision": "V", "mission": "M", "short_goal": ["G"],
                                                   "short_measure": ["1"], "short_by": ["2027"], "long_goal": ["L"],
                                                   "long_measure": ["2"], "long_by": ["2030"], "text": "New text"})
        brief = BrandBrief.current()
        self.assertEqual((brief.vision, brief.mission, brief.text), ("V", "M", "New text"))
        self.assertEqual([g["horizon"] for g in brief.goals], ["short", "long"])
        before = BrandBriefVersion.objects.get(source=BrandBriefVersion.Source.BEFORE)
        self.client.post(reverse("brand_restore", args=[before.pk]))
        self.assertEqual(BrandBrief.current().text, original)

    def test_a_failed_question_after_a_saved_answer_tells_the_page_to_reload(self):
        self.client.post(reverse("brand_interview_start"))
        interview = BrandInterview.objects.get()
        with override_settings(FDW_INTERVIEW_LIMIT_USD=Decimal("0")):
            response = self.client.post(reverse("brand_interview_answer", args=[interview.pk]), {"answer": "Kept."})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["saved"])
        page = self.client.get(reverse("brand_interview"))
        self.assertContains(page, "Ask the next question")

    def test_ending_the_interview_changes_nothing(self):
        before = BrandBrief.current().prompt_text
        self.client.post(reverse("brand_interview_start"))
        interview = BrandInterview.objects.get()
        self.client.post(reverse("brand_interview_close", args=[interview.pk]))
        self.assertEqual(BrandBrief.current().prompt_text, before)
        self.assertContains(self.client.get(reverse("brand_brief")), "Start the interview")


@override_settings(FDW_DRY_RUN=False, FDW_INTERVIEW_LIMIT_USD=Decimal("2"), FDW_ANALYST_TURN_TOKENS=6000)
class CallRecordTests(TestCase):
    def test_every_call_is_recorded_with_prompt_reply_and_time(self):
        interview = analyst.start(ScriptedAnalyst(turn_json()))
        call = AnalystCall.objects.get()
        self.assertEqual((call.interview, call.kind, call.ok), (interview, "question", True))
        self.assertIn("<questions_asked_so_far>0</questions_asked_so_far>", call.prompt)
        self.assertIn("What future do you want", call.response)
        self.assertEqual(call.cost_usd, Decimal("0.0300"))
        self.assertGreaterEqual(call.seconds, 0)

    def test_a_failed_call_is_recorded_with_its_error(self):
        class Broken:
            def complete(self, *args):
                raise bakeoff.OutOfCredit("OpenRouter returned 402 (payment_required).")

        with self.assertRaises(analyst.AnalystError):
            analyst.start(Broken())
        call = AnalystCall.objects.get()
        self.assertFalse(call.ok)
        self.assertIn("402", call.error)

    def test_a_reply_cut_off_at_the_length_limit_is_asked_again_with_more_room(self):
        class CutOff:
            def __init__(self):
                self.tokens = []

            def complete(self, prompt, max_tokens, effort):
                self.tokens.append(max_tokens)
                if len(self.tokens) == 1:
                    return analyst.Reply("", Decimal("0.05"), finish_reason="length")
                return analyst.Reply(turn_json(), Decimal("0.03"), finish_reason="stop")

        client = CutOff()
        interview = analyst.start(client)
        self.assertEqual(client.tokens, [6000, 12000])
        self.assertEqual(list(AnalystCall.objects.order_by("id").values_list("kind", "finish_reason")),
                         [("question", "length"), ("question_retry", "stop")])
        self.assertEqual(interview.cost_usd, Decimal("0.08"))

    def test_cut_off_twice_says_which_setting_to_raise(self):
        client = ScriptedAnalyst(analyst.Reply("", finish_reason="length"), analyst.Reply("", finish_reason="length"))
        client.complete = lambda prompt, max_tokens, effort, answers=client.answers: answers.pop(0)
        with self.assertRaisesMessage(analyst.AnalystError, "FDW_ANALYST_TURN_TOKENS"):
            analyst.start(client)


class GoalHorizonDraftTests(TestCase):
    def test_a_draft_without_long_term_goals_puts_the_gap_in_front_of_the_founder(self):
        from . import schemas
        data = json.loads(DRAFT)
        data["goals"] = [g for g in data["goals"] if g["horizon"] == "short"]
        draft = schemas.validate_brand_draft(data)
        self.assertTrue(any("No long-term goals" in c for c in draft["to_confirm"]))

    def test_a_goal_without_a_horizon_is_sent_back(self):
        from . import schemas
        data = json.loads(DRAFT)
        del data["goals"][0]["horizon"]
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_brand_draft(data)

    def test_the_interview_covers_both_horizons(self):
        self.assertIn("short_goals", prompts.BRAND_AREAS)
        self.assertIn("long_goals", prompts.BRAND_AREAS)
        self.assertIn("Long-term goals: where the brand should stand in three to five years", prompts.BRAND_ANALYST)


@override_settings(FDW_DRY_RUN=False, FDW_INTERVIEW_LIMIT_USD=Decimal("2"))
class ReadyToDraftTests(TestCase):
    """In the first live interview the analyst said 'ready' with no question three
    times; the app forced it to invent one. Now 'ready' is a proper ending."""

    READY = json.dumps({"reflection": "Every area is covered.", "question": "", "why": "Enough to write the brief.",
                        "area": "", "examples": [], "coverage": {}, "ready_to_draft": True})

    def test_ready_with_no_question_is_accepted_without_a_retry(self):
        interview = analyst.start(ScriptedAnalyst(turn_json()))
        client = ScriptedAnalyst(self.READY)
        turn = analyst.answer(interview, "That's everything.", client)
        self.assertEqual((turn.text, turn.area, turn.examples), ("", "", []))
        self.assertEqual(len(client.calls), 1)
        interview.refresh_from_db()
        self.assertTrue(interview.coverage["ready"])
        self.assertIn("(said the picture is complete", analyst.conversation_text(interview))

    def test_no_question_without_ready_is_still_sent_back(self):
        from . import schemas
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_analyst_turn({"question": "", "ready_to_draft": False}, prompts.BRAND_AREAS)

    def test_the_founder_can_still_add_more_after_ready(self):
        interview = analyst.start(ScriptedAnalyst(turn_json()))
        analyst.answer(interview, "Done.", ScriptedAnalyst(self.READY))
        turn = analyst.answer(interview, "One more thing: no leather.", ScriptedAnalyst(turn_json("Why no leather?", "boundaries")))
        self.assertEqual(turn.text, "Why no leather?")

    def test_the_page_offers_the_draft_instead_of_an_empty_question(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="pw"))
        interview = analyst.start(ScriptedAnalyst(turn_json()))
        analyst.answer(interview, "Done.", ScriptedAnalyst(self.READY))
        page = self.client.get(reverse("brand_interview")).content.decode()
        self.assertIn("The analyst has what it needs to write your draft.", page)
        self.assertEqual(page.count("Write the draft now"), 1)


class DraftCheckTests(TestCase):
    def draft(self, **changes):
        base = {"vision": "Craft that scales.", "mission": "Malina makes pieces.",
                "goals": [{"goal": "Sell coats", "measure": "40 pieces", "by_when": "March 2027", "horizon": "short"}],
                "brief": "WHO BUYS\nWomen 25-35.", "changes": [], "to_confirm": []}
        base.update(changes)
        return base

    def test_every_protected_rule_in_the_current_brief_is_restored_and_flagged(self):
        from .models import DEFAULT_BRAND_BRIEF
        result = analyst.check_draft(self.draft(), DEFAULT_BRAND_BRIEF, "40 pieces by March 2027, women 25-35")
        for rule in analyst.PROTECTED_LINES:
            self.assertIn(rule, result["brief"])
        self.assertEqual(sum(1 for c in result["checks"] if c["kind"] == "restored"), 5)

    def test_rules_already_kept_are_not_flagged(self):
        from .models import DEFAULT_BRAND_BRIEF
        brief = "WHO BUYS\nWomen 25-35.\n\nRULES\n" + "\n".join(analyst.PROTECTED_LINES)
        result = analyst.check_draft(self.draft(brief=brief), DEFAULT_BRAND_BRIEF, "40 pieces by March 2027, women 25-35")
        self.assertEqual(result["checks"], [])
        self.assertEqual(result["brief"], brief)

    def test_a_rule_the_founder_wrote_under_hard_rules_is_protected_too(self):
        start = "Hard rules:\n- Never use leather.\n- Never use real fur."
        result = analyst.check_draft(self.draft(), start, "40 pieces by March 2027, women 25-35")
        self.assertIn("- Never use leather.", result["brief"])
        self.assertIn("- Never use real fur.", result["brief"])

    def test_figures_not_in_the_answers_or_brief_are_flagged(self):
        result = analyst.check_draft(self.draft(vision="Reach €250k by 2030."), "", "40 pieces by March 2027, women 25-35")
        flagged = [c["text"] for c in result["checks"] if c["kind"] == "number"]
        self.assertEqual(len(flagged), 1)
        self.assertIn("2030", flagged[0])
        self.assertIn("250000", flagged[0])
        self.assertNotIn("2027", flagged[0])

    def test_long_statements_are_flagged(self):
        result = analyst.check_draft(self.draft(vision=" ".join(["word"] * 30)), "", "40 pieces March 2027 25-35")
        self.assertTrue(any(c["kind"] == "length" and "30 words" in c["text"] for c in result["checks"]))

    @override_settings(FDW_DRY_RUN=False, FDW_INTERVIEW_LIMIT_USD=Decimal("2"))
    def test_the_draft_step_stores_the_checks(self):
        interview = analyst.start(ScriptedAnalyst(turn_json()))
        analyst.answer(interview, "Clothes handed on.", ScriptedAnalyst(turn_json("Next?", "mission")))
        result = analyst.draft(interview, ScriptedAnalyst(DRAFT))
        interview.refresh_from_db()
        self.assertEqual(interview.draft["checks"], result["checks"])
        self.assertTrue(any(c["kind"] == "number" for c in result["checks"]))  # 40 pieces, 2027, 2030 were never said


class LineChangeTests(TestCase):
    def test_changes_are_worked_out_by_the_app(self):
        from .views_brand import line_changes
        rows = line_changes("A\nB\nC\nD\nE", "A\nB\nX\nD\nE")
        self.assertEqual([(r["kind"], r["text"]) for r in rows],
                         [("same", "B"), ("removed", "C"), ("added", "X"), ("same", "D")])

    def test_identical_texts_have_no_changes(self):
        from .views_brand import line_changes
        self.assertEqual(line_changes("A\nB", "A\nB"), [])


OPTIONS = json.dumps({"options": [
    {"label": "Undyed earth", "text": "Colour palette: Oat #D8CBB0, Undyed ecru #EDE6D6, Walnut #5C4632.",
     "colours": [{"name": "Oat", "hex": "#d8cbb0"}, {"name": "Ecru", "hex": "#EDE6D6"}, {"name": "Bad", "hex": "brown"}]},
    {"label": "Stone and flax", "text": "Colour palette: Flax #C9B48A, Stone #A39B8B, Charcoal wool #4A4744.", "colours": []},
], "note": ""})
PALETTE = "Colour palette (To confirm): named colours with hex values. Direction given by founder: natural undyed tones."


@override_settings(FDW_DRY_RUN=False, FDW_OPTIONS_DAILY_LIMIT_USD=Decimal("1"))
class OpenPointTests(TestCase):
    def setUp(self):
        brief = BrandBrief.current()
        brief.text = "BRAND BRIEF\n" + PALETTE + "\nMaterials (To confirm): no synthetic fibres.\n\nHard rules:\n- Never use leather.\n"
        brief.save()

    def test_every_open_line_is_listed(self):
        points = analyst.open_points(BrandBrief.current())
        self.assertEqual([p["line"] for p in points], [PALETTE, "Materials (To confirm): no synthetic fibres."])

    def test_options_are_suggested_for_one_line_with_valid_swatches_only(self):
        client = ScriptedAnalyst(OPTIONS)
        saved = analyst.suggest_options(PALETTE, client)
        self.assertIn("natural undyed tones", client.calls[0]["prompt"])
        self.assertIn("- Never use leather.", client.calls[0]["prompt"])
        self.assertEqual(saved.options[0]["colours"], [{"name": "Oat", "hex": "#D8CBB0"}, {"name": "Ecru", "hex": "#EDE6D6"}])
        self.assertEqual(analyst.open_points(BrandBrief.current())[0]["options"][1]["label"], "Stone and flax")
        call = AnalystCall.objects.get()
        self.assertEqual((call.kind, call.interview), ("options", None))

    def test_an_option_that_keeps_the_marker_is_sent_back(self):
        bad = json.dumps({"options": [{"label": "A", "text": "Colour palette (To confirm): oat."},
                                      {"label": "B", "text": "Colour palette: stone."}]})
        client = ScriptedAnalyst(bad, OPTIONS)
        analyst.suggest_options(PALETTE, client)
        self.assertIn("must not keep '(To confirm)'", client.calls[1]["prompt"])

    def test_settling_replaces_only_that_line_and_keeps_a_version(self):
        analyst.suggest_options(PALETTE, ScriptedAnalyst(OPTIONS))
        analyst.settle_point(PALETTE, "Colour palette: Oat #D8CBB0, Undyed ecru #EDE6D6, Walnut #5C4632.")
        text = BrandBrief.current().text
        self.assertIn("Colour palette: Oat #D8CBB0, Undyed ecru #EDE6D6, Walnut #5C4632.", text)
        self.assertNotIn(PALETTE, text)
        self.assertIn("Materials (To confirm): no synthetic fibres.", text)
        self.assertIn("- Never use leather.", text)
        self.assertEqual(BrandBriefVersion.objects.filter(source=BrandBriefVersion.Source.BEFORE).count(), 1)
        self.assertEqual(len(analyst.open_points(BrandBrief.current())), 1)
        self.assertFalse(OpenPointOptions.objects.filter(line=PALETTE).exists())

    def test_settling_refuses_the_marker_an_empty_line_and_a_changed_line(self):
        for line, text, message in ((PALETTE, "Colour palette (To confirm): oat", "Remove"),
                                    (PALETTE, "  ", "Choose an option"),
                                    ("A line that is not there (To confirm)", "x", "changed")):
            with self.assertRaisesMessage(analyst.AnalystError, message):
                analyst.settle_point(line, text)

    def test_the_daily_limit_stops_more_suggestions(self):
        AnalystCall.objects.create(kind="options", model="m", prompt="p", cost_usd=Decimal("1.00"))
        client = ScriptedAnalyst(OPTIONS)
        with self.assertRaisesMessage(analyst.AnalystError, "FDW_OPTIONS_DAILY_LIMIT_USD"):
            analyst.suggest_options(PALETTE, client)
        self.assertEqual(client.calls, [])

    def test_the_interview_asks_about_open_lines_with_concrete_choices(self):
        self.assertIn('every line of the current brief still marked "(To confirm)"', prompts.BRAND_ANALYST)
        self.assertIn("hex values", prompts.BRAND_ANALYST)


@override_settings(FDW_DRY_RUN=True)
class OpenPointPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="pw"))
        brief = BrandBrief.current()
        brief.text = "BRAND BRIEF\n" + PALETTE + "\n"
        brief.save()

    def test_suggest_pick_and_settle_through_the_page(self):
        page = self.client.get(reverse("brand_brief"))
        self.assertContains(page, "Open points")
        self.assertContains(page, "Suggest options")
        self.client.post(reverse("brand_suggest_options"), {"line": PALETTE})
        page = self.client.get(reverse("brand_brief"))
        self.assertContains(page, "Sample direction 1")
        self.assertContains(page, 'class="swatch" style="background: #D8CBB0"')
        self.client.post(reverse("brand_settle_point"), {"line": PALETTE, "choice": "Colour palette: Oat #D8CBB0.", "own": ""})
        self.assertIn("Colour palette: Oat #D8CBB0.", BrandBrief.current().text)
        page = self.client.get(reverse("brand_brief"))
        self.assertNotContains(page, "Open points")
        self.assertContains(page, "All lines confirmed")

    def test_your_own_wording_wins_over_the_picked_option(self):
        self.client.post(reverse("brand_settle_point"), {"line": PALETTE, "choice": "Colour palette: Oat.",
                                                          "own": "Colour palette: Oat #D8CBB0 and Walnut #5C4632."})
        self.assertIn("Colour palette: Oat #D8CBB0 and Walnut #5C4632.", BrandBrief.current().text)
