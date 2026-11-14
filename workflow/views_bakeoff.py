"""Screens for the image model bake-off."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import bakeoff, reviewer_check
from .models import BakeoffImage, BakeoffPiece

SCORE_LABELS = [
    ("matches_piece", "Matches the real piece"),
    ("material_realism", "Stitches and material look real"),
    ("colour_accuracy", "Colours are right"),
    ("photo_realism", "Looks like a real photo"),
]
PHOTO_FIELDS = ("photo_front", "photo_back", "photo_detail", "photo_worn")


def _progress(piece):
    images = list(piece.images.all())
    done = sum(1 for i in images if i.status == BakeoffImage.Status.DONE)
    failed = sum(1 for i in images if i.status == BakeoffImage.Status.FAILED)
    scored = sum(1 for i in images if i.is_scored)
    return {"total": len(images), "done": done, "failed": failed,
            "waiting": len(images) - done - failed, "scored": scored}


def _base_context():
    return {
        "dry_run": settings.FDW_DRY_RUN,
        "spent": bakeoff.total_spend(),
        "limit": settings.FDW_BAKEOFF_LIMIT_USD,
    }


@login_required
def home(request):
    pieces = [{"piece": p, "progress": _progress(p)} for p in BakeoffPiece.objects.all()]
    return render(request, "workflow/bakeoff_home.html", {
        **_base_context(),
        "pieces": pieces,
        "available": request.session.pop("bakeoff_models", None),
        "default_models": settings.FDW_BAKEOFF_MODELS,
    })


@login_required
@require_POST
def create_piece(request):
    name = request.POST.get("name", "").strip()
    if not name:
        messages.error(request, "Give the piece a name.")
        return redirect("bakeoff_home")
    if "photo_front" not in request.FILES:
        messages.error(request, "A front photo is required. The other three are optional but make the test fairer.")
        return redirect("bakeoff_home")
    piece = BakeoffPiece(name=name[:120], notes=request.POST.get("notes", "").strip()[:2000])
    try:
        for field in PHOTO_FIELDS:
            upload = request.FILES.get(field)
            if upload:
                setattr(piece, field, bakeoff.check_photo(upload))
    except bakeoff.BakeoffError as exc:
        messages.error(request, str(exc))
        return redirect("bakeoff_home")
    piece.save()
    messages.success(request, f"Added {piece.name}. Next, have Claude describe it and write the prompts.")
    return redirect("bakeoff_piece", pk=piece.pk)


@login_required
@require_POST
def check_models(request):
    try:
        request.session["bakeoff_models"] = bakeoff.get_image_client().list_models()
    except bakeoff.BakeoffError as exc:
        messages.error(request, str(exc))
    return redirect("bakeoff_home")


@login_required
def piece_detail(request, pk):
    piece = get_object_or_404(BakeoffPiece, pk=pk)
    images = list(piece.images.all())
    shots = []
    for shot, label in BakeoffImage.Shot.choices:
        shot_images = [i for i in images if i.shot == shot]
        if not shot_images:
            continue
        photo_field = bakeoff.SHOT_PHOTO[shot]
        shots.append({
            "code": shot, "label": label,
            "photo_field": photo_field if getattr(piece, photo_field) else None,
            "images": sorted(shot_images, key=lambda i: i.code),
        })
    queued_models = sorted({i.model for i in images})
    return render(request, "workflow/bakeoff_piece.html", {
        **_base_context(),
        "piece": piece,
        "progress": _progress(piece),
        "shots": shots,
        "score_labels": SCORE_LABELS,
        "default_models": settings.FDW_BAKEOFF_MODELS,
        "queued_models": queued_models,
    })


@login_required
@require_POST
def describe(request, pk):
    piece = get_object_or_404(BakeoffPiece, pk=pk)
    try:
        bakeoff.describe_piece(piece)
        messages.success(request, "Description and prompts written. Check them, then choose the models to compare.")
    except bakeoff.BakeoffError as exc:
        messages.error(request, str(exc))
    return redirect("bakeoff_piece", pk=pk)


@login_required
@require_POST
def queue(request, pk):
    piece = get_object_or_404(BakeoffPiece, pk=pk)
    wanted = request.POST.getlist("models")
    extra = request.POST.get("extra_model", "").strip()
    if extra:
        wanted.append(extra)
    try:
        added = bakeoff.queue_images(piece, wanted)
        if added:
            messages.success(request, f"Queued {added} images. Press Make images to start.")
        else:
            messages.success(request, "Those models are already queued for this piece.")
    except bakeoff.BakeoffError as exc:
        messages.error(request, str(exc))
    return redirect("bakeoff_piece", pk=pk)


@login_required
@require_POST
def make_next(request, pk):
    """Make one waiting image. Called repeatedly by the page until nothing is left."""
    piece = get_object_or_404(BakeoffPiece, pk=pk)
    try:
        image = bakeoff.make_next_image(piece)
    except bakeoff.BakeoffError as exc:
        return JsonResponse({"error": str(exc), "progress": _progress(piece)}, status=409)
    progress = _progress(piece)
    if image is None:
        return JsonResponse({"finished": True, "progress": progress})
    return JsonResponse({
        "finished": progress["waiting"] == 0,
        "progress": progress,
        "last": {"shot": image.get_shot_display(), "status": image.status},
    })


@login_required
@require_POST
def retry(request, pk):
    piece = get_object_or_404(BakeoffPiece, pk=pk)
    count = bakeoff.retry_failed(piece)
    messages.success(request, f"{count} failed image{'s' if count != 1 else ''} queued again.")
    return redirect("bakeoff_piece", pk=pk)


@login_required
@require_POST
def score(request, pk, image_pk):
    image = get_object_or_404(BakeoffImage, pk=image_pk, piece_id=pk)
    wants_json = request.headers.get("X-Requested-With") == "fetch"
    if image.status != BakeoffImage.Status.DONE:
        error = "Only a made image can be scored."
    else:
        try:
            bakeoff.save_scores(image, request.POST)
            error = None
        except bakeoff.BakeoffError as exc:
            error = str(exc)
    if wants_json:
        return JsonResponse({"ok": error is None, "error": error, "progress": _progress(image.piece)},
                            status=200 if error is None else 400)
    if error:
        messages.error(request, error)
    return redirect(f"/bakeoff/pieces/{pk}/#image-{image.pk}")


@login_required
def results(request):
    rows = bakeoff.results()
    total = BakeoffImage.objects.filter(status=BakeoffImage.Status.DONE).count()
    scored = sum(1 for i in BakeoffImage.objects.filter(status=BakeoffImage.Status.DONE) if i.is_scored)
    return render(request, "workflow/bakeoff_results.html", {
        **_base_context(),
        "rows": rows,
        "score_labels": SCORE_LABELS,
        "total": total,
        "scored": scored,
        "pieces": BakeoffPiece.objects.prefetch_related("images"),
    })


def _file_response(field_file):
    if not field_file:
        raise Http404
    name = field_file.name.lower()
    response = FileResponse(field_file.open("rb"))
    if name.endswith(".svg"):
        response["Content-Type"] = "image/svg+xml"
    # Images are shown, never run: block scripts inside any file served here.
    response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=3600"
    return response


@login_required
def photo(request, pk, field):
    if field not in PHOTO_FIELDS:
        raise Http404
    piece = get_object_or_404(BakeoffPiece, pk=pk)
    return _file_response(getattr(piece, field))


@login_required
def image_file(request, image_pk):
    image = get_object_or_404(BakeoffImage, pk=image_pk)
    return _file_response(image.file)


# --- Reviewer check ------------------------------------------------------------------


@login_required
def reviewer(request):
    run = reviewer_check.latest_run()
    scored = reviewer_check.scored_images()
    context = {
        **_base_context(),
        "run": run,
        "scored_count": len(scored),
        "would_reject": sum(1 for i in scored if not reviewer_check.founder_accepts(i)),
        "estimate": reviewer_check.ESTIMATE_PER_IMAGE * len(scored),
        "accept_score": settings.FDW_REVIEWER_ACCEPT_SCORE,
        "score_labels": SCORE_LABELS,
    }
    if run:
        context["progress"] = reviewer_check.progress(run)
        context["info"] = reviewer_check.summary(run)
        results = list(run.results.select_related("image", "image__piece"))
        order = {False: 0, True: 1, None: 2}
        context["results"] = sorted(results, key=lambda r: (order[r.agrees], r.founder_accepts, r.pk))
    return render(request, "workflow/bakeoff_reviewer.html", context)


@login_required
@require_POST
def reviewer_start(request):
    try:
        run = reviewer_check.start_run()
        messages.success(request, f"Ready to review {run.results.count()} scored images. Press Run the check.")
    except bakeoff.BakeoffError as exc:
        messages.error(request, str(exc))
    return redirect("bakeoff_reviewer")


@login_required
@require_POST
def reviewer_next(request):
    """Review one image. Called repeatedly by the page until every image is done."""
    run = reviewer_check.latest_run()
    if run is None:
        return JsonResponse({"error": "Start a check first."}, status=409)
    try:
        result = reviewer_check.review_next(run)
    except bakeoff.BakeoffError as exc:
        return JsonResponse({"error": str(exc), "progress": reviewer_check.progress(run)}, status=409)
    progress = reviewer_check.progress(run)
    return JsonResponse({"finished": result is None or progress["waiting"] == 0, "progress": progress})
