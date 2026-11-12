"""Stage logic and approval gates for slice 1.

The order of work is fixed in code. A stage refuses to run unless the gate
before it has an explicit approval, so nothing depends on a model choosing to
wait. Model calls happen first and results are saved together at the end, so
a failed stage leaves the cycle exactly as it was.
"""
import json
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import prompts, schemas
from .llm import StageError, get_client, run_call, run_json
from .models import (
    Approval,
    BrandBrief,
    Concept,
    ConceptReview,
    Cycle,
    DemandBrief,
    Piece,
    Signal,
)

MAX_CONCEPT_REVISIONS = 1
NO_PAST_RESULTS = "None yet. This is the first cycle."


class GateError(StageError):
    """A stage was asked to run before its gate was passed."""


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _clean_attributes(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


# --- Stage 1: demand signals -------------------------------------------------


def run_stage1(cycle: Cycle, client=None) -> DemandBrief:
    """Extract signals from the cycle's raw items and write the demand brief."""
    if cycle.status not in (Cycle.Status.NEW, Cycle.Status.BRIEF_REJECTED):
        raise GateError(
            "Stage 1 has already produced a demand brief for this cycle. "
            "Decide on that brief before running the stage again."
        )
    raw_items = list(cycle.raw_items.all())
    if not raw_items:
        raise StageError("Add at least one message, comment or trend line before running stage 1.")
    client = client or get_client()
    brand_brief = BrandBrief.current().text

    extracted = []
    offset = 0
    for batch in _chunks(raw_items, settings.FDW_EXTRACT_BATCH_SIZE):
        lines = [
            f"[{offset + i + 1}] ({item.source}) {' '.join(item.text.split())}"
            for i, item in enumerate(batch)
        ]
        offset += len(batch)
        prompt = prompts.render(prompts.EXTRACT_SIGNALS, raw_items="\n".join(lines))
        extracted.extend(
            run_json(
                cycle, "extract_signals", prompt, settings.FDW_MODEL_EXTRACTOR,
                8000, schemas.validate_signals, client,
            )
        )
    if not extracted:
        raise StageError("No signals could be extracted from the items. Nothing was saved.")

    brief_prompt = prompts.render(
        prompts.DEMAND_BRIEF,
        brand_brief=brand_brief,
        signals_json=json.dumps(extracted, ensure_ascii=False),
        past_test_results=NO_PAST_RESULTS,
    )
    brief_text = run_call(
        cycle, "demand_brief", brief_prompt, settings.FDW_MODEL_WRITER, 4000, client
    ).strip()
    if not brief_text:
        raise StageError("The model returned an empty demand brief. Nothing was saved.")

    with transaction.atomic():
        cycle.signals.all().delete()
        Signal.objects.bulk_create(
            Signal(
                cycle=cycle,
                source=s["source"],
                date=str(s.get("date") or "")[:20],
                product_type=str(s.get("product_type") or "")[:200],
                technique=str(s.get("technique") or "")[:40],
                attributes=_clean_attributes(s.get("attributes")),
                intent=s["intent"],
                strength=s.get("strength"),
                quote=str(s.get("quote") or "")[:300],
            )
            for s in extracted
        )
        brief = DemandBrief.objects.create(cycle=cycle, text=brief_text)
        cycle.status = Cycle.Status.BRIEF_PENDING
        cycle.save(update_fields=["status"])
    return brief


def decide_brief(brief: DemandBrief, decision: str, notes: str = "", user: str = "") -> DemandBrief:
    """Gate 1. Record the founder's decision on the demand brief."""
    if decision not in ("approve", "reject"):
        raise StageError("The decision must be approve or reject.")
    if brief.status != DemandBrief.Status.PENDING:
        raise GateError("This demand brief has already been decided.")
    if decision == "reject" and not notes.strip():
        raise StageError("Give a reason when rejecting, so the next run can take it into account.")
    cycle = brief.cycle
    with transaction.atomic():
        brief.status = (
            DemandBrief.Status.APPROVED if decision == "approve" else DemandBrief.Status.REJECTED
        )
        brief.founder_notes = notes.strip()
        brief.decided_at = timezone.now()
        brief.save(update_fields=["status", "founder_notes", "decided_at"])
        cycle.status = (
            Cycle.Status.BRIEF_APPROVED if decision == "approve" else Cycle.Status.BRIEF_REJECTED
        )
        cycle.save(update_fields=["status"])
        Approval.objects.create(
            cycle=cycle, stage=1, item=f"Demand brief #{brief.pk}",
            decision="approved" if decision == "approve" else "rejected",
            notes=notes.strip(), decided_by=user,
        )
    return brief


# --- Stage 2: collection concepts -------------------------------------------


def _review(cycle, concept_data, brand_brief, brief_text, client):
    prompt = prompts.render(
        prompts.REVIEW_CONCEPT,
        brand_brief=brand_brief,
        approved_demand_brief=brief_text,
        one_concept_json=json.dumps(concept_data, ensure_ascii=False, indent=1),
    )
    review = run_json(
        cycle, "review_concept", prompt, settings.FDW_MODEL_WRITER,
        3000, schemas.validate_review, client,
    )
    # Rules that must hold whatever the reviewer model concluded.
    code_issues = schemas.code_blocking_issues(concept_data)
    review["code_issues"] = code_issues
    review["blocking_issues"] = [str(i) for i in review["blocking_issues"]] + code_issues
    if review["blocking_issues"] and review["overall"] == "recommend":
        review["overall"] = "revise"
    return review


def run_stage2(cycle: Cycle, client=None) -> list:
    """Generate three collection concepts and review each one separately."""
    brief = cycle.approved_brief
    if cycle.status != Cycle.Status.BRIEF_APPROVED or brief is None:
        raise GateError(
            "Stage 2 needs an approved demand brief. Approve the brief at gate 1 first."
        )
    client = client or get_client()
    brand_brief = BrandBrief.current().text

    prompt = prompts.render(
        prompts.GENERATE_CONCEPTS,
        brand_brief=brand_brief,
        approved_demand_brief=brief.text,
        gate1_notes=brief.founder_notes or "No notes.",
        past_test_results=NO_PAST_RESULTS,
    )
    generated = run_json(
        cycle, "generate_concepts", prompt, settings.FDW_MODEL_WRITER,
        16000, schemas.validate_concepts, client,
    )

    results = []
    for concept_data in generated["concepts"]:
        reviews = [_review(cycle, concept_data, brand_brief, brief.text, client)]
        revisions = 0
        while reviews[-1]["blocking_issues"] and revisions < MAX_CONCEPT_REVISIONS:
            revise_prompt = prompts.render(
                prompts.REVISE_CONCEPT,
                brand_brief=brand_brief,
                approved_demand_brief=brief.text,
                one_concept_json=json.dumps(concept_data, ensure_ascii=False, indent=1),
                issues="\n".join(f"- {issue}" for issue in reviews[-1]["blocking_issues"]),
            )
            revised = run_json(
                cycle, "revise_concept", revise_prompt, settings.FDW_MODEL_WRITER,
                8000, schemas.validate_single_concept, client,
            )
            # The revision must stay the same concept, whatever the model returned.
            revised["concept_id"] = concept_data["concept_id"]
            concept_data = revised
            revisions += 1
            reviews.append(_review(cycle, concept_data, brand_brief, brief.text, client))
        results.append((concept_data, reviews, revisions))

    with transaction.atomic():
        cycle.concepts.all().delete()
        saved = []
        for concept_data, reviews, revisions in results:
            unresolved = bool(reviews[-1]["blocking_issues"])
            concept = Concept.objects.create(
                cycle=cycle,
                concept_id=str(concept_data["concept_id"])[:12],
                name=str(concept_data["name"])[:200],
                data=concept_data,
                revision_count=revisions,
                status=Concept.Status.UNRESOLVED if unresolved else Concept.Status.PROPOSED,
            )
            for review in reviews:
                ConceptReview.objects.create(concept=concept, data=review, overall=review["overall"])
            saved.append(concept)
        cycle.status = Cycle.Status.CONCEPTS_PENDING
        cycle.save(update_fields=["status"])
    return saved


def select_concepts(cycle: Cycle, concept_pks, notes: str = "", user: str = "") -> list:
    """Gate 2. The founder picks one or two concepts; their pieces become approved specifications."""
    if cycle.status != Cycle.Status.CONCEPTS_PENDING:
        raise GateError("There are no concepts awaiting selection for this cycle.")
    chosen = list(cycle.concepts.filter(pk__in=list(concept_pks)))
    if not 1 <= len(chosen) <= 2:
        raise StageError("Pick one or two concepts.")
    with transaction.atomic():
        pieces = []
        for concept in cycle.concepts.all():
            if concept in chosen:
                concept.status = Concept.Status.SELECTED
                concept.save(update_fields=["status"])
                for spec in concept.data["pieces"]:
                    pieces.append(
                        Piece.objects.create(
                            concept=concept,
                            piece_id=str(spec["piece_id"])[:20],
                            name=str(spec["name"])[:200],
                            role=spec["role"],
                            production_route=spec["production_route"],
                            target_price_eur=Decimal(str(spec["target_price_eur"])),
                            spec=spec,
                        )
                    )
            else:
                concept.status = Concept.Status.NOT_SELECTED
                concept.save(update_fields=["status"])
        cycle.status = Cycle.Status.CONCEPTS_SELECTED
        cycle.save(update_fields=["status"])
        Approval.objects.create(
            cycle=cycle, stage=2,
            item=", ".join(f"{c.concept_id} {c.name}" for c in chosen),
            decision="selected", notes=notes.strip(), decided_by=user,
        )
    return pieces
