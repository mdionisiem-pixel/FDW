"""Brief test page under Tools: run stage 2 twice, judge blind, then see which brief made which."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import brief_test
from .llm import StageError
from .models import BrandBriefVersion, BriefTest, Cycle


@login_required
def page(request, pk=None):
    test = get_object_or_404(BriefTest, pk=pk) if pk else BriefTest.objects.first()
    context = {
        "test": test,
        "tests": BriefTest.objects.all()[:10],
        "cycles": brief_test.eligible_cycles(),
        "versions": BrandBriefVersion.objects.all()[:20],
        "default_version": brief_test.default_version(),
        "estimate": brief_test.run_estimate() * 2,
        "limit": settings.FDW_BRIEF_TEST_LIMIT_USD,
        "questions": brief_test.QUESTIONS,
        "answers": brief_test.ANSWERS,
        "dry_run": settings.FDW_DRY_RUN,
        "can_finish": bool(test) and brief_test.can_finish(test),
        "stale_minutes": settings.FDW_BRIEF_TEST_STALE_MINUTES,
    }
    if test and test.status in (BriefTest.Status.DONE, BriefTest.Status.JUDGED, BriefTest.Status.FAILED):
        context["sets"] = brief_test.sets(test)
    if test and test.status == BriefTest.Status.JUDGED:
        context["outcome"] = brief_test.outcome(test)
    return render(request, "workflow/brief_test.html", context)


@login_required
@require_POST
def start(request):
    if request.POST.get("confirm") != "1":
        # A new test spends real money, so it needs an explicit tick, never a stray click.
        messages.error(request, "Tick the box to confirm you want to start a new paid test.")
        return redirect("bakeoff_brief_test")
    cycle = get_object_or_404(Cycle, pk=request.POST.get("cycle"), is_test=False)
    version = BrandBriefVersion.objects.filter(pk=request.POST.get("version")).first()
    try:
        test = brief_test.create(cycle, version)
        brief_test.run(test)
    except (brief_test.BriefTestError, StageError) as exc:
        messages.error(request, str(exc))
        return redirect("bakeoff_brief_test")
    if test.status == BriefTest.Status.FAILED:
        messages.error(request, "A run did not finish. " + test.error)
    return redirect("bakeoff_brief_test_detail", pk=test.pk)


@login_required
@require_POST
def finish(request, pk):
    test = get_object_or_404(BriefTest, pk=pk)
    try:
        brief_test.finish(test)
    except (brief_test.BriefTestError, StageError) as exc:
        messages.error(request, str(exc))
    else:
        if test.status == BriefTest.Status.FAILED:
            messages.error(request, "A run still did not finish. " + test.error)
    return redirect("bakeoff_brief_test_detail", pk=test.pk)


@login_required
@require_POST
def judge(request, pk):
    test = get_object_or_404(BriefTest, pk=pk)
    answers = {key: request.POST.get(key, "") for key, _ in brief_test.QUESTIONS}
    try:
        brief_test.judge(test, answers, request.POST.get("notes", ""))
    except brief_test.BriefTestError as exc:
        messages.error(request, str(exc))
    return redirect("bakeoff_brief_test_detail", pk=test.pk)


@login_required
@require_POST
def discard(request, pk):
    test = get_object_or_404(BriefTest, pk=pk)
    if test.status == BriefTest.Status.RUNNING and not brief_test.is_stale(test):
        messages.error(request, "This test is still running. Discard it once it has finished or stopped.")
        return redirect("bakeoff_brief_test_detail", pk=test.pk)
    if test.status == BriefTest.Status.JUDGED and request.POST.get("confirm") != "1":
        # A judged test holds your verdict; losing it by a stray click cannot be undone.
        messages.error(request, "Tick the box to confirm you want to delete this judged test and its result.")
        return redirect("bakeoff_brief_test_detail", pk=test.pk)
    for cycle in (test.cycle_a, test.cycle_b):
        if cycle is not None and cycle.is_test:
            cycle.delete()  # the model calls go with it; spend stays in the totals already shown
    test.delete()
    messages.success(request, "Brief test discarded.")
    return redirect("bakeoff_brief_test")
