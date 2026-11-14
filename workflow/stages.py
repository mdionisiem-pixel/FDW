"""Stage logic and approval gates for stages 1 to 3.

The order of work is fixed in code. A stage refuses to run unless the gate
before it has an explicit approval, so nothing depends on a model choosing to
wait. Model calls happen first and results are saved together at the end, so
a failed stage leaves the cycle exactly as it was.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction
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
    SourcingCheck,
    SourcingResult,
    Supplier,
    TrendFinding,
)

MAX_CONCEPT_REVISIONS = 1
NO_PAST_RESULTS = "None yet. This is the first cycle."


class GateError(StageError):
    """A stage was asked to run before its gate was passed."""


def _in_parallel(work, items):
    """Run ``work`` on each item, several at a time, and return the results in order.

    Used only for model calls that do not depend on each other. Each worker
    closes its own database connection when it finishes. If any call fails,
    its error is raised once all running calls have stopped, and because the
    stage saves its results only at the end, nothing is saved.
    """
    workers = max(1, settings.FDW_PARALLEL_CALLS)
    if workers == 1 or len(items) < 2:
        return [work(item) for item in items]

    def run(item):
        try:
            return work(item)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(run, items))


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


def _words(value) -> str:
    return str(value).replace("_", " ")


def brief_as_text(data: dict) -> str:
    """The market report as plain text, for the designer step and for the record."""
    lines = [f"Season: {data['season']}", "", f"Headline: {data['headline']}", "", "Key takeaways:"]
    for takeaway in data["key_takeaways"]:
        lines.append(f"- {takeaway['headline']} So what: {takeaway['so_what']}")
    lines += ["", "Demand themes, strongest market opportunity first:"]
    for i, theme in enumerate(data["themes"], 1):
        basis = ", ".join(_words(b) for b in theme["basis"])
        history = ", ".join(f"{h['period']}: {h['level']}" for h in theme["history"])
        lines.append(
            f"{i}. {theme['name']} ({_words(theme['category'])}) [evidence: {theme['strength']}; "
            f"momentum: {theme['momentum']}; time frame: {_words(theme['timeframe'])}; "
            f"market attractiveness {theme['attractiveness']}/5; fit with Malina {theme['fit']}/5; rests on: {basis}]"
        )
        lines.append(f"   {theme['what_people_want']}")
        lines.append(f"   Why this rating: {theme['strength_reason']}")
        lines.append(f"   How Malina could make it: {theme['fit_note']}")
        lines.append(f"   Over time: {history}. Outlook: {theme['outlook']['level']} ({theme['outlook']['reason']})")
        if theme.get("audience_signals"):
            lines.append(f"   Audience signals: {theme['audience_signals']}")
        for source in theme["sources"]:
            lines.append(f"   Source: {source['title']} ({source['url']})")
    if data["market_shifts"]:
        lines += ["", "Market shifts:"] + [
            f"- From {m['from']} to {m['to']}. {m['evidence']}" for m in data["market_shifts"]
        ]
    view = data["season_view"]
    lines += [
        "",
        "Season view:",
        f"- Returns every year: {'; '.join(view['returns_every_year']) or 'nothing found'}",
        f"- New this year: {'; '.join(view['new_this_year']) or 'nothing found'}",
        f"- Fading, avoid: {'; '.join(view['fading']) or 'nothing found'}",
    ]
    if data["beyond_current_range"]:
        lines += ["", "Opportunities beyond Malina's current range:"] + [
            f"- {o['opportunity']}. Why: {o['why']} Route: {o['route']}" for o in data["beyond_current_range"]
        ]
    lines += ["", "Strategic implications:"] + [f"- {i['title']}: {i['action']}" for i in data["implications"]]
    lines += [
        "",
        f"Founder's direction: {_words(data['direction']['verdict'])}."
        + (f" {data['direction']['explanation']}" if data["direction"].get("explanation") else ""),
        "",
        f"Earlier price tests: {data['past_tests']}",
    ]
    if data["gaps"]:
        lines += ["", "Gaps:"] + [f"- {gap}" for gap in data["gaps"]]
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
        cycle, "research_trends", prompt, settings.FDW_MODEL_WRITER, settings.FDW_MAX_OUTPUT_TOKENS, client,
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
        text = run_call(cycle, "reformat_findings", reformat_prompt, settings.FDW_MODEL_WRITER, settings.FDW_MAX_OUTPUT_TOKENS, client)
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
        finding["figures"] = schemas.supported_figures(item)
        finding["forecast"] = schemas.supported_forecast(item)
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
                settings.FDW_MAX_OUTPUT_TOKENS, schemas.validate_signals, client,
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
        cycle, "demand_brief", brief_prompt, settings.FDW_MODEL_WRITER, settings.FDW_MAX_OUTPUT_TOKENS,
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
                category=f["category"],
                momentum=f["momentum"],
                malina_fit=f["malina_fit"],
                figures=f["figures"],
                forecast=f["forecast"],
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
        design_standard=prompts.DESIGN_STANDARD,
        brand_brief=brand_brief,
        approved_demand_brief=brief_text,
        one_concept_json=json.dumps(concept_data, ensure_ascii=False, indent=1),
    )
    review = run_json(
        cycle, "review_concept", prompt, settings.FDW_MODEL_WRITER,
        settings.FDW_MAX_OUTPUT_TOKENS, schemas.validate_review, client,
        effort=settings.FDW_REVIEW_EFFORT,
    )
    # Rules that must hold whatever the reviewer model concluded.
    code_issues = schemas.code_blocking_issues(concept_data)
    review["code_issues"] = code_issues
    review["blocking_issues"] = [str(i) for i in review["blocking_issues"]] + code_issues
    if review["blocking_issues"] and review["overall"] == "recommend":
        review["overall"] = "revise"
    review["design_issues"] = design_issues(review)
    if review["design_issues"] and review["overall"] == "recommend":
        review["overall"] = "revise"
    return review


def design_issues(review) -> list:
    """Creative scores below the bar, worded as issues for the designer to answer."""
    minimum = settings.FDW_DESIGN_MIN_SCORE
    issues = []
    for criterion in schemas.DESIGN_CRITERIA:
        score = review.get("scores", {}).get(criterion)
        if isinstance(score, (int, float)) and score < minimum:
            reason = str(review.get("reasons", {}).get(criterion, "")).strip() or "Below the design standard."
            label = criterion.replace("_", " ")
            issues.append(f"Design: {label} scored {score:g} of 5. {reason}")
    return issues


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
        design_standard=prompts.DESIGN_STANDARD,
        brand_brief=brand_brief,
        approved_demand_brief=brief.text,
        founder_direction=_direction(cycle),
        target_season=_target_season(cycle),
        gate1_notes=brief.founder_notes or "No notes.",
        past_test_results=NO_PAST_RESULTS,
    )
    plans = run_json(
        cycle, "plan_concepts", plan_prompt, settings.FDW_MODEL_DESIGNER,
        settings.FDW_MAX_OUTPUT_TOKENS, schemas.validate_concept_plans, client,
    )
    # The three collections do not depend on each other, so each one is
    # specified, reviewed and if needed revised alongside the others.
    def specify(plan):
        detail_prompt = prompts.render(
            prompts.DETAIL_CONCEPT,
            design_standard=prompts.DESIGN_STANDARD,
            brand_brief=brand_brief,
            approved_demand_brief=brief.text,
            target_season=_target_season(cycle),
            concept_plan_json=json.dumps(plan, ensure_ascii=False, indent=1),
        )
        return run_json(
            cycle, "detail_concept", detail_prompt, settings.FDW_MODEL_DESIGNER, settings.FDW_MAX_OUTPUT_TOKENS,
            lambda data: schemas.validate_concept_against_plan(data, plan), client,
        )

    def review_and_revise(concept_data):
        reviews = [_review(cycle, concept_data, brand_brief, brief.text, client)]
        revisions = 0
        def issues_of(review):
            return review["blocking_issues"] + review.get("design_issues", [])

        while issues_of(reviews[-1]) and revisions < MAX_CONCEPT_REVISIONS:
            revise_prompt = prompts.render(
                prompts.REVISE_CONCEPT,
                design_standard=prompts.DESIGN_STANDARD,
                brand_brief=brand_brief,
                approved_demand_brief=brief.text,
                one_concept_json=json.dumps(concept_data, ensure_ascii=False, indent=1),
                issues="\n".join(f"- {issue}" for issue in issues_of(reviews[-1])),
            )
            revised = run_json(
                cycle, "revise_concept", revise_prompt, settings.FDW_MODEL_DESIGNER,
                settings.FDW_MAX_OUTPUT_TOKENS, schemas.validate_single_concept, client,
            )
            # The revision must stay the same concept, whatever the model returned.
            revised["concept_id"] = concept_data["concept_id"]
            concept_data = revised
            revisions += 1
            reviews.append(_review(cycle, concept_data, brand_brief, brief.text, client))
        return concept_data, reviews, revisions

    results = _in_parallel(lambda plan: review_and_revise(specify(plan)), plans["concepts"])

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


# --- Stage 3: sourcing check --------------------------------------------------


def run_stage3(cycle: Cycle, client=None) -> SourcingCheck:
    """Check every approved piece against the founder's supplier table.

    It may run again while the sheet awaits a decision, for example after the
    founder adds a supplier; the earlier sheet is then kept as superseded.
    """
    if cycle.status not in (Cycle.Status.CONCEPTS_SELECTED, Cycle.Status.SOURCING_PENDING):
        raise GateError("Stage 3 needs a selected collection. Pick one or two concepts at gate 2 first.")
    pieces = list(cycle.selected_pieces.order_by("piece_id"))
    if not pieces:
        raise StageError("The selected collection has no pieces to check.")
    suppliers = [s.as_prompt_data() for s in Supplier.objects.filter(active=True)]
    client = client or get_client()

    specs = {p.piece_id: p.spec for p in pieces}
    prompt = prompts.render(
        prompts.SOURCING_CHECK,
        approved_pieces_json=json.dumps([p.spec for p in pieces], ensure_ascii=False, indent=1),
        supplier_table_json=json.dumps(suppliers, ensure_ascii=False, indent=1)
        if suppliers else "[] (the table is empty: no supplier has been added yet)",
    )
    data = run_json(
        cycle, "sourcing_check", prompt, settings.FDW_MODEL_WRITER, settings.FDW_MAX_OUTPUT_TOKENS,
        lambda d: schemas.validate_sourcing(d, specs, suppliers), client,
    )
    entries = {item["piece_id"]: item for item in data["pieces"]}

    with transaction.atomic():
        cycle.sourcing_checks.filter(status=SourcingCheck.Status.PENDING).update(
            status=SourcingCheck.Status.SUPERSEDED
        )
        check = SourcingCheck.objects.create(
            cycle=cycle, summary=str(data["collection_summary"]), suppliers_sent=suppliers,
        )
        for piece in pieces:
            item = entries[piece.piece_id]
            checked = schemas.sourcing_code_checks(
                item, piece.spec, suppliers, settings.FDW_EUR_RATES, settings.FDW_MIN_GROSS_MARGIN,
            )
            SourcingResult.objects.create(
                sourcing_check=check,
                piece=piece,
                route=item["route"],
                verdict=item["status"],
                changes=[c for c in item["changes"] if c.strip()],
                candidate_suppliers=[
                    {"name": str(c["name"]), "changes_needed": str(c.get("changes_needed") or "")}
                    for c in item["candidate_suppliers"]
                ],
                questions=[q for q in item["questions_for_supplier"] if q.strip()],
                unit_cost=checked["unit_cost"],
                currency=checked["currency"],
                cost_basis=str(item.get("cost_basis") or "")[:2000]
                + (f" Model's arithmetic: {item['margin_calc']}" if item.get("margin_calc") else ""),
                margin=checked["margin"],
                warnings=checked["warnings"],
            )
        cycle.status = Cycle.Status.SOURCING_PENDING
        cycle.save(update_fields=["status"])
    return check


def decide_sourcing(check: SourcingCheck, decisions: dict, notes: str = "", user: str = "") -> SourcingCheck:
    """Gate 3. The founder decides on every piece: continue, continue at risk, or drop.

    ``decisions`` maps a SourcingResult primary key to a decision value. A
    piece with no supplier cannot simply continue; the founder must accept
    the risk explicitly, and it is then held to the waitlist.
    """
    if check.status != SourcingCheck.Status.PENDING or check.cycle.status != Cycle.Status.SOURCING_PENDING:
        raise GateError("This sourcing sheet is not awaiting a decision.")
    results = list(check.results.select_related("piece"))
    problems = []
    chosen = {}
    allowed = {value for value, _ in SourcingResult.Decision.choices if value}
    for result in results:
        decision = decisions.get(result.pk) or decisions.get(str(result.pk)) or ""
        name = f"{result.piece.piece_id} {result.piece.name}"
        if decision not in allowed:
            problems.append(f"Choose what happens to {name}.")
        elif decision == SourcingResult.Decision.APPROVED and result.verdict == SourcingResult.Verdict.NEEDS_SOURCING:
            problems.append(f"{name} has no supplier. Accept the risk explicitly, or drop it.")
        elif decision == SourcingResult.Decision.ACCEPT_RISK and result.verdict != SourcingResult.Verdict.NEEDS_SOURCING:
            problems.append(f"{name} has a supplier; choose Continue instead.")
        chosen[result.pk] = decision
    if problems:
        raise StageError(" ".join(problems))
    if all(d == SourcingResult.Decision.DROPPED for d in chosen.values()):
        raise StageError("Every piece would be dropped. Keep at least one, or run stage 2 again in a new cycle.")
    cycle = check.cycle
    with transaction.atomic():
        for result in results:
            result.decision = chosen[result.pk]
            result.save(update_fields=["decision"])
        check.status = SourcingCheck.Status.APPROVED
        check.founder_notes = notes.strip()
        check.decided_at = timezone.now()
        check.save(update_fields=["status", "founder_notes", "decided_at"])
        cycle.status = Cycle.Status.SOURCING_APPROVED
        cycle.save(update_fields=["status"])
        counts = {label: sum(1 for d in chosen.values() if d == value)
                  for value, label in SourcingResult.Decision.choices if value}
        Approval.objects.create(
            cycle=cycle, stage=3, item=f"Sourcing sheet #{check.pk}", decision="approved",
            notes="; ".join(f"{label}: {n}" for label, n in counts.items() if n)
            + (f". {notes.strip()}" if notes.strip() else ""),
            decided_by=user,
        )
    return check
