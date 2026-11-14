"""The reviewer check: does the stage 4 image reviewer judge images the way the founder does?

Stage 4 trusts a Claude reviewer to fail any image that does not match the
piece before the founder sees it. This check runs that same reviewer, with
the same prompt (visuals.review_prompt) and model, over images the founder
has already scored in the image test, and compares its verdicts with the
founder's.

The founder "would accept" an image when every one of their four scores is at
least FDW_REVIEWER_ACCEPT_SCORE. The number that matters most is how many of
the images the founder would reject the reviewer also failed: a missed bad
image reaches buyers. Failing a good image only costs a redraw.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from . import schemas, visuals
from .bakeoff import BakeoffError, check_spend
from .llm import StageError, cost_of, get_client, parse_json
from .models import BakeoffImage, ReviewerResult, ReviewerRun, ShotAttempt

STALE_AFTER = timedelta(minutes=10)
# Set aside per review when estimating the cost of a run.
ESTIMATE_PER_IMAGE = Decimal("0.05")


def founder_accepts(image: BakeoffImage) -> bool:
    scores = [getattr(image, f) for f in BakeoffImage.SCORE_FIELDS]
    return min(scores) >= settings.FDW_REVIEWER_ACCEPT_SCORE


def scored_images():
    images = BakeoffImage.objects.filter(status=BakeoffImage.Status.DONE).select_related("piece")
    return [i for i in images if i.is_scored and i.file and i.piece.spec]


def marked_attempts():
    """Stage 4 images the founder has marked as ones they would accept or reject."""
    return list(
        ShotAttempt.objects.exclude(founder_verdict="").exclude(file="").filter(review__isnull=False)
        .select_related("visuals", "visuals__piece")
    )


def latest_run():
    return ReviewerRun.objects.first()


def total_cost() -> Decimal:
    return ReviewerResult.objects.aggregate(total=Sum("cost_usd"))["total"] or Decimal("0")


def start_run() -> ReviewerRun:
    images = scored_images()
    attempts = marked_attempts()
    if not images and not attempts:
        raise BakeoffError("Mark some stage 4 images below, or score images in the image test, first: the check "
                           "compares the reviewer with your own calls.")
    check_spend()
    with transaction.atomic():
        run = ReviewerRun.objects.create(
            model=settings.FDW_MODEL_WRITER, version=visuals.reviewer_version(),
            accept_score=settings.FDW_REVIEWER_ACCEPT_SCORE,
        )
        ReviewerResult.objects.bulk_create(
            [ReviewerResult(run=run, image=image, founder_accepts=founder_accepts(image)) for image in images]
            + [ReviewerResult(run=run, attempt=a, founder_accepts=a.founder_verdict == "accept") for a in attempts]
        )
    return run


def _reference(image: BakeoffImage):
    """The image model's own front packshot, which it was given as reference for shots C and D."""
    if image.shot == "A":
        return None
    return image.piece.images.filter(model=image.model, shot="A", status=BakeoffImage.Status.DONE).first()


def _claim(run: ReviewerRun):
    cutoff = timezone.now() - STALE_AFTER
    run.results.filter(status=ReviewerResult.Status.RUNNING, claimed_at__lt=cutoff).update(
        status=ReviewerResult.Status.WAITING, claimed_at=None)
    with transaction.atomic():
        for result in run.results.filter(status=ReviewerResult.Status.WAITING).order_by("id"):
            if ReviewerResult.objects.filter(pk=result.pk, status=ReviewerResult.Status.WAITING).update(
                    status=ReviewerResult.Status.RUNNING, claimed_at=timezone.now()):
                result.refresh_from_db()
                return result
    return None


def review_next(run: ReviewerRun, client=None):
    """Review one waiting image. Returns the result, or None when every image is done."""
    check_spend()
    result = _claim(run)
    if result is None:
        return None
    client = client or get_client()
    if result.attempt_id:
        # A stage 4 image: the same inputs stage 4's reviewer had, judged fresh.
        attempt = result.attempt
        piece_set = attempt.visuals
        refs = sorted(ShotAttempt.objects.filter(pk__in=attempt.references), key=lambda a: a.shot)
        prompt = visuals.review_prompt(piece_set.piece.spec, piece_set.accepted_changes + piece_set.assumptions,
                                       visuals.checklist_for(piece_set, attempt.shot), attempt.shot,
                                       [r.shot for r in refs])
        sources = [attempt] + refs
    else:
        image = result.image
        piece = image.piece
        reference = _reference(image)
        spec = {"name": piece.name, "description": piece.spec.get("description", ""),
                "founder_notes": piece.notes.strip() or "None."}
        # A real, finished piece: judged as one, not as a concept.
        prompt = visuals.review_prompt(spec, [], piece.spec.get("checklist", []), image.shot,
                                       [reference.shot] if reference else [], mode="real")
        sources = [image] + ([reference] if reference else [])
    images = []
    if not getattr(client, "dry_run", False):
        images = [visuals.review_payload(item.file) for item in sources if visuals._is_raster(item.file)]
    model = run.model
    cost_of(model, 0, 0)
    cost = Decimal("0")
    review = None
    attempt_prompt = prompt
    try:
        for _ in range(2):
            answer = client.complete("reviewer_check", attempt_prompt, model, settings.FDW_MAX_OUTPUT_TOKENS,
                                     images=images)
            if not getattr(client, "dry_run", False):
                cost += cost_of(model, answer.input_tokens, answer.output_tokens)
            try:
                review = schemas.validate_image_review(parse_json(answer.text))
                break
            except schemas.SchemaError as exc:
                attempt_prompt = (prompt + "\n\nYour previous answer could not be used. Problems found:\n- "
                                  + "\n- ".join(exc.problems[:10]) + "\n\nReturn the complete corrected JSON and nothing else.")
    except StageError as exc:
        ReviewerResult.objects.filter(pk=result.pk).update(
            status=ReviewerResult.Status.WAITING, claimed_at=None, error=str(exc)[:2000],
            cost_usd=result.cost_usd + cost)
        raise BakeoffError(str(exc)) from exc
    result.cost_usd += cost
    result.claimed_at = None
    if review is None:
        result.status = ReviewerResult.Status.FAILED
        result.error = "The reviewer's answer could not be used twice."
    else:
        result.status = ReviewerResult.Status.DONE
        result.review = review
        result.verdict = review["verdict"]
        result.error = ""
    result.save()
    return result


def progress(run: ReviewerRun) -> dict:
    results = list(run.results.all())
    done = sum(1 for r in results if r.status == ReviewerResult.Status.DONE)
    failed = sum(1 for r in results if r.status == ReviewerResult.Status.FAILED)
    return {"total": len(results), "done": done, "failed": failed, "waiting": len(results) - done - failed}


def summary(run: ReviewerRun) -> dict:
    """How far the reviewer agrees with the founder, and whether that is enough to trust it."""
    results = [r for r in run.results.select_related("image", "image__piece", "attempt", "attempt__visuals__piece")
               if r.verdict]
    rejected = [r for r in results if not r.founder_accepts]
    accepted = [r for r in results if r.founder_accepts]
    caught = [r for r in rejected if r.verdict == "fail"]
    missed = [r for r in rejected if r.verdict == "pass"]
    extra_fails = [r for r in accepted if r.verdict == "fail"]
    catch_rate = (Decimal(len(caught)) / len(rejected)) if rejected else None
    minimum = settings.FDW_REVIEWER_MIN_REJECTED
    target = settings.FDW_REVIEWER_TARGET_CATCH
    if len(rejected) < minimum:
        state, reason = "not_enough", (
            f"Only {len(rejected)} of the images you scored are ones you would reject. The check needs at least "
            f"{minimum} to say anything firm. Add pieces to the image test, ideally ones the models got wrong."
        )
    elif catch_rate >= target:
        state, reason = "ready", (
            f"The reviewer failed {len(caught)} of the {len(rejected)} images you would reject. "
            "That meets the bar, so stage 4 can rely on it."
        )
    else:
        state, reason = "not_ready", (
            f"The reviewer passed {len(missed)} of the {len(rejected)} images you would reject. Below "
            f"{int(target * 100)}% caught, flawed images would reach you, so its prompt needs tightening "
            "before stage 4 can rely on it."
        )
    return {
        "reviewed": len(results),
        "rejected": len(rejected),
        "accepted": len(accepted),
        "caught": len(caught),
        "passed_good": len(accepted) - len(extra_fails),
        "missed": missed,
        "extra_fails": extra_fails,
        "catch_percent": None if catch_rate is None else int(catch_rate * 100),
        "pass_percent": int(Decimal(len(accepted) - len(extra_fails)) / len(accepted) * 100) if accepted else None,
        "state": state,
        "reason": reason,
        "current": run.version == visuals.reviewer_version(),
        "target_percent": int(target * 100),
        "minimum": minimum,
    }


def status_line():
    """One line for the stage 4 section of a cycle: has the reviewer been checked?"""
    run = latest_run()
    if run is None or progress(run)["waiting"]:
        return {"state": "unchecked", "text": "The image reviewer has not yet been checked against your own scores."}
    info = summary(run)
    if not info["current"]:
        return {"state": "stale", "text": "The image reviewer changed since it was last checked against your scores."}
    return {"state": info["state"], "text": info["reason"]}
