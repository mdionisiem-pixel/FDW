"""Screens for running a cycle: research the market, review the brief, pick a collection."""
from collections import Counter

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import charts, stages
from .llm import StageError
from .models import BrandBrief, Concept, Cycle, DemandBrief, RawItem
from .schemas import REVIEW_CRITERIA

STAGES = [
    (1, "Market research", "What people want now, with sources"),
    (2, "Collection concepts", "Three new collections, reviewed, you pick"),
    (3, "Sourcing check", "Can each piece be made, at what cost"),
    (4, "Visual prototypes", "Five photoreal shots per piece"),
    (5, "3D generation", "A rotating model of each piece"),
    (6, "Price test", "Waitlist with prices, then pre-orders"),
    (7, "Launch kit and learning", "Content, and results for next time"),
]
BUILT_STAGES = 2

CRITERION_LABELS = {
    "demand_evidence": "Demand evidence",
    "coherence": "Coherence",
    "uniqueness": "Uniqueness",
    "makeability": "Makeability",
    "price_fit": "Price fit",
    "spec_completeness": "Specification",
}


def _season_years():
    """The years a collection can sensibly be planned for: this year and the next two."""
    this_year = timezone.localdate().year
    return [this_year, this_year + 1, this_year + 2]


STRENGTH_LEVEL = {"strong": 3, "moderate": 2, "weak": 1, "untested": 0}
TIMEFRAME_LABELS = {
    "both": "Recurs and rising now",
    "recurring": "Recurs in past seasons",
    "current": "Current trend",
}
BASIS_LABELS = {
    "web_research": "Web research",
    "audience_signals": "Your audience",
    "founder_direction": "Your direction",
}
VERDICT_LABELS = {
    "supported": "Supported by the research",
    "mixed": "Mixed evidence",
    "contradicted": "The research points the other way",
    "untested": "Untested: the price test decides",
    "none_given": "No direction given",
}


CATEGORY_LABELS = charts.CATEGORY_LABELS
MOMENTUM_LABELS = charts.MOMENTUM_LABELS


def _brief_view(brief, findings):
    """Prepare the market report for the dashboard. Returns None for an older text-only brief."""
    if brief is None or not brief.data or "headline" not in brief.data:
        return None
    data = brief.data
    themes = []
    for number, theme in enumerate(data.get("themes", []), 1):
        themes.append({
            **theme,
            "number": number,
            "level": STRENGTH_LEVEL.get(theme.get("strength"), 0),
            "strength_label": str(theme.get("strength", "")).capitalize(),
            "timeframe_label": TIMEFRAME_LABELS.get(theme.get("timeframe"), ""),
            "momentum_label": MOMENTUM_LABELS.get(theme.get("momentum"), ""),
            "category_label": CATEGORY_LABELS.get(theme.get("category"), ""),
            "basis_labels": [BASIS_LABELS.get(b, b) for b in theme.get("basis", [])],
        })
    direction = data.get("direction", {})
    percentages, other_figures = charts.reported_figures(findings)
    return {
        "season": data.get("season", ""),
        "headline": data.get("headline", ""),
        "takeaways": data.get("key_takeaways", []),
        "kpis": charts.kpis(findings, data),
        "themes": themes,
        "matrix": charts.opportunity_matrix(themes) if themes else "",
        "periods": data.get("timeline_periods", []),
        "timeline": charts.timeline_rows(themes, data.get("timeline_periods", [])),
        "market_shifts": data.get("market_shifts", []),
        "percent_figures": percentages,
        "other_figures": other_figures,
        "forecasts": charts.published_forecasts(findings),
        "category_mix": charts.category_mix(findings),
        "season_view": data.get("season_view", {}),
        "beyond": data.get("beyond_current_range", []),
        "implications": data.get("implications", []),
        "direction": {
            "verdict": direction.get("verdict", "none_given"),
            "label": VERDICT_LABELS.get(direction.get("verdict"), ""),
            "explanation": direction.get("explanation") or "",
        },
        "past_tests": data.get("past_tests", ""),
        "gaps": data.get("gaps", []),
    }


def _finding_groups(findings):
    """Verified findings grouped by time frame, strongest grouping first."""
    groups = []
    for key in ("both", "recurring", "current"):
        items = [f for f in findings if f.verified and f.timeframe == key]
        if items:
            label = "Current trends" if key == "current" else TIMEFRAME_LABELS[key]
            groups.append({"key": key, "label": label, "items": items})
    unlabelled = [f for f in findings if f.verified and f.timeframe not in TIMEFRAME_LABELS]
    if unlabelled:
        groups.append({"key": "other", "label": "Findings", "items": unlabelled})
    return groups


def _apply_research_inputs(request, cycle):
    """Save the season and direction from a posted form. Returns an error message, or None."""
    season = request.POST.get("season", "")
    if season not in Cycle.Season.values:
        season = ""
    year = None
    raw_year = request.POST.get("season_year", "").strip()
    if season and season != Cycle.Season.YEAR_ROUND and raw_year:
        allowed = _season_years()
        if raw_year.isdigit() and int(raw_year) in allowed:
            year = int(raw_year)
        else:
            return f"Choose a year from {allowed[0]} to {allowed[-1]}."
    cycle.direction = request.POST.get("direction", "").strip()[:4000]
    cycle.season = season
    cycle.season_year = year
    cycle.save(update_fields=["direction", "season", "season_year"])
    return None


def _stage_rail(cycle):
    current = cycle.stage_number
    rail = []
    for number, name, summary in STAGES:
        if number > BUILT_STAGES:
            state = "later"
        elif number < current:
            state = "done"
        elif number == current:
            state = "current"
        else:
            state = "waiting"
        rail.append({"number": number, "name": name, "summary": summary, "state": state})
    return rail


def _spend(cycle):
    spent = cycle.spend_usd
    limit = settings.FDW_CYCLE_LIMIT_USD
    percent = min(100, int(spent / limit * 100)) if limit else 0
    return {"spent": spent, "limit": limit, "percent": percent}


def _concept_cards(cycle):
    cards = []
    for concept in cycle.concepts.prefetch_related("reviews"):
        review = concept.latest_review
        scores = []
        if review:
            for key in REVIEW_CRITERIA:
                value = review.data.get("scores", {}).get(key)
                scores.append({
                    "key": key,
                    "label": CRITERION_LABELS[key],
                    "value": value,
                    "reason": review.data.get("reasons", {}).get(key, ""),
                })
        cards.append({
            "concept": concept,
            "data": concept.data,
            "review": review,
            "scores": scores,
            "blocking": review.data.get("blocking_issues", []) if review else [],
            "risks": review.data.get("risks", []) if review else [],
            "price_range": concept.price_range,
            "piece_count": len(concept.data.get("pieces", [])),
        })
    return cards


@login_required
def dashboard(request):
    cycles = Cycle.objects.all()
    rows = [{"cycle": c, "rail": _stage_rail(c), "spend": _spend(c)} for c in cycles]
    return render(request, "workflow/dashboard.html", {
        "rows": rows,
        "brand_brief": BrandBrief.current(),
        "dry_run": settings.FDW_DRY_RUN,
    })


@login_required
@require_POST
def create_cycle(request):
    name = request.POST.get("name", "").strip()
    if not name:
        messages.error(request, "Give the cycle a name, such as \"Winter 2026\".")
        return redirect("dashboard")
    cycle = Cycle.objects.create(name=name[:120])
    messages.success(request, f"Created {cycle.name}. Start with market research.")
    return redirect("cycle_detail", pk=cycle.pk)


@login_required
def brand_brief(request):
    brief = BrandBrief.current()
    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        if not text:
            messages.error(request, "The brand brief cannot be empty. Every prompt depends on it.")
        else:
            brief.text = text
            brief.save()
            messages.success(request, "Brand brief saved. It applies to every stage run from now on.")
            return redirect("brand_brief")
    return render(request, "workflow/brand_brief.html", {
        "brief": brief,
        "dry_run": settings.FDW_DRY_RUN,
    })


@login_required
def cycle_detail(request, pk):
    cycle = get_object_or_404(Cycle, pk=pk)
    raw_items = list(cycle.raw_items.all())
    signals = list(cycle.signals.all())
    intent_counts = Counter(s.intent for s in signals)
    product_counts = Counter(s.product_type for s in signals if s.product_type).most_common(6)
    brief = cycle.latest_brief
    findings = list(cycle.findings.all())
    cards = _concept_cards(cycle)
    pieces = [
        piece
        for concept in cycle.concepts.filter(status=Concept.Status.SELECTED)
        for piece in concept.pieces.all()
    ]
    context = {
        "cycle": cycle,
        "rail": _stage_rail(cycle),
        "spend": _spend(cycle),
        "raw_items": raw_items,
        "source_counts": Counter(i.get_source_display() for i in raw_items).most_common(),
        "sources": RawItem.Source.choices,
        "finding_groups": _finding_groups(findings),
        "dropped_findings": [f for f in findings if not f.verified],
        "bv": _brief_view(brief, findings),
        "seasons": Cycle.Season.choices,
        "season_years": _season_years(),
        "signals": signals,
        "intent_counts": {
            "purchase": intent_counts.get("purchase", 0),
            "interest": intent_counts.get("interest", 0),
            "trend": intent_counts.get("trend", 0),
        },
        "product_counts": product_counts,
        "brief": brief,
        "cards": cards,
        "criteria": [CRITERION_LABELS[k] for k in REVIEW_CRITERIA],
        "pieces": pieces,
        "approvals": cycle.approvals.all(),
        "model_calls": cycle.model_calls.all()[:60],
        "can_run_stage1": cycle.status in (Cycle.Status.NEW, Cycle.Status.BRIEF_REJECTED),
        "can_edit_items": cycle.status in (Cycle.Status.NEW, Cycle.Status.BRIEF_REJECTED),
        "brief_pending": bool(brief and brief.status == DemandBrief.Status.PENDING),
        "can_run_stage2": cycle.status == Cycle.Status.BRIEF_APPROVED,
        "concepts_pending": cycle.status == Cycle.Status.CONCEPTS_PENDING,
        "dry_run": settings.FDW_DRY_RUN,
    }
    return render(request, "workflow/cycle_detail.html", context)


@login_required
@require_POST
def add_items(request, pk):
    cycle = get_object_or_404(Cycle, pk=pk)
    if cycle.status not in (Cycle.Status.NEW, Cycle.Status.BRIEF_REJECTED):
        messages.error(request, "Inputs are locked once a research brief has been written from them.")
        return redirect("cycle_detail", pk=pk)
    source = request.POST.get("source", RawItem.Source.DM)
    if source not in RawItem.Source.values:
        source = RawItem.Source.DM
    lines = [line.strip() for line in request.POST.get("items", "").splitlines() if line.strip()]
    if not lines:
        messages.error(request, "Paste at least one message. Put each one on its own line.")
        return redirect("cycle_detail", pk=pk)
    RawItem.objects.bulk_create(RawItem(cycle=cycle, source=source, text=line[:4000]) for line in lines)
    messages.success(request, f"Added {len(lines)} item{'s' if len(lines) != 1 else ''}.")
    return redirect("cycle_detail", pk=pk)


@login_required
@require_POST
def delete_item(request, pk, item_pk):
    cycle = get_object_or_404(Cycle, pk=pk)
    if cycle.status in (Cycle.Status.NEW, Cycle.Status.BRIEF_REJECTED):
        RawItem.objects.filter(cycle=cycle, pk=item_pk).delete()
    else:
        messages.error(request, "Inputs are locked once a research brief has been written from them.")
    return redirect("cycle_detail", pk=pk)


@login_required
@require_POST
def save_direction(request, pk):
    cycle = get_object_or_404(Cycle, pk=pk)
    if cycle.status not in (Cycle.Status.NEW, Cycle.Status.BRIEF_REJECTED):
        messages.error(request, "The direction is locked once a research brief has been written from it.")
        return redirect("cycle_detail", pk=pk)
    error = _apply_research_inputs(request, cycle)
    if error:
        messages.error(request, error)
    else:
        messages.success(request, "Research inputs saved.")
    return redirect("cycle_detail", pk=pk)


@login_required
@require_POST
def run_stage1(request, pk):
    cycle = get_object_or_404(Cycle, pk=pk)
    # The research button sits in the same form as the inputs, so whatever is on
    # screen is what gets researched, saved or not.
    if "season" in request.POST and cycle.status in (Cycle.Status.NEW, Cycle.Status.BRIEF_REJECTED):
        error = _apply_research_inputs(request, cycle)
        if error:
            messages.error(request, error)
            return redirect("cycle_detail", pk=pk)
    try:
        stages.run_stage1(cycle)
        messages.success(request, "Research finished and brief written. Read it and decide below.")
    except StageError as exc:
        messages.error(request, str(exc))
    return redirect("cycle_detail", pk=pk)


@login_required
@require_POST
def decide_brief(request, pk):
    cycle = get_object_or_404(Cycle, pk=pk)
    brief = cycle.latest_brief
    if brief is None:
        messages.error(request, "There is no research brief to decide on yet.")
        return redirect("cycle_detail", pk=pk)
    try:
        stages.decide_brief(
            brief,
            request.POST.get("decision", ""),
            request.POST.get("notes", ""),
            request.user.get_username(),
        )
        if brief.status == DemandBrief.Status.APPROVED:
            messages.success(request, "Research brief approved. Stage 2 is unlocked.")
        else:
            messages.success(request, "Research brief rejected. Adjust the direction and run the research again.")
    except StageError as exc:
        messages.error(request, str(exc))
    return redirect("cycle_detail", pk=pk)


@login_required
@require_POST
def run_stage2(request, pk):
    cycle = get_object_or_404(Cycle, pk=pk)
    try:
        stages.run_stage2(cycle)
        messages.success(request, "Three new collections designed and reviewed. Pick one or two below.")
    except StageError as exc:
        messages.error(request, str(exc))
    return redirect("cycle_detail", pk=pk)


@login_required
@require_POST
def select_concepts(request, pk):
    cycle = get_object_or_404(Cycle, pk=pk)
    try:
        pieces = stages.select_concepts(
            cycle,
            request.POST.getlist("concepts"),
            request.POST.get("notes", ""),
            request.user.get_username(),
        )
        messages.success(request, f"Collection selected. {len(pieces)} piece specifications are now approved.")
    except StageError as exc:
        messages.error(request, str(exc))
    return redirect("cycle_detail", pk=pk)
