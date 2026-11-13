"""Tests for stage 3: the sourcing check, its code-enforced rules, and gate 3."""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import dryrun, schemas, stages
from .llm import DryRunClient, StageError
from .models import Approval, Cycle, ModelCall, SourcingCheck, SourcingResult, Supplier
from .tests import ScriptedClient, approved_cycle

RATES = {"EUR": Decimal("1"), "USD": Decimal("0.86")}


def selected_cycle(concept_id="C3"):
    """A cycle whose collection has been selected at gate 2, ready for stage 3."""
    cycle = approved_cycle()
    stages.run_stage2(cycle, client=DryRunClient())
    concept = cycle.concepts.get(concept_id=concept_id)
    stages.select_concepts(cycle, [concept.pk], "", "founder")
    cycle.refresh_from_db()
    return cycle


def dry_sheet(cycle):
    """The dry-run sourcing answer for this cycle, as a dict the tests can alter."""
    pieces = [p.spec for p in cycle.selected_pieces.order_by("piece_id")]
    suppliers = [s.as_prompt_data() for s in Supplier.objects.filter(active=True)]
    prompt = (
        f"<piece_specifications>\n{json.dumps(pieces)}\n</piece_specifications>\n"
        f"<supplier_table>\n{json.dumps(suppliers)}\n</supplier_table>"
    )
    return json.loads(dryrun.respond("sourcing_check", prompt))


class SupplierSeedTests(TestCase):
    def test_migration_seeds_the_three_suppliers_from_the_specification(self):
        names = set(Supplier.objects.values_list("name", flat=True))
        self.assertEqual(names, {"Printful knitwear", "Knitwise", "Wildemasche"})
        knitwise = Supplier.objects.get(name="Knitwise")
        self.assertEqual(knitwise.price_currency, "USD")
        self.assertIn("$27 to $49", knitwise.prices)

    def test_empty_fields_reach_the_prompt_as_not_stated(self):
        data = Supplier.objects.get(name="Wildemasche").as_prompt_data()
        self.assertEqual(data["prices"], "not stated")
        self.assertEqual(data["materials"], "not stated")


class Stage3GateTests(TestCase):
    def test_stage3_refuses_before_a_collection_is_selected(self):
        cycle = approved_cycle()
        with self.assertRaises(stages.GateError):
            stages.run_stage3(cycle, client=DryRunClient())
        self.assertFalse(SourcingCheck.objects.exists())

    def test_stage3_writes_one_result_per_piece(self):
        cycle = selected_cycle()
        check = stages.run_stage3(cycle, client=DryRunClient())
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.SOURCING_PENDING)
        self.assertEqual(cycle.stage_number, 3)
        self.assertEqual(check.results.count(), cycle.selected_pieces.count())
        self.assertEqual(len(check.suppliers_sent), 3)

    def test_running_again_while_pending_keeps_the_old_sheet_as_superseded(self):
        cycle = selected_cycle()
        first = stages.run_stage3(cycle, client=DryRunClient())
        second = stages.run_stage3(cycle, client=DryRunClient())
        first.refresh_from_db()
        self.assertEqual(first.status, SourcingCheck.Status.SUPERSEDED)
        self.assertEqual(second.status, SourcingCheck.Status.PENDING)

    def test_stage3_cannot_run_after_the_sheet_is_approved(self):
        cycle = selected_cycle()
        check = stages.run_stage3(cycle, client=DryRunClient())
        stages.decide_sourcing(check, {r.pk: "approved" for r in check.results.all()})
        cycle.refresh_from_db()
        with self.assertRaises(stages.GateError):
            stages.run_stage3(cycle, client=DryRunClient())


class CostAndMarginTests(TestCase):
    def test_without_an_exchange_rate_the_margin_is_left_blank(self):
        cycle = selected_cycle("C3")
        check = stages.run_stage3(cycle, client=DryRunClient())
        result = check.results.get(piece__piece_id="C3-P1")
        self.assertEqual(result.unit_cost, Decimal("27.00"))
        self.assertEqual(result.currency, "USD")
        self.assertIsNone(result.margin)
        self.assertTrue(any("exchange rate" in w for w in result.warnings))

    @override_settings(FDW_EUR_RATES=RATES)
    def test_margin_is_computed_in_code_from_the_traced_cost(self):
        cycle = selected_cycle("C3")
        check = stages.run_stage3(cycle, client=DryRunClient())
        result = check.results.get(piece__piece_id="C3-P1")  # 120 euros, cost 27 USD
        # (120 - 27 x 0.86) / 120 = 0.8065
        self.assertEqual(result.margin, Decimal("0.807"))
        self.assertEqual(result.margin_percent, 81)

    @override_settings(FDW_EUR_RATES=RATES)
    def test_margin_below_the_threshold_is_flagged(self):
        cycle = selected_cycle("C3")
        check = stages.run_stage3(cycle, client=DryRunClient())
        cheap = check.results.get(piece__piece_id="C3-P2")  # 39 euros, cost 27 USD -> 40%
        self.assertEqual(cheap.margin, Decimal("0.405"))
        self.assertTrue(any("below your 50% threshold" in w for w in cheap.warnings))

    def test_a_cost_not_in_the_supplier_table_is_discarded(self):
        cycle = selected_cycle("C3")
        sheet = dry_sheet(cycle)
        sheet["pieces"][0]["unit_cost"] = 18.5
        client = ScriptedClient({"sourcing_check": [json.dumps(sheet)]})
        check = stages.run_stage3(cycle, client=client)
        result = check.results.get(piece__piece_id=sheet["pieces"][0]["piece_id"])
        self.assertIsNone(result.unit_cost)
        self.assertIsNone(result.margin)
        self.assertTrue(any("not in the price of any supplier" in w for w in result.warnings))

    def test_cost_must_come_from_a_supplier_the_model_named(self):
        Supplier.objects.create(name="Other mill", route="on_demand", makes="Sweaters",
                                prices="Sweaters from 18.5", price_currency="EUR")
        cycle = selected_cycle("C3")
        sheet = dry_sheet(cycle)
        sheet["pieces"][0]["unit_cost"] = 18.5  # in Other mill's prices, but Knitwise was named
        check = stages.run_stage3(cycle, client=ScriptedClient({"sourcing_check": [json.dumps(sheet)]}))
        self.assertIsNone(check.results.get(piece__piece_id=sheet["pieces"][0]["piece_id"]).unit_cost)


class ValidationTests(TestCase):
    def test_a_supplier_not_in_the_table_is_sent_back_once(self):
        cycle = selected_cycle("C3")
        bad = dry_sheet(cycle)
        bad["pieces"][0]["candidate_suppliers"] = [{"name": "Invented Knits", "changes_needed": ""}]
        client = ScriptedClient({"sourcing_check": [json.dumps(bad)]})
        stages.run_stage3(cycle, client=client)
        self.assertEqual(client.calls, ["sourcing_check", "sourcing_check_retry"])
        retry = ModelCall.objects.get(step="sourcing_check_retry")
        self.assertIn("Invented Knits", retry.prompt)

    def test_hand_technique_on_a_machine_supplier_fails_twice_and_saves_nothing(self):
        cycle = selected_cycle("C1")
        bad = dry_sheet(cycle)
        hand = next(p for p in bad["pieces"] if p["route"] == "atelier")
        hand.update({"route": "on_demand", "candidate_suppliers": [{"name": "Knitwise", "changes_needed": ""}]})
        client = ScriptedClient({"sourcing_check": [json.dumps(bad)], "sourcing_check_retry": [json.dumps(bad)]})
        with self.assertRaises(StageError):
            stages.run_stage3(cycle, client=client)
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.CONCEPTS_SELECTED)
        self.assertFalse(SourcingCheck.objects.exists())

    def test_every_piece_must_be_covered(self):
        cycle = selected_cycle("C3")
        sheet = dry_sheet(cycle)
        specs = {p.piece_id: p.spec for p in cycle.selected_pieces}
        suppliers = [s.as_prompt_data() for s in Supplier.objects.all()]
        sheet["pieces"] = sheet["pieces"][1:]
        with self.assertRaises(schemas.SchemaError) as caught:
            schemas.validate_sourcing(sheet, specs, suppliers)
        self.assertTrue(any("missing" in p for p in caught.exception.problems))

    def test_with_changes_must_list_the_changes(self):
        cycle = selected_cycle("C3")
        sheet = dry_sheet(cycle)
        sheet["pieces"][0]["status"] = "makeable_with_changes"
        specs = {p.piece_id: p.spec for p in cycle.selected_pieces}
        suppliers = [s.as_prompt_data() for s in Supplier.objects.all()]
        with self.assertRaises(schemas.SchemaError):
            schemas.validate_sourcing(sheet, specs, suppliers)

    def test_a_supplied_piece_needs_a_supplier_on_the_same_route(self):
        cycle = selected_cycle("C1")
        sheet = dry_sheet(cycle)
        specialist = next(p for p in sheet["pieces"] if p["route"] == "specialist")
        specialist["status"] = "makeable_as_specified"  # but no specialist is in the table
        specs = {p.piece_id: p.spec for p in cycle.selected_pieces}
        suppliers = [s.as_prompt_data() for s in Supplier.objects.all()]
        with self.assertRaises(schemas.SchemaError) as caught:
            schemas.validate_sourcing(sheet, specs, suppliers)
        self.assertTrue(any("needs_sourcing" in p for p in caught.exception.problems))

    def test_a_route_change_away_from_atelier_warns_about_handmade(self):
        piece = {"production_route": "atelier", "target_price_eur": 100, "technique": "hand knit",
                 "materials": "Handmade in wool"}
        item = {"route": "specialist", "status": "needs_sourcing", "candidate_suppliers": [], "unit_cost": None}
        checked = schemas.sourcing_code_checks(item, piece, [], RATES, Decimal("0.5"))
        self.assertTrue(any("handmade" in w for w in checked["warnings"]))


class Gate3Tests(TestCase):
    def _pending(self, concept_id="C1"):
        cycle = selected_cycle(concept_id)
        return cycle, stages.run_stage3(cycle, client=DryRunClient())

    def test_a_piece_without_a_supplier_cannot_simply_continue(self):
        _, check = self._pending("C1")
        decisions = {r.pk: "approved" for r in check.results.all()}
        with self.assertRaises(StageError) as caught:
            stages.decide_sourcing(check, decisions)
        self.assertIn("Accept the risk", str(caught.exception))
        check.refresh_from_db()
        self.assertEqual(check.status, SourcingCheck.Status.PENDING)

    def test_accepting_risk_is_only_for_pieces_without_a_supplier(self):
        _, check = self._pending("C3")
        decisions = {r.pk: "accept_risk" for r in check.results.all()}
        with self.assertRaises(StageError):
            stages.decide_sourcing(check, decisions)

    def test_every_piece_needs_a_decision(self):
        _, check = self._pending("C3")
        first = check.results.first()
        with self.assertRaises(StageError):
            stages.decide_sourcing(check, {first.pk: "approved"})

    def test_dropping_every_piece_is_refused(self):
        _, check = self._pending("C3")
        with self.assertRaises(StageError):
            stages.decide_sourcing(check, {r.pk: "dropped" for r in check.results.all()})

    def test_approval_records_each_decision_and_unlocks_stage_4(self):
        cycle, check = self._pending("C1")
        decisions = {}
        for r in check.results.all():
            decisions[r.pk] = "accept_risk" if r.verdict == "needs_sourcing" else "approved"
        last = check.results.last()
        decisions[last.pk] = "dropped"
        stages.decide_sourcing(check, decisions, "Ask about yarn", "founder")
        cycle.refresh_from_db()
        check.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.SOURCING_APPROVED)
        self.assertEqual(cycle.stage_number, 4)
        self.assertEqual(check.status, SourcingCheck.Status.APPROVED)
        risky = check.results.filter(decision=SourcingResult.Decision.ACCEPT_RISK)
        self.assertTrue(risky.exists())
        self.assertTrue(all(r.waitlist_only for r in risky))
        approval = Approval.objects.get(cycle=cycle, stage=3)
        self.assertIn("Ask about yarn", approval.notes)

    def test_a_decided_sheet_cannot_be_decided_again(self):
        _, check = self._pending("C3")
        stages.decide_sourcing(check, {r.pk: "approved" for r in check.results.all()})
        with self.assertRaises(stages.GateError):
            stages.decide_sourcing(check, {r.pk: "dropped" for r in check.results.all()})


class Stage3ScreenTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("founder", password="pw-for-tests-only")
        self.client.force_login(user)

    def test_supplier_page_lists_and_adds_suppliers(self):
        response = self.client.get(reverse("suppliers"))
        self.assertContains(response, "Knitwise")
        response = self.client.post(reverse("suppliers"), {
            "name": "Atelier Lana", "route": "specialist", "makes": "Small runs of knitwear",
            "price_currency": "eur", "active": "on",
        })
        self.assertRedirects(response, reverse("suppliers"))
        self.assertEqual(Supplier.objects.get(name="Atelier Lana").price_currency, "EUR")

    def test_supplier_form_rejects_a_bad_currency(self):
        response = self.client.post(reverse("suppliers"), {
            "name": "Bad", "route": "specialist", "makes": "Things", "price_currency": "E1",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "three-letter code")
        self.assertFalse(Supplier.objects.filter(name="Bad").exists())

    def test_run_and_decide_through_the_screens(self):
        cycle = selected_cycle("C3")
        page = self.client.get(reverse("cycle_detail", args=[cycle.pk]))
        self.assertContains(page, "Run the sourcing check")
        self.client.post(reverse("run_stage3", args=[cycle.pk]))
        page = self.client.get(reverse("cycle_detail", args=[cycle.pk]))
        self.assertContains(page, "Gate 3: your decision")
        check = cycle.sourcing_checks.get(status="pending")
        data = {f"decision-{r.pk}": "approved" for r in check.results.all()}
        self.client.post(reverse("decide_sourcing", args=[cycle.pk]), data)
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, Cycle.Status.SOURCING_APPROVED)
        page = self.client.get(reverse("cycle_detail", args=[cycle.pk]))
        self.assertContains(page, "Continue")
        self.assertNotContains(page, "Gate 3: your decision")
