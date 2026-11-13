"""Stage 4: visual prototypes.

For every piece that passed gate 3, an art director step writes five shot
prompts and a checklist, the image model chosen in the bake-off draws each
shot, and a separate reviewer step looks at every image against the piece
specification before the founder sees it. A failed image is drawn again with
the reviewer's correction note, up to FDW_MAX_SHOT_ATTEMPTS times. Gate 4 is
the founder's decision per piece.

The work is split into small units (write one piece's prompts, or make and
review one image), and the page asks for one unit per request. Several
requests can run at once: each unit is claimed in the database before it
starts, so two workers never make the same image, and money is set aside for
every claimed unit before it starts, so parallel work cannot pass the
stage 4 spending limit.

Order inside a piece is fixed by REFERENCES: shot A first; B and C use A as
their reference; D uses A and B; E uses A and D. Only images that passed
review, or that the founder chose to keep, are ever used as references, so a
flawed image never shapes the shots after it.
"""
import base64
import io
import json
import time
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import prompts, schemas
from .bakeoff import BakeoffError, get_image_client, sniff
from .llm import SpendLimitError, StageError, get_client, run_json
from .models import (
    SHOTS,
    Approval,
    BrandBrief,
    Cycle,
    PieceVisuals,
    ShotAttempt,
    SourcingCheck,
)

SHOT_NAMES = dict(SHOTS)
SHOT_CODES = tuple(SHOT_NAMES)
REFERENCES = {"A": (), "B": ("A",), "C": ("A",), "D": ("A", "B"), "E": ("A", "D")}
CONCEPT_LABEL = "Concept render. The finished piece may differ slightly."
# A unit whose worker vanished (closed tab, server restart) is offered again after this.
STALE_AFTER = timedelta(minutes=15)
# Reference images are sent to the image model as JPEG at this size, which
# keeps each request small without losing the detail the model copies.
REFERENCE_EDGE = 2048

DECISIONS = {
    "approve": "approved",
    "changes": "changes requested",
    "reject": "rejected",
    "continue": "kept the best front packshot",
}


class VisualsError(StageError):
    """Stage 4 could not do what was asked. The message is safe to show."""


# --- Starting the stage --------------------------------------------------------


def approved_sourcing(cycle: Cycle):
    return cycle.sourcing_checks.filter(status=SourcingCheck.Status.APPROVED).order_by("-decided_at", "-id").first()


def start_stage4(cycle: Cycle) -> int:
    """Create one set of shots for every piece the founder kept at gate 3."""
    if cycle.status != Cycle.Status.SOURCING_APPROVED:
        raise VisualsError("Visual prototypes start after the sourcing sheet is approved at gate 3.")
    check = approved_sourcing(cycle)
    results = [r for r in check.results.select_related("piece")] if check else []
    keep = [r for r in results if r.continues]
    if not keep:
        raise VisualsError("No piece continued past gate 3, so there is nothing to draw.")
    get_image_client()  # stops here, before anything changes, if no image key is set
    with transaction.atomic():
        for result in keep:
            PieceVisuals.objects.get_or_create(
                cycle=cycle, piece=result.piece,
                defaults={
                    "accepted_changes": [c for c in result.changes if str(c).strip()],
                    "waitlist_only": result.waitlist_only,
                },
            )
        cycle.status = Cycle.Status.VISUALS_IN_PROGRESS
        cycle.save(update_fields=["status"])
    return len(keep)


# --- Spend ------------------------------------------------------------------------


def unit_estimate(kind: str) -> Decimal:
    if kind == "prompts":
        return settings.FDW_VISUAL_CALL_ESTIMATE_USD
    return settings.FDW_IMAGE_COST_ESTIMATE_USD + settings.FDW_VISUAL_CALL_ESTIMATE_USD


def reserved(cycle: Cycle) -> Decimal:
    """Money set aside for units that are running now and have not yet recorded their cost."""
    running = ShotAttempt.objects.filter(visuals__cycle=cycle, status=ShotAttempt.Status.RUNNING).count()
    writing = PieceVisuals.objects.filter(
        cycle=cycle, status=PieceVisuals.Status.WRITING, claimed_at__isnull=False
    ).count()
    return running * unit_estimate("image") + writing * unit_estimate("prompts")


def estimate_for(piece_count: int) -> Decimal:
    """Expected stage 4 cost: prompts, then five shots at about 1.5 attempts each, every attempt reviewed."""
    per_piece = unit_estimate("prompts") + Decimal("7.5") * unit_estimate("image")
    return (per_piece * piece_count).quantize(Decimal("0.01"))


# --- Claiming one unit of work ---------------------------------------------------


def _release_stale(cycle: Cycle) -> None:
    cutoff = timezone.now() - STALE_AFTER
    ShotAttempt.objects.filter(
        visuals__cycle=cycle, status=ShotAttempt.Status.RUNNING, claimed_at__lt=cutoff
    ).update(status=ShotAttempt.Status.WAITING, claimed_at=None)
    PieceVisuals.objects.filter(
        cycle=cycle, status=PieceVisuals.Status.WRITING, claimed_at__lt=cutoff
    ).update(claimed_at=None)


def _claim(cycle: Cycle):
    """Claim the next unit, or return None. Raises SpendLimitError, claiming nothing, if it would not fit.

    The claim is the first write in the transaction, so on SQLite it takes the
    write lock and a second worker waits until this one has finished counting.
    """
    limit = settings.FDW_VISUALS_LIMIT_USD
    with transaction.atomic():
        now = timezone.now()
        job = None
        for visuals in PieceVisuals.objects.filter(
            cycle=cycle, status=PieceVisuals.Status.WRITING, claimed_at__isnull=True
        ).select_related("piece"):
            if PieceVisuals.objects.filter(
                pk=visuals.pk, status=PieceVisuals.Status.WRITING, claimed_at__isnull=True
            ).update(claimed_at=now):
                job = ("prompts", visuals)
                break
        if job is None:
            waiting = ShotAttempt.objects.filter(
                visuals__cycle=cycle, visuals__status=PieceVisuals.Status.MAKING,
                round=F("visuals__round"), status=ShotAttempt.Status.WAITING,
            ).order_by("visuals__piece__piece_id", "shot", "attempt")
            for attempt in waiting:
                if ShotAttempt.objects.filter(pk=attempt.pk, status=ShotAttempt.Status.WAITING).update(
                    status=ShotAttempt.Status.RUNNING, claimed_at=now
                ):
                    job = ("image", attempt)
                    break
        if job is None:
            return None
        committed = cycle.visuals_spend_usd + reserved(cycle)
        if committed > limit:
            # Raising here rolls the claim back, so the unit stays waiting.
            raise SpendLimitError(
                f"Visual prototypes have cost ${cycle.visuals_spend_usd:.2f} of their ${limit} limit, and the "
                f"next image would take it past the limit, so it was not made. Everything made so far is kept. "
                "Raise FDW_VISUALS_LIMIT_USD in .env to continue, or decide on the pieces that are ready."
            )
    kind, item = job
    if kind == "image":
        item.refresh_from_db()
    return job


def busy(cycle: Cycle) -> bool:
    return (
        ShotAttempt.objects.filter(visuals__cycle=cycle, status=ShotAttempt.Status.RUNNING).exists()
        or PieceVisuals.objects.filter(
            cycle=cycle, status=PieceVisuals.Status.WRITING, claimed_at__isnull=False
        ).exists()
    )


def work_next(cycle: Cycle, client=None, image_client=None) -> dict:
    """Do one unit of stage 4. Called repeatedly by the page, possibly several times at once.

    Returns {"state": "worked", "did": text}, {"state": "waiting"} when the
    only remaining work depends on units other workers are doing, or
    {"state": "finished"} when nothing is left to make.
    """
    cycle.refresh_from_db()
    if cycle.status != Cycle.Status.VISUALS_IN_PROGRESS:
        return {"state": "finished"}
    _release_stale(cycle)
    job = _claim(cycle)
    if job is None:
        return {"state": "waiting" if busy(cycle) else "finished"}
    kind, item = job
    if kind == "prompts":
        write_prompts(item, client)
        return {"state": "worked", "did": f"Wrote the shot prompts for {item.piece.name}."}
    make_attempt(item, client, image_client)
    item.refresh_from_db()
    outcome = "passed review" if item.status == ShotAttempt.Status.PASSED else "failed review"
    return {
        "state": "worked",
        "did": f"{item.visuals.piece.name}, shot {item.shot} ({SHOT_NAMES[item.shot].lower()}), "
               f"attempt {item.attempt}: {outcome}.",
    }


# --- Unit 1: the art director writes the prompts -----------------------------------


def _bullets(items, empty: str) -> str:
    items = [str(i).strip() for i in items if str(i).strip()]
    return "\n".join(f"- {i}" for i in items) if items else empty


def _collection_context(piece) -> str:
    data = piece.concept.data or {}
    context = {
        "collection": piece.concept.name,
        "theme": data.get("theme", ""),
        "target_customer": data.get("target_customer", ""),
        "signature_detail": data.get("signature_detail", ""),
        "palette": data.get("palette", []),
    }
    return json.dumps(context, ensure_ascii=False, indent=1)


def write_prompts(visuals: PieceVisuals, client=None) -> PieceVisuals:
    cycle = visuals.cycle
    piece = visuals.piece
    prompt = prompts.render(
        prompts.VISUAL_ART_DIRECTOR,
        brand_brief=BrandBrief.current().text,
        realism_standard=prompts.REALISM_STANDARD,
        piece_json=json.dumps(piece.spec, ensure_ascii=False, indent=1),
        accepted_changes=_bullets(visuals.accepted_changes, "None. Make the piece exactly as specified."),
        collection_context=_collection_context(piece),
        founder_notes=visuals.founder_notes.strip() or "None. This is the first round.",
    )
    try:
        data = run_json(
            cycle, "visual_art_direction", prompt, settings.FDW_MODEL_WRITER, settings.FDW_MAX_OUTPUT_TOKENS,
            schemas.validate_art_direction, client or get_client(),
        )
    except StageError as exc:
        PieceVisuals.objects.filter(pk=visuals.pk).update(claimed_at=None, error=str(exc)[:2000])
        raise
    with transaction.atomic():
        visuals.prompts = data["prompts"]
        visuals.checklist = data["reviewer_checklist"]
        visuals.missing_information = data["missing_information"]
        visuals.status = PieceVisuals.Status.MAKING
        visuals.claimed_at = None
        visuals.error = ""
        visuals.save()
        ShotAttempt.objects.get_or_create(
            visuals=visuals, round=visuals.round, shot="A", attempt=1,
            defaults={"prompt": visuals.prompts["A"], "model": settings.FDW_IMAGE_MODEL},
        )
    return visuals


# --- Unit 2: make one image, then review it ------------------------------------------


def _read(field_file) -> bytes:
    field_file.open("rb")
    try:
        return field_file.read()
    finally:
        field_file.close()


def _as_jpeg(data: bytes, edge: int, quality: int) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        image = opened.convert("RGB")
    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _is_raster(field_file) -> bool:
    return bool(field_file) and not field_file.name.lower().endswith(".svg")


def usable_attempt(attempts):
    """The image that stands for a shot: the one that passed review, or one the founder kept."""
    for attempt in attempts:
        if attempt.status == ShotAttempt.Status.PASSED:
            return attempt
    for attempt in attempts:
        if attempt.accepted:
            return attempt
    return None


def best_attempt(attempts):
    """The image to show for a shot: a usable one, or else the failed attempt with the fewest problems."""
    usable = usable_attempt(attempts)
    if usable:
        return usable
    made = [a for a in attempts if a.file and a.review]
    if not made:
        return None
    return max(made, key=lambda a: (-a.mismatches, a.matches, -len(a.review.get("realism_issues", [])), a.attempt))


def _by_shot(visuals: PieceVisuals) -> dict:
    grouped = {shot: [] for shot in SHOT_CODES}
    for attempt in visuals.current_attempts().order_by("shot", "attempt"):
        grouped[attempt.shot].append(attempt)
    return grouped


def _references(visuals: PieceVisuals, shot: str) -> list:
    grouped = _by_shot(visuals)
    refs = []
    for code in REFERENCES[shot]:
        usable = usable_attempt(grouped[code])
        if usable:
            refs.append(usable)
    return refs


def _counts_as_attempt(message: str) -> bool:
    """Whether an image error is about this prompt, so trying it forever would not help.

    Problems with the account or the connection (no credit, a bad key, a
    service outage) are not the prompt's fault and never use up an attempt.
    """
    text = message.lower()
    infrastructure = ("returned 401", "returned 402", "returned 403", "returned 408", "returned 429",
                      "returned 5", "could not reach", "certificate", "openrouter_api_key")
    return not any(marker in text for marker in infrastructure)


def make_attempt(attempt: ShotAttempt, client=None, image_client=None) -> ShotAttempt:
    visuals = attempt.visuals
    image_client = image_client or get_image_client()
    sample_images = getattr(image_client, "dry_run", False)

    if not attempt.file:
        refs = _references(visuals, attempt.shot)
        urls = []
        if not sample_images:
            for ref in refs:
                if _is_raster(ref.file):
                    jpeg = _as_jpeg(_read(ref.file), REFERENCE_EDGE, 92)
                    urls.append("data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"))
        started = time.monotonic()
        try:
            data, (media_type, ext), cost = image_client.generate(settings.FDW_IMAGE_MODEL, attempt.prompt, urls)
        except BakeoffError as exc:
            message = str(exc)[:2000]
            attempt.error = message
            attempt.claimed_at = None
            if _counts_as_attempt(message):
                attempt.error_count += 1
            if attempt.error_count >= ShotAttempt.MAX_ERRORS:
                attempt.status = ShotAttempt.Status.FAILED
                attempt.correction_note = ""
                attempt.save()
                advance(visuals)
            else:
                attempt.status = ShotAttempt.Status.WAITING
                attempt.save()
            raise
        attempt.file.save(f"{uuid.uuid4().hex}.{ext}", ContentFile(data), save=False)
        if sample_images:
            attempt.cost_usd = Decimal("0")
        elif cost is None:
            attempt.cost_usd = settings.FDW_IMAGE_COST_ESTIMATE_USD
            attempt.cost_estimated = True
        else:
            attempt.cost_usd = cost
        attempt.model = settings.FDW_IMAGE_MODEL
        attempt.references = [r.pk for r in refs]
        attempt.seconds = round(time.monotonic() - started, 1)
        attempt.error = ""
        attempt.save()

    try:
        review = review_attempt(attempt, client or get_client())
    except StageError as exc:
        # The image is kept; when the unit runs again, only the review is repeated.
        attempt.status = ShotAttempt.Status.WAITING
        attempt.claimed_at = None
        attempt.error = str(exc)[:2000]
        attempt.save(update_fields=["status", "claimed_at", "error"])
        raise
    attempt.review = review
    attempt.status = ShotAttempt.Status.PASSED if review["verdict"] == "pass" else ShotAttempt.Status.FAILED
    attempt.correction_note = review["correction_note"]
    attempt.claimed_at = None
    attempt.error = ""
    attempt.save()
    advance(visuals)
    return attempt


def review_attempt(attempt: ShotAttempt, client) -> dict:
    visuals = attempt.visuals
    refs = sorted(ShotAttempt.objects.filter(pk__in=attempt.references), key=lambda a: a.shot)
    images = []
    if not getattr(client, "dry_run", False) and _is_raster(attempt.file):
        edge = settings.FDW_REVIEW_IMAGE_EDGE
        for item in [attempt] + [r for r in refs if _is_raster(r.file)]:
            images.append(("image/jpeg", base64.b64encode(_as_jpeg(_read(item.file), edge, 88)).decode("ascii")))
    if refs:
        names = ", ".join(f"shot {r.shot} ({SHOT_NAMES[r.shot].lower()})" for r in refs)
        reference_note = (
            f"The {'next image is' if len(refs) == 1 else f'next {len(refs)} images are'} the approved "
            f"reference {'image' if len(refs) == 1 else 'images'} the image model was given, in this order: "
            f"{names}. The image under review must show the same piece."
        )
    else:
        reference_note = "This shot has no reference image; judge it against the specification alone."
    earlier = [
        a.correction_note for a in visuals.current_attempts().filter(
            shot=attempt.shot, attempt__lt=attempt.attempt, status=ShotAttempt.Status.FAILED
        ).order_by("attempt") if a.correction_note
    ]
    earlier_text = (
        "Earlier attempts at this shot failed review for the reasons below. Check specifically that each "
        "one is now fixed, as well as everything else:\n" + _bullets(earlier, "")
    ) if earlier else ""
    prompt = prompts.render(
        prompts.VISUAL_REVIEWER,
        reference_note=reference_note,
        earlier_corrections=earlier_text,
        realism_standard=prompts.REALISM_STANDARD,
        piece_json=json.dumps(visuals.piece.spec, ensure_ascii=False, indent=1),
        accepted_changes=_bullets(visuals.accepted_changes, "None."),
        reviewer_checklist=_bullets(visuals.checklist, "None."),
        shot=attempt.shot,
        shot_name=SHOT_NAMES[attempt.shot],
    )
    return run_json(
        visuals.cycle, "visual_review", prompt, settings.FDW_MODEL_WRITER, settings.FDW_MAX_OUTPUT_TOKENS,
        schemas.validate_image_review, client, images=images,
    )


def _retry_prompt(visuals: PieceVisuals, failed) -> str:
    notes = [a.correction_note for a in failed if a.correction_note]
    base = visuals.prompts[failed[0].shot]
    if not notes:
        return base
    return base + "\n\nCorrections from earlier attempts at this image, all of which must be met:\n" + _bullets(notes, "")


def shot_state(attempts) -> str:
    """passed, kept, working, could_not_meet, or not_started."""
    if any(a.status == ShotAttempt.Status.PASSED for a in attempts):
        return "passed"
    if any(a.accepted for a in attempts):
        return "kept"
    if not attempts:
        return "not_started"
    if any(a.status in (ShotAttempt.Status.WAITING, ShotAttempt.Status.RUNNING) for a in attempts):
        return "working"
    if len(attempts) >= settings.FDW_MAX_SHOT_ATTEMPTS:
        return "could_not_meet"
    return "working"


def advance(visuals: PieceVisuals) -> None:
    """After an attempt finishes: queue a retry or the shots it unlocks, and hand finished pieces to gate 4."""
    with transaction.atomic():
        visuals.refresh_from_db()
        if visuals.status != PieceVisuals.Status.MAKING:
            return
        grouped = _by_shot(visuals)
        maximum = settings.FDW_MAX_SHOT_ATTEMPTS
        for shot, attempts in grouped.items():
            if attempts and shot_state(attempts) == "working" and all(
                a.status == ShotAttempt.Status.FAILED for a in attempts
            ) and len(attempts) < maximum:
                ShotAttempt.objects.get_or_create(
                    visuals=visuals, round=visuals.round, shot=shot, attempt=len(attempts) + 1,
                    defaults={"prompt": _retry_prompt(visuals, attempts), "model": settings.FDW_IMAGE_MODEL},
                )
        grouped = _by_shot(visuals)
        states = {shot: shot_state(attempts) for shot, attempts in grouped.items()}
        resolved = {s for s, state in states.items() if state in ("passed", "kept", "could_not_meet")}
        if states["A"] in ("passed", "kept"):
            for shot in SHOT_CODES[1:]:
                if states[shot] == "not_started" and all(dep in resolved for dep in REFERENCES[shot]):
                    ShotAttempt.objects.get_or_create(
                        visuals=visuals, round=visuals.round, shot=shot, attempt=1,
                        defaults={"prompt": visuals.prompts[shot], "model": settings.FDW_IMAGE_MODEL},
                    )
            grouped = _by_shot(visuals)
            states = {shot: shot_state(attempts) for shot, attempts in grouped.items()}
            done = all(state in ("passed", "kept", "could_not_meet") for state in states.values())
        else:
            done = states["A"] == "could_not_meet"
        if done:
            visuals.status = PieceVisuals.Status.READY
            visuals.save(update_fields=["status"])


# --- Gate 4 ----------------------------------------------------------------------------


def decide(visuals: PieceVisuals, decision: str, notes: str = "", keep=(), user: str = "") -> PieceVisuals:
    """Gate 4 for one piece.

    approve: the piece's shots go forward. Shots that could not meet the
      standard go forward only if their key is in ``keep``; the rest are left out.
    changes: a new round starts from new prompts that follow the notes.
    reject: the piece leaves the collection.
    continue: shot A could not meet the standard; the founder keeps its best
      attempt and the other four shots are made from it.
    """
    if decision not in DECISIONS:
        raise VisualsError("Choose approve, request changes, or reject.")
    if visuals.status != PieceVisuals.Status.READY:
        raise VisualsError(f"{visuals.piece.name} is not awaiting a decision.")
    notes = notes.strip()
    grouped = _by_shot(visuals)
    states = {shot: shot_state(attempts) for shot, attempts in grouped.items()}
    a_stuck = states["A"] == "could_not_meet"
    if decision == "changes" and not notes:
        raise VisualsError("Say what should change, so the next round of prompts can follow it.")
    if decision == "approve" and a_stuck:
        raise VisualsError(
            "The front packshot did not meet the standard, so the other shots were not made. Keep its best "
            "attempt and continue, request changes, or reject the piece."
        )
    if decision == "continue" and not a_stuck:
        raise VisualsError("Continue is only for a front packshot that could not meet the standard.")
    keep = {str(k) for k in keep}
    cycle = visuals.cycle
    piece_label = f"{visuals.piece.piece_id} {visuals.piece.name}"
    with transaction.atomic():
        record = notes
        if decision == "approve":
            kept, left_out = [], []
            for shot, state in states.items():
                if state != "could_not_meet":
                    continue
                if shot in keep:
                    best = best_attempt(grouped[shot])
                    if best is not None:
                        best.accepted = True
                        best.save(update_fields=["accepted"])
                        kept.append(shot)
                        continue
                left_out.append(shot)
            parts = []
            if kept:
                parts.append(f"Kept below standard: shot {', '.join(kept)}")
            if left_out:
                parts.append(f"Left out: shot {', '.join(left_out)}")
            record = ". ".join(parts + ([notes] if notes else []))
            visuals.status = PieceVisuals.Status.APPROVED
            visuals.decided_at = timezone.now()
            visuals.save(update_fields=["status", "decided_at"])
        elif decision == "reject":
            visuals.status = PieceVisuals.Status.REJECTED
            visuals.decided_at = timezone.now()
            visuals.save(update_fields=["status", "decided_at"])
        elif decision == "changes":
            history = visuals.founder_notes.strip()
            visuals.founder_notes = (history + "\n" if history else "") + f"After round {visuals.round}: {notes}"
            visuals.round += 1
            visuals.status = PieceVisuals.Status.WRITING
            visuals.prompts, visuals.checklist, visuals.missing_information = {}, [], []
            visuals.claimed_at = None
            visuals.error = ""
            visuals.save()
        else:  # continue
            best = best_attempt(grouped["A"])
            if best is None:
                raise VisualsError("No front packshot was made, so there is nothing to keep. Request changes instead.")
            best.accepted = True
            best.save(update_fields=["accepted"])
            visuals.status = PieceVisuals.Status.MAKING
            visuals.save(update_fields=["status"])
        Approval.objects.create(
            cycle=cycle, stage=4, item=piece_label[:200], decision=DECISIONS[decision][:20],
            notes=record, decided_by=user,
        )
    if decision == "continue":
        advance(visuals)
    _finish_if_done(cycle, user)
    return visuals


def _finish_if_done(cycle: Cycle, user: str) -> None:
    sets = list(cycle.visuals.all())
    open_states = (PieceVisuals.Status.WRITING, PieceVisuals.Status.MAKING, PieceVisuals.Status.READY)
    if any(v.status in open_states for v in sets):
        return
    approved = [v for v in sets if v.status == PieceVisuals.Status.APPROVED]
    if not approved:
        return
    with transaction.atomic():
        cycle.status = Cycle.Status.VISUALS_APPROVED
        cycle.save(update_fields=["status"])
        Approval.objects.create(
            cycle=cycle, stage=4, item="Visual prototypes", decision="approved",
            notes=f"{len(approved)} of {len(sets)} pieces approved.", decided_by=user,
        )


# --- What the page shows ----------------------------------------------------------------


STATE_LABELS = {
    "passed": "Passed review",
    "kept": "Kept by you, below standard",
    "working": "Being made",
    "could_not_meet": "Could not meet standard",
    "not_started": "Waiting",
}


def piece_view(visuals: PieceVisuals) -> dict:
    grouped = _by_shot(visuals)
    shots = []
    for shot in SHOT_CODES:
        attempts = grouped[shot]
        state = shot_state(attempts)
        shown = best_attempt(attempts)
        shots.append({
            "code": shot,
            "name": SHOT_NAMES[shot],
            "state": state,
            "label": STATE_LABELS[state],
            "shown": shown,
            "attempts": attempts,
            "made": sum(1 for a in attempts if a.file),
            "error": next((a.error for a in reversed(attempts) if a.error), ""),
        })
    return {
        "visuals": visuals,
        "piece": visuals.piece,
        "shots": shots,
        "below_standard": [s for s in shots if s["state"] == "could_not_meet" and s["shown"]],
        "a_stuck": shots[0]["state"] == "could_not_meet",
        "passed": sum(1 for s in shots if s["state"] == "passed"),
    }


def progress(cycle: Cycle) -> dict:
    sets = list(cycle.visuals.all())
    active = [v for v in sets if v.status != PieceVisuals.Status.REJECTED]
    resolved = 0
    for visuals in active:
        if visuals.status == PieceVisuals.Status.WRITING:
            continue
        for attempts in _by_shot(visuals).values():
            if shot_state(attempts) in ("passed", "kept", "could_not_meet"):
                resolved += 1
    made = ShotAttempt.objects.filter(visuals__cycle=cycle).exclude(file="").count()
    return {
        "pieces": len(active),
        "shots": len(active) * len(SHOT_CODES),
        "resolved": resolved,
        "made": made,
        "ready": sum(1 for v in active if v.status == PieceVisuals.Status.READY),
        "working": sum(1 for v in active if v.status in (PieceVisuals.Status.WRITING, PieceVisuals.Status.MAKING)),
        "spent": f"{cycle.visuals_spend_usd:.2f}",
        "limit": f"{settings.FDW_VISUALS_LIMIT_USD:.2f}",
    }
