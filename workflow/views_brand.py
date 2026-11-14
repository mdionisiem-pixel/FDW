"""Brand brief page and the brand analyst interview."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import analyst
import difflib

from .models import BrandBrief, BrandBriefVersion, BrandInterview, InterviewTurn, compose_prompt_text
from .prompts import BRAND_AREAS

COVERAGE_WORDS = {0: "not yet", 1: "thin", 2: "clear", 3: "sharp"}


@login_required
def brand_brief(request):
    brief = BrandBrief.current()
    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        if not text:
            messages.error(request, "The brand brief cannot be empty. Every prompt depends on it.")
        else:
            brief.snapshot(BrandBriefVersion.Source.BEFORE, "Before an edit by hand")
            brief.vision = request.POST.get("vision", "").strip()
            brief.mission = request.POST.get("mission", "").strip()
            brief.goals = analyst.goals_from_form(request.POST)
            brief.text = text
            brief.save()
            brief.snapshot(BrandBriefVersion.Source.HAND, "Edited by hand")
            messages.success(request, "Brand brief saved. It applies to every stage run from now on.")
            return redirect("brand_brief")
    return render(request, "workflow/brand_brief.html", {
        "brief": brief,
        "goal_groups": [
            {"title": "Short-term goals", "horizon": "the next 12 months", "goals": brief.short_goals},
            {"title": "Long-term goals", "horizon": "3 to 5 years", "goals": brief.long_goals},
        ],
        "interview": analyst.current_interview(),
        "open_points": analyst.open_points(brief),
        "reworkable": analyst.reworkable_lines(brief),
        "max_choices": analyst.MAX_CHOICES,
        "versions": BrandBriefVersion.objects.all()[:12],
        "analyst_model": settings.FDW_MODEL_ANALYST,
        "dry_run": settings.FDW_DRY_RUN,
    })


@login_required
@require_POST
def suggest_options(request):
    try:
        analyst.suggest_options(request.POST.get("line", ""))
    except analyst.AnalystError as exc:
        messages.error(request, str(exc))
    return redirect(reverse("brand_brief") + "#open-points")


@login_required
@require_POST
def settle_point(request):
    """The founder's own wording wins over picked options, since it is usually an edited combination of them."""
    line = request.POST.get("line", "")
    chosen = request.POST.getlist("choice")
    if len(chosen) > analyst.MAX_CHOICES:
        messages.error(request, f"Choose at most {analyst.MAX_CHOICES} options.")
        return redirect(reverse("brand_brief") + "#open-points")
    text = request.POST.get("own", "").strip() or analyst.combine_options(line, chosen)
    try:
        analyst.settle_point(line, text)
    except analyst.AnalystError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Settled. The new line applies to every stage run from now on.")
    return redirect(reverse("brand_brief") + "#open-points")


@login_required
@require_POST
def stop_rework(request):
    analyst.stop_rework(request.POST.get("line", ""))
    return redirect(reverse("brand_brief") + "#open-points")


@login_required
@require_POST
def restore_version(request, pk):
    version = get_object_or_404(BrandBriefVersion, pk=pk)
    brief = BrandBrief.current()
    brief.snapshot(BrandBriefVersion.Source.BEFORE, "Before restoring an earlier version")
    brief.vision, brief.mission, brief.goals, brief.text = version.vision, version.mission, version.goals, version.text
    brief.save()
    brief.snapshot(BrandBriefVersion.Source.HAND, f"Restored the version of {version.created_at:%d %b %Y, %H:%M}")
    messages.success(request, f"Restored the version of {version.created_at:%d %b %Y, %H:%M}.")
    return redirect("brand_brief")


def line_changes(before: str, after: str, context: int = 1) -> list:
    """Line-by-line differences, computed by the app rather than reported by the model.

    Returns rows of {"kind": "same" | "removed" | "added" | "gap", "text": ...}, with
    ``context`` unchanged lines around each change and a gap marker between groups."""
    a = [l.rstrip() for l in before.splitlines()]
    b = [l.rstrip() for l in after.splitlines()]
    rows = []
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context):
        if rows:
            rows.append({"kind": "gap", "text": ""})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows += [{"kind": "same", "text": l} for l in a[i1:i2]]
            else:
                rows += [{"kind": "removed", "text": l} for l in a[i1:i2] if l.strip()]
                rows += [{"kind": "added", "text": l} for l in b[j1:j2] if l.strip()]
    return rows


def _interview_context(interview):
    turns = list(interview.turns.all())
    current = turns[-1] if turns and turns[-1].role == InterviewTurn.Role.ANALYST else None
    coverage = [
        {"key": key, "label": label, "level": interview.coverage.get(key, 0),
         "word": COVERAGE_WORDS[interview.coverage.get(key, 0)]}
        for key, label in BRAND_AREAS.items()
    ]
    covered = sum(1 for c in coverage if c["level"] >= 2)
    draft = interview.draft or {}
    brief = BrandBrief.current()
    changes = []
    if draft:
        changes = line_changes(brief.prompt_text, compose_prompt_text(
            draft.get("vision", ""), draft.get("mission", ""), draft.get("goals", []), draft.get("brief", "")))
    return {
        "interview": interview,
        "turns": turns[:-1] if current else turns,
        "current": current,
        # The analyst said the picture is complete instead of asking another question.
        "finished": current is not None and not current.text,
        "coverage": coverage,
        "covered": covered,
        "ready": bool(interview.coverage.get("ready")) or covered == len(coverage),
        "answered": sum(1 for t in turns if t.role == InterviewTurn.Role.FOUNDER),
        "current_area_label": BRAND_AREAS.get(current.area, current.area) if current else "",
        "draft": draft,
        "draft_short_goals": [g for g in draft.get("goals", []) if g.get("horizon") != "long"],
        "draft_long_goals": [g for g in draft.get("goals", []) if g.get("horizon") == "long"],
        "brief": brief,
        "line_changes": changes,
        "added_count": sum(1 for r in changes if r["kind"] == "added"),
        "removed_count": sum(1 for r in changes if r["kind"] == "removed"),
        "limit": settings.FDW_INTERVIEW_LIMIT_USD,
        "analyst_model": interview.model,
    }


@login_required
def interview(request):
    current = analyst.current_interview()
    if current is None:
        return redirect("brand_brief")
    return render(request, "workflow/brand_interview.html", _interview_context(current))


@login_required
@require_POST
def interview_start(request):
    try:
        analyst.start()
    except analyst.AnalystError as exc:
        messages.error(request, str(exc))
        return redirect("brand_brief")
    return redirect("brand_interview")


@login_required
@require_POST
def interview_answer(request, pk):
    """Called by the page with fetch; the page reloads to show the next question."""
    current = get_object_or_404(BrandInterview, pk=pk)
    try:
        turn = analyst.answer(current, request.POST.get("answer", ""))
    except analyst.AnalystError as exc:
        # saved: the answer was kept and only the next question failed, so the page reloads to offer a retry.
        last = current.turns.last()
        saved = last is not None and last.role == InterviewTurn.Role.FOUNDER
        return JsonResponse({"error": str(exc), "saved": saved}, status=409)
    return JsonResponse({"question": turn.text, "area": turn.area})


@login_required
@require_POST
def interview_retry(request, pk):
    current = get_object_or_404(BrandInterview, pk=pk)
    try:
        analyst.retry_question(current)
    except analyst.AnalystError as exc:
        messages.error(request, str(exc))
    return redirect("brand_interview")


@login_required
@require_POST
def interview_draft(request, pk):
    current = get_object_or_404(BrandInterview, pk=pk)
    try:
        analyst.draft(current)
    except analyst.AnalystError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "The analyst's draft is ready. Edit anything, then approve it.")
    return redirect("brand_interview")


@login_required
@require_POST
def interview_resume(request, pk):
    current = get_object_or_404(BrandInterview, pk=pk, status=BrandInterview.Status.DRAFTED)
    current.status = BrandInterview.Status.ASKING
    current.save(update_fields=["status", "updated_at"])
    return redirect("brand_interview")


@login_required
@require_POST
def interview_approve(request, pk):
    current = get_object_or_404(BrandInterview, pk=pk)
    try:
        analyst.approve(current, request.POST.get("vision", ""), request.POST.get("mission", ""),
                        analyst.goals_from_form(request.POST), request.POST.get("text", ""))
    except analyst.AnalystError as exc:
        messages.error(request, str(exc))
        return redirect("brand_interview")
    messages.success(request, "Approved. The new vision, mission, goals and brief apply to every stage run from now "
                              "on; the previous version is kept below.")
    return redirect("brand_brief")


@login_required
@require_POST
def interview_close(request, pk):
    current = get_object_or_404(BrandInterview, pk=pk)
    analyst.close(current)
    messages.success(request, "Interview closed. The brand brief is unchanged.")
    return redirect("brand_brief")
