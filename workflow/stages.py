"""Stage logic and approval gates for stages 1 and 2.

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
from .llm import StageError, get_client, parse_json, run_call, run_json, run_result
from .models import (
    Approval,
    BrandBrief,
    Concept,
    ConceptReview,
    Cycle,
    DemandBrief,
    Piece,
    Signal,
    TrendFinding,
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


# --- Stage 1: market research -------------------------------------------------


def _direction(cycle: Cycle) -> str:
    return cycle.direction.strip() or "None given."


def brief_as_text(data: dict) -> str:
    """The structured brief as plain text, for the designer step and for the record."""
    lines = [f"Season: {data['season']}", "", data["summary"], "", "Demand themes, strongest first:"]
    for i, theme in enumerate(data["themes"], 1):
        basis = ", ".join(b.replace("_", " ") for b in theme["basis"])
        lines.append(
            f"{i}. {theme['name']} [evidence: {theme['strength']}; time frame: "
            f"{theme['timeframe'].replace('_', ' ')}; rests on: {basis}]"
        )
        lines.append(f"   {theme['what_people_want']}")
        lines.append(f"   Why this rating: {theme['strength_reason']}")
        if theme.get("audience_signals"):
            lines.append(f"   Audience signals: {theme['audience_signals']}")
        for source in theme["sources"]:
            lines.append(f"   Source: {source['title']} ({source['url']})")
    view = data["season_view"]
    lines += [
        "",
        "Season view:",
        f"- Returns every year: {'; '.join(view['returns_every_year']) or 'nothing found'}",
        f"- New this year: {'; '.join(view['new_this_year']) or 'nothing found'}",
        f"- Fading, avoid: {'; '.join(view['fading']) or 'nothing found'}",
        "",
        f"Founder's direction: {data['direction']['verdict'].replace('_', ' ')}."
        + (f" {data['direction']['explanation']}" if data["direction"].get("explanation") else ""),
        "",
        f"Earlier price tests: {data['past_tests']}",
    ]
    if data["gaps"]:
        lines += ["", "Gaps:"] + [f"- {gap}" for gap in data["gaps"]]
    if data["conflicts"]:
        lines += ["", "Conflicts with the brand brief:"] + [f"- {c}" for c in data["conflicts"]]
    return "\n".join(lines)


def _target_season(cycle: Cycle) -> str:
    if cycle.season_label:
        return cycle.season_label
    return "Not set. Research the next season to arrive in the markets named in the brand brief, and say which season that is."


def run_research(cycle: Cycle, brand_brief: str, client) -> list:
    """Search the web for current demand and return findings, each marked verified or not.

    A finding is verified only when its URL is one the search tool actually
    returned in this run. This is checked in code, so a source the model
    invented or altered can never reach the brief.
    """
    prompt = prompts.render(
        prompts.RESEARCH_TRENDS,
        brand_brief=brand_brief,
        founder_direction=_direction(cycle),
        target_season=_target_season(cycle),
        today=timezone.localdate().isoformat(),
    )
    result = run_result(
        cycle, "research_trends", prompt, settings.FDW_MODEL_WRITER, 8000, client,
        web_search_max=settings.FDW_RESEARCH_MAX_SEARCHES,
    )
    try:
        data = schemas.validate_findings(parse_json(result.text))
    except schemas.SchemaError as first:
        # Reformat without searching again, so a formatting slip does not cost more searches.
        reformat_prompt = prompts.render(
            prompts.REFORMAT_FINDINGS,
            problems="\n".join(f"- {problem}" for problem in first.problems[:20]),
            previous_answer=result.text,
        )
        text = run_call(cycle, "reformat_findings", reformat_prompt, settings.FDW_MODEL_WRITER, 8000, client)
        try:
            data = schemas.validate_findings(parse_json(text))
        except schemas.SchemaError as second:
            raise StageError(
                "The research output was invalid twice, so nothing was saved. "
                f"First problems: {'; '.join(second.problems[:5])}"
            ) from second
    known = {schemas.normalise_url(url) for url in result.source_urls}
    findings = []
    for item in data["findings"]:
        finding = dict(item)
        finding["verified"] = schemas.normalise_url(finding["source_url"]) in known
        findings.append(finding)
    return findings


def run_stage1(cycle: Cycle, client=None) -> DemandBrief:
    """Research the market and write the research brief.

    Nothing needs to be typed in. Audience messages and the founder's
    direction are optional extra inputs.
    """
    if cycle.status not in (Cycle.Status.NEW, Cycle.Status.BRIEF_REJECTED):
        raise GateError(
            "Stage 1 has already produced a research brief for this cycle. "
            "Decide on that brief before running the stage again."
        )
    client = client or get_client()
    brand_brief = BrandBrief.current().text

    extracted = []
    raw_items = list(cycle.raw_items.all())
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

    findings = run_research(cycle, brand_brief, client)
    verified = [f for f in findings if f["verified"]]
    if not verified and not extracted and not cycle.direction.strip():
        raise StageError(
            "The research returned no findings with a confirmed source, and there is no "
            "direction or audience signal to work from. Nothing was saved. Run it again, "
            "or add a direction first."
        )

    brief_prompt = prompts.render(
        prompts.DEMAND_BRIEF,
        brand_brief=brand_brief,
        founder_direction=_direction(cycle),
        target_season=_target_season(cycle),
        findings_json=json.dumps(
            [{k: v for k, v in f.items() if k != "verified"} for f in verified],
            ensure_ascii=False,
        ),
        signals_json=json.dumps(extracted, ensure_ascii=False),
        past_test_results=NO_PAST_RESULTS,
    )
    allowed_urls = {schemas.normalise_url(f["source_url"]) for f in verified}
    brief_data = run_json(
        cycle, "demand_brief", brief_prompt, settings.FDW_MODEL_WRITER, 5000,
        lambda data: schemas.validate_brief(data, allowed_urls), client,
    )
    brief_text = brief_as_text(brief_data)

    with transaction.atomic():
        cycle.signals.all().delete()
        cycle.findings.all().delete()
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
        TrendFinding.objects.bulk_create(
            TrendFinding(
                cycle=cycle,
                theme=str(f["theme"])[:200],
                what_people_want=str(f["what_people_want"]),
                evidence=str(f["evidence"]),
                source_title=str(f.get("source_title") or "")[:300],
                source_url=str(f["source_url"])[:1000],
                source_date=str(f.get("source_date") or "")[:20],
                relevance=str(f.get("relevance") or ""),
                timeframe=f["timeframe"],
                seasons_observed=str(f.get("seasons_observed") or "")[:200],
                verified=f["verified"],
            )
            for f in findings
        )
        brief = DemandBrief.objects.create(cycle=cycle, data=brief_data, text=brief_text)
        cycle.status = Cycle.Status.BRIEF_PENDING
        cycle.save(update_fields=["status"])
    return brief


def decide_brief(brief: DemandBrief, decision: str, notes: str = "", user: str = "") -> DemandBrief:
    """Gate 1. Record the founder's decision on the research brief."""
    if decision not in ("approve", "reject"):
        raise StageError("The decision must be approve or reject.")
    if brief.status != DemandBrief.Status.PENDING:
        raise GateError("This research brief has already been decided.")
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
            cycle=cycle, stage=1, item=f"Research brief #{brief.pk}",
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
    """Plan three collection concepts, specify each one, and review each one separately."""
    brief = cycle.approved_brief
    if cycle.status != Cycle.Status.BRIEF_APPROVED or brief is None:
        raise GateError(
            "Stage 2 needs an approved research brief. Approve the brief at gate 1 first."
        )
    client = client or get_client()
    brand_brief = BrandBrief.current().text

    # Planning and specifying are separate calls. Three fully specified
    # collections do not fit in one answer, and a cut-off answer saves nothing.
    plan_prompt = prompts.render(
        prompts.PLAN_CONCEPTS,
        brand_brief=brand_brief,
        approved_demand_brief=brief.text,
        founder_direction=_direction(cycle),
        target_season=_target_season(cycle),
        gate1_notes=brief.founder_notes or "No notes.",
        past_test_results=NO_PAST_RESULTS,
    )
    plans = run_json(
        cycle, "plan_concepts", plan_prompt, settings.FDW_MODEL_WRITER,
        6000, schemas.validate_concept_plans, client,
    )
    generated = {"concepts": []}
    for plan in plans["concepts"]:
        detail_prompt = prompts.render(
            prompts.DETAIL_CONCEPT,
            brand_brief=brand_brief,
            approved_demand_brief=brief.text,
            target_season=_target_season(cycle),
            concept_plan_json=json.dumps(plan, ensure_ascii=False, indent=1),
        )
        generated["concepts"].append(
            run_json(
                cycle, "detail_concept", detail_prompt, settings.FDW_MODEL_WRITER, 12000,
                lambda data, plan=plan: schemas.validate_concept_against_plan(data, plan), client,
            )
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
                12000, schemas.validate_single_concept, client,
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
