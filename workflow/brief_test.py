"""Brief test: does the brand brief make stage 2's designs better?

Stage 2 runs twice on hidden copies of one real cycle, once with an earlier
brand brief (A) and once with the current one (B). Everything else is held
equal: the same approved research brief, direction and season. The founder
judges the two sets blind, as Set 1 and Set 2 in random order; only then does
the page say which brief made which, and show the app's own checks.
"""
import random
import re
import threading
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from . import stages
from .models import (BrandBrief, BrandBriefVersion, BriefTest, Cycle, DemandBrief, ModelCall,
                     compose_prompt_text)

STAGE2_STEPS = ("plan_concepts", "detail_concept", "review_concept", "revise_concept")
DEFAULT_RUN_ESTIMATE = Decimal("3.00")
QUESTIONS = (
    ("brand", "Which set feels more like Malina?"),
    ("customer", "Which would your customer want more?"),
    ("goals", "Which better serves your goals: pre-order conversion, the waiting list, revenue?"),
    ("make", "Which would you rather make?"),
    ("overall", "Overall, which set is better?"),
)
ANSWERS = {"1": "Set 1", "2": "Set 2", "same": "No real difference"}
# Not natural fibres: synthetics, and man-made fibres from cellulose.
MAN_MADE = re.compile(r"\b(polyester|acrylic|nylon|polyamide|elastane|spandex|lycra|polypropylene|viscose|rayon|"
                      r"modal|lyocell|tencel|acetate|microfib\w*|faux fur|faux leather|pu leather|vegan leather)\b", re.I)
HANDMADE = re.compile(r"\bhand[\s-]?made\b", re.I)
HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")
PALETTE_DISTANCE = 45  # within this RGB distance a colour counts as one of the palette's


class BriefTestError(Exception):
    pass


def eligible_cycles():
    """The founder's own cycles that have an approved research brief, so stage 2 can run on a copy."""
    return [c for c in Cycle.objects.filter(is_test=False).order_by("-created_at") if c.approved_brief]


def default_version():
    """The brief as it was before the first analyst interview was approved, if there is one."""
    before = BrandBriefVersion.objects.filter(
        source=BrandBriefVersion.Source.BEFORE, note__startswith="Before the analyst interview").order_by("created_at")
    return before.first() or BrandBriefVersion.objects.order_by("created_at").first()


def version_text(version: BrandBriefVersion) -> str:
    return compose_prompt_text(version.vision, version.mission, version.goals, version.text)


def run_estimate() -> Decimal:
    """What one stage 2 run has cost so far, on average, from the founder's own cycles."""
    real = ModelCall.objects.filter(cycle__is_test=False)
    runs = real.filter(step="plan_concepts").count()
    if not runs:
        return DEFAULT_RUN_ESTIMATE
    steps = [s for base in STAGE2_STEPS for s in (base, base + "_retry")]
    total = real.filter(step__in=steps).aggregate(total=Sum("cost_usd"))["total"] or Decimal("0")
    if total <= 0:  # dry runs cost nothing and say nothing about a real run
        return DEFAULT_RUN_ESTIMATE
    return (total / runs).quantize(Decimal("0.01"))


def create(source_cycle: Cycle, version: BrandBriefVersion) -> BriefTest:
    if source_cycle.is_test or not source_cycle.approved_brief:
        raise BriefTestError("Choose one of your cycles with an approved research brief.")
    if version is None:
        raise BriefTestError("There is no earlier brand brief to compare with yet.")
    current = BrandBrief.current().prompt_text
    earlier = version_text(version)
    if earlier.strip() == current.strip():
        raise BriefTestError("That version is the same as the current brief, so there is nothing to compare.")
    if run_estimate() * 2 > settings.FDW_BRIEF_TEST_LIMIT_USD:
        raise BriefTestError(
            f"Two stage 2 runs are expected to cost about ${run_estimate() * 2:.2f}, more than the "
            f"${settings.FDW_BRIEF_TEST_LIMIT_USD} limit. Raise FDW_BRIEF_TEST_LIMIT_USD in .env to run it.")
    return BriefTest.objects.create(
        source_cycle=source_cycle, version_a=version,
        label_a=f"{version.get_source_display()}, {version.created_at:%d %b %Y %H:%M}",
        brief_a=earlier, brief_b=current, first_is_a=random.random() < 0.5,
    )


def _copy(source: Cycle, letter: str) -> Cycle:
    brief = source.approved_brief
    cycle = Cycle.objects.create(
        name=f"Brief test {letter}: {source.name}"[:120], language=source.language, direction=source.direction,
        season=source.season, season_year=source.season_year, is_test=True,
        status=Cycle.Status.BRIEF_APPROVED)
    DemandBrief.objects.create(cycle=cycle, data=brief.data, text=brief.text, founder_notes=brief.founder_notes,
                               status=DemandBrief.Status.APPROVED, decided_at=brief.decided_at)
    return cycle


def is_stale(test: BriefTest) -> bool:
    """A run that has gone on far longer than stage 2 ever takes has stopped."""
    if test.status != BriefTest.Status.RUNNING or test.started_at is None:
        return False
    return timezone.now() - test.started_at > timedelta(minutes=settings.FDW_BRIEF_TEST_STALE_MINUTES)


def can_finish(test: BriefTest) -> bool:
    """Failed, stuck, or started and never recorded (an older test interrupted before
    the running state existed): all can be finished on the copies already made."""
    if test.status == BriefTest.Status.FAILED or is_stale(test):
        return True
    return test.status == BriefTest.Status.READY and (test.cycle_a_id or test.cycle_b_id) is not None


def _claim(test: BriefTest, *, finishing: bool) -> None:
    """Mark the test as running, in one database update, so two requests can never
    run it at once. A stuck run can be taken over once it is stale."""
    now = timezone.now()
    if finishing:
        allowed = Q(status=BriefTest.Status.FAILED) | Q(
            status=BriefTest.Status.READY) & (Q(cycle_a__isnull=False) | Q(cycle_b__isnull=False))
        stale_before = now - timedelta(minutes=settings.FDW_BRIEF_TEST_STALE_MINUTES)
        allowed |= Q(status=BriefTest.Status.RUNNING, started_at__lt=stale_before)
    else:
        allowed = Q(status=BriefTest.Status.READY, cycle_a__isnull=True, cycle_b__isnull=True)
    claimed = BriefTest.objects.filter(allowed, pk=test.pk).update(
        status=BriefTest.Status.RUNNING, started_at=now, error="")
    test.refresh_from_db()
    if not claimed:
        if test.status == BriefTest.Status.RUNNING:
            raise BriefTestError("This test is already running. Reload the page in a few minutes.")
        if finishing:
            raise BriefTestError("Only a test that stopped part-way can be finished.")
        raise BriefTestError("This test has already run.")


def run(test: BriefTest, client=None) -> BriefTest:
    """Run stage 2 on both copies at the same time and keep whatever each produced."""
    _claim(test, finishing=False)
    with transaction.atomic():
        test.cycle_a = _copy(test.source_cycle, "A")
        test.cycle_b = _copy(test.source_cycle, "B")
        test.save(update_fields=["cycle_a", "cycle_b"])
    return _run_copies(test, client)


def finish(test: BriefTest, client=None) -> BriefTest:
    """Finish a test that stopped part-way, on the same copies.

    Every answer already paid for is reused (see llm._earlier_answer), so only
    the steps that never finished cost money. A run that did finish is left as it is."""
    _claim(test, finishing=True)
    with transaction.atomic():
        for letter in ("A", "B"):
            if getattr(test, f"cycle_{letter.lower()}") is None:
                setattr(test, f"cycle_{letter.lower()}", _copy(test.source_cycle, letter))
        test.save(update_fields=["cycle_a", "cycle_b"])
    return _run_copies(test, client)


def _run_copies(test: BriefTest, client) -> BriefTest:
    """Run every copy that has no concepts yet, then record the result.

    The status comes from what is actually saved (both copies have concepts, or
    not), never from which request happened to finish last."""
    runs = [(letter, cycle, text) for letter, cycle, text in
            (("A", test.cycle_a, test.brief_a), ("B", test.cycle_b, test.brief_b))
            if not cycle.concepts.exists()]
    errors = {}

    def one(letter, cycle, brief_text):
        try:
            stages.run_stage2(cycle, client=client, brand_brief=brief_text)
        except Exception as exc:  # recorded and shown; the other run carries on
            errors[letter] = str(exc) or type(exc).__name__

    def in_thread(*args):
        try:
            one(*args)
        finally:
            connection.close()  # each worker closes its own connection, as stage 2's own workers do

    if settings.FDW_PARALLEL_CALLS > 1 and len(runs) > 1:
        threads = [threading.Thread(target=in_thread, args=run) for run in runs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for run in runs:
            one(*run)

    missing = [letter for letter, cycle in (("A", test.cycle_a), ("B", test.cycle_b))
               if not cycle.concepts.exists()]
    if missing:
        status = BriefTest.Status.FAILED
        error = "; ".join(f"Run {k}: {errors.get(k, 'it produced no concepts')}" for k in missing)[:4000]
    else:
        status, error = BriefTest.Status.DONE, ""
    # Only the request that started this run writes its result. If a stale run was
    # taken over, started_at has moved on and this write does nothing.
    BriefTest.objects.filter(pk=test.pk, status=BriefTest.Status.RUNNING,
                             started_at=test.started_at).update(status=status, error=error)
    test.refresh_from_db()
    return test


def _rgb(hex_value: str):
    h = hex_value.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def palette_hexes(brief_text: str) -> list:
    """The colours of the current brief's palette line, as #RRGGBB."""
    for line in brief_text.splitlines():
        if line.strip().lower().startswith("colour palette"):
            return [h.upper() for h in HEX.findall(line)]
    return []


def checks(concepts: list, palette: list) -> dict:
    """What the app can measure on one set, against the current brief."""
    pieces = [p for c in concepts for p in c.get("pieces", [])]
    colours, near = 0, 0
    man_made, handmade_claims, prices = [], [], []
    for piece in pieces:
        for colour in piece.get("colours", []):
            value = str(colour.get("hex", ""))
            if not HEX.fullmatch(value):
                continue
            colours += 1
            if palette:
                r, g, b = _rgb(value)
                distance = min(((r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2) ** 0.5
                               for pr, pg, pb in (_rgb(h) for h in palette))
                near += distance <= PALETTE_DISTANCE
        material_text = " ".join(str(piece.get(k, "")) for k in ("materials", "trims", "construction"))
        found = sorted({m.lower() for m in MAN_MADE.findall(material_text)})
        if found:
            man_made.append(f"{piece.get('name', '?')}: {', '.join(found)}")
        claim_text = " ".join(str(piece.get(k, "")) for k in ("name", "technique", "construction", "surface_detail"))
        if piece.get("production_route") != "atelier" and HANDMADE.search(claim_text):
            handmade_claims.append(str(piece.get("name", "?")))
        try:
            prices.append(float(piece.get("target_price_eur")))
        except (TypeError, ValueError):
            pass
    return {
        "concepts": len(concepts),
        "pieces": len(pieces),
        "palette_share": round(100 * near / colours) if colours and palette else None,
        "colours": colours,
        "man_made": man_made,
        "handmade_claims": handmade_claims,
        "price_low": min(prices) if prices else None,
        "price_high": max(prices) if prices else None,
        "price_mean": round(sum(prices) / len(prices)) if prices else None,
    }


def sets(test: BriefTest) -> list:
    """The two sets in the order shown: Set 1 then Set 2, each with its concepts and checks."""
    palette = palette_hexes(BrandBrief.current().text)
    out = []
    for letter, cycle in (("A", test.cycle_a), ("B", test.cycle_b)):
        concepts = []
        if cycle is not None:
            for concept in cycle.concepts.all().order_by("concept_id"):
                review = concept.reviews.order_by("-id").first()
                concepts.append(dict(concept.data, review_overall=review.overall if review else None,
                                     unresolved=concept.status == concept.Status.UNRESOLVED))
        out.append({"letter": letter, "concepts": concepts, "checks": checks(concepts, palette),
                    "cost": cycle.spend_usd if cycle else Decimal("0")})
    if not test.first_is_a:
        out.reverse()
    for number, item in enumerate(out, 1):
        item["number"] = number
    return out


def judge(test: BriefTest, answers: dict, notes: str = "") -> BriefTest:
    if test.status != BriefTest.Status.DONE:
        raise BriefTestError("Judge the sets once both runs have finished.")
    missing = [label for key, label in QUESTIONS if answers.get(key) not in ANSWERS]
    if missing:
        raise BriefTestError("Answer every question: " + "; ".join(missing))
    test.judgement = {key: answers[key] for key, _ in QUESTIONS}
    test.notes = notes.strip()
    test.status = BriefTest.Status.JUDGED
    test.save(update_fields=["judgement", "notes", "status"])
    return test


def outcome(test: BriefTest) -> dict:
    """The founder's answers translated from Set 1 / Set 2 to the earlier (A) and current (B) brief."""
    set_letter = {"1": "A" if test.first_is_a else "B", "2": "B" if test.first_is_a else "A"}
    rows, score = [], {"A": 0, "B": 0}
    for key, label in QUESTIONS:
        answer = test.judgement.get(key)
        winner = set_letter.get(answer)
        if winner:
            score[winner] += 1
        rows.append({"question": label, "answer": ANSWERS.get(answer, "?"), "winner": winner})
    return {"rows": rows, "score": score}
