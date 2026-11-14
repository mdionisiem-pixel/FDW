"""Brand analyst interview: an open-weight model questions the founder, then drafts
the vision, mission, goals and brand brief for the founder to edit and approve.

The brief reaches every prompt in every stage, so nothing the analyst writes is
used until the founder approves it, and every approved version is kept.
"""
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction

from . import bakeoff, prompts, schemas
from .llm import parse_json
from .models import LONG, SHORT, AnalystCall, BrandBrief, BrandBriefVersion, BrandInterview, InterviewTurn

AREAS = prompts.BRAND_AREAS
MAX_ANSWER_CHARS = 4000


class AnalystError(Exception):
    """Something the founder should read: what went wrong and what to do."""


@dataclass
class Reply:
    text: str
    cost: Decimal = Decimal("0")
    finish_reason: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class OpenRouterChat(bakeoff.OpenRouterImages):
    """Text chat through OpenRouter, sharing the image client's request, credit and error handling."""

    def complete(self, prompt: str, max_tokens: int, effort: str) -> Reply:
        payload = {
            "model": settings.FDW_MODEL_ANALYST,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            # The model may think first; only the answer is returned.
            "reasoning": {"effort": effort, "exclude": True},
            "response_format": {"type": "json_object"},
        }
        if settings.FDW_ANALYST_PRIVATE:
            # The founder's strategy goes only to providers that neither store nor train on it.
            payload["provider"] = {"data_collection": "deny", "zdr": True}
        try:
            data = self._send(payload)
        except bakeoff.BakeoffError as exc:
            if "returned 400" not in str(exc) or "response_format" not in payload:
                raise
            # Some providers reject JSON mode; the prompt already asks for JSON only.
            payload.pop("response_format")
            data = self._send(payload)
        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        usage = data.get("usage") or {}
        cost = Decimal("0")
        if usage.get("cost") is not None:
            try:
                cost = Decimal(str(usage["cost"])).quantize(Decimal("0.0001"))
            except InvalidOperation:
                pass
        return Reply(
            text=(choice.get("message") or {}).get("content") or "",
            cost=cost,
            finish_reason=str(choice.get("finish_reason") or choice.get("native_finish_reason") or ""),
            provider=str(data.get("provider") or ""),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )

    def _send(self, payload):
        try:
            return self._send_image_request(payload, path="/chat/completions")
        except bakeoff.BakeoffError as exc:
            text = str(exc)
            if "provider" in payload and ("returned 404" in text or "data policy" in text.lower()):
                raise AnalystError(
                    f"No provider of {payload['model']} on OpenRouter meets the privacy setting (no storing, no "
                    "training on your answers). Choose another model with FDW_MODEL_ANALYST, or, if you accept "
                    "providers that may keep your answers, set FDW_ANALYST_PRIVATE=0 in .env. Nothing was charged."
                ) from exc
            raise


class DryRunAnalyst:
    """Stand-in answers for FDW_DRY_RUN: walks the areas in order and drafts a sample brief."""

    def complete(self, prompt: str, max_tokens: int, effort: str) -> Reply:
        if "<interview>" in prompt:
            return Reply(json.dumps({
                "vision": "A wardrobe where every piece is made to be kept, mended and handed on. (To confirm)",
                "mission": "Malina designs embroidered and crochet pieces in small batches, proving demand before "
                           "making, for people who buy fewer, better things. (To confirm)",
                "goals": [
                    {"goal": "Sell out the winter pre-order", "measure": "40 pieces", "by_when": "March 2027",
                     "horizon": "short"},
                    {"goal": "Reach 1,000 waiting-list sign-ups", "measure": "sign-ups", "by_when": "December 2027",
                     "horizon": "short"},
                    {"goal": "Run Malina as a two-person atelier", "measure": "two makers", "by_when": "2030",
                     "horizon": "long"},
                ],
                "brief": "WHO BUYS\nSample customer from the dry run.\n\nRULES\nSample rules kept from the current brief.",
                "changes": ["Sample change made by the dry run."],
                "to_confirm": ["Sample gap the founder should settle."],
            }))
        asked = int(re.search(r"<questions_asked_so_far>(\d+)</questions_asked_so_far>", prompt).group(1))
        keys = list(AREAS)
        area = keys[min(asked, len(keys) - 1)]
        coverage = {k: (2 if i < asked else 0) for i, k in enumerate(keys)}
        return Reply(json.dumps({
            "reflection": "" if asked == 0 else "Sample: understood.",
            "question": f"Sample question {asked + 1} about {AREAS[area].lower()}: what should the brief say?",
            "why": "Sample: the dry run asks one question per area.",
            "area": area,
            "examples": ["A sample answer", "A different sample answer"],
            "coverage": coverage,
            "ready_to_draft": asked >= len(keys) - 1,
        }))


def get_client():
    if settings.FDW_DRY_RUN:
        return DryRunAnalyst()
    try:
        return OpenRouterChat()
    except bakeoff.BakeoffError as exc:
        raise AnalystError(str(exc)) from exc


def current_interview():
    return BrandInterview.objects.filter(status__in=(BrandInterview.Status.ASKING, BrandInterview.Status.DRAFTED)).first()


def conversation_text(interview: BrandInterview) -> str:
    lines = []
    for turn in interview.turns.all():
        if turn.role == InterviewTurn.Role.ANALYST and not turn.text:
            lines.append("Analyst: (said the picture is complete and offered to write the draft)")
            continue
        speaker = "Analyst" if turn.role == InterviewTurn.Role.ANALYST else "Founder"
        lines.append(f"{speaker}: {turn.text}")
    return "\n".join(lines) or "(The interview has not started yet.)"


def _check_spend(interview: BrandInterview):
    if interview.cost_usd >= settings.FDW_INTERVIEW_LIMIT_USD:
        raise AnalystError(
            f"This interview has spent ${interview.cost_usd:.2f}, its limit of ${settings.FDW_INTERVIEW_LIMIT_USD:.2f}. "
            "Draft the brief from what is covered, or raise FDW_INTERVIEW_LIMIT_USD in .env."
        )


def _ask(interview, client, kind, prompt, max_tokens, effort) -> Reply:
    """One request, timed and recorded whatever happens, so any question or draft can be traced."""
    record = AnalystCall(interview=interview, kind=kind, model=settings.FDW_MODEL_ANALYST, prompt=prompt,
                         max_tokens=max_tokens)
    started = time.monotonic()
    try:
        reply = client.complete(prompt, max_tokens, effort)
        if isinstance(reply, tuple):  # a plain (text, cost) answer from a stand-in client
            reply = Reply(*reply)
    except Exception as exc:
        record.ok, record.error = False, str(exc)[:4000]
        record.seconds = Decimal(str(round(time.monotonic() - started, 2)))
        record.save()
        raise
    record.seconds = Decimal(str(round(time.monotonic() - started, 2)))
    record.response, record.finish_reason, record.provider = reply.text, reply.finish_reason, reply.provider[:80]
    record.input_tokens, record.output_tokens, record.cost_usd = reply.input_tokens, reply.output_tokens, reply.cost
    record.save()
    return reply


def _call(interview, client, kind, prompt, max_tokens, effort, validate):
    """One answer, retried once if it cannot be used.

    A reply cut off at the length limit (the model spent its budget thinking)
    is asked again with twice the room; any other unusable reply is asked
    again with the problems listed."""
    _check_spend(interview)
    attempt_prompt, attempt_tokens, attempt_kind = prompt, max_tokens, kind
    spent = Decimal("0")
    try:
        for _ in range(2):
            reply = _ask(interview, client, attempt_kind, attempt_prompt, attempt_tokens, effort)
            spent += reply.cost
            attempt_kind = kind + "_retry"
            if reply.finish_reason == "length" or not reply.text.strip():
                attempt_tokens = max_tokens * 2
                continue
            try:
                return validate(parse_json(reply.text)), spent
            except schemas.SchemaError as exc:
                attempt_prompt = (prompt + "\n\nYour previous answer could not be used. Problems found:\n- "
                                  + "\n- ".join(exc.problems[:10]) + "\n\nReturn the complete corrected JSON and nothing else.")
    except bakeoff.BakeoffError as exc:
        raise AnalystError(str(exc)) from exc
    finally:
        if spent:
            BrandInterview.objects.filter(pk=interview.pk).update(cost_usd=interview.cost_usd + spent)
            interview.cost_usd += spent
    if reply.finish_reason == "length" or not reply.text.strip():
        raise AnalystError(
            f"{settings.FDW_MODEL_ANALYST} ran out of room before answering, twice. Raise "
            f"{'FDW_ANALYST_DRAFT_TOKENS' if kind == 'draft' else 'FDW_ANALYST_TURN_TOKENS'} in .env, or press "
            "the button again.")
    raise AnalystError("The analyst's answer could not be used twice in a row. Press the button again.")


def start(client=None) -> BrandInterview:
    """Resume the open interview, or start one from the current brief with the first question."""
    interview = current_interview()
    if interview is not None:
        return interview
    interview = BrandInterview.objects.create(
        model=settings.FDW_MODEL_ANALYST, starting_brief=BrandBrief.current().prompt_text)
    ask_next(interview, client or get_client())
    return interview


def ask_next(interview: BrandInterview, client) -> InterviewTurn:
    asked = interview.turns.filter(role=InterviewTurn.Role.ANALYST).count()
    coverage = {k: interview.coverage.get(k, 0) for k in AREAS}
    prompt = prompts.render(
        prompts.BRAND_ANALYST,
        areas=", ".join(AREAS.values()),
        area_keys=", ".join(AREAS),
        coverage_template=json.dumps(coverage),
        brief=interview.starting_brief,
        conversation=conversation_text(interview),
        asked=asked,
    )
    turn_data, cost = _call(interview, client, "question", prompt, settings.FDW_ANALYST_TURN_TOKENS,
                            settings.FDW_ANALYST_TURN_EFFORT, lambda d: schemas.validate_analyst_turn(d, AREAS))
    with transaction.atomic():
        last = interview.turns.last()
        if last is not None and last.role == InterviewTurn.Role.ANALYST and asked < \
                interview.turns.filter(role=InterviewTurn.Role.ANALYST).count():
            # Another request already brought the next question; keep that one.
            return last
        interview.coverage = dict(turn_data["coverage"], ready=turn_data["ready_to_draft"])
        interview.error = ""
        interview.save(update_fields=["coverage", "error", "updated_at"])
        return InterviewTurn.objects.create(
            interview=interview, role=InterviewTurn.Role.ANALYST, text=turn_data["question"],
            reflection=turn_data["reflection"], why=turn_data["why"], area=turn_data["area"],
            examples=turn_data["examples"], cost_usd=cost,
        )


def answer(interview: BrandInterview, text: str, client=None) -> InterviewTurn:
    """Record the founder's answer and return the analyst's next question."""
    text = (text or "").strip()
    if not text:
        raise AnalystError("Write an answer, tap an example, or write 'skip'.")
    if len(text) > MAX_ANSWER_CHARS:
        raise AnalystError(f"Keep each answer under {MAX_ANSWER_CHARS} characters; split a long one across questions.")
    with transaction.atomic():
        interview = BrandInterview.objects.get(pk=interview.pk)
        if interview.status != BrandInterview.Status.ASKING:
            raise AnalystError("This interview is no longer taking answers.")
        last = interview.turns.last()
        if last is None or last.role != InterviewTurn.Role.ANALYST:
            # A second click while the analyst is still thinking.
            raise AnalystError("The analyst is still working on the previous answer.")
        InterviewTurn.objects.create(interview=interview, role=InterviewTurn.Role.FOUNDER, text=text)
    try:
        return ask_next(interview, client or get_client())
    except AnalystError as exc:
        # The answer is kept; pressing Ask again retries the question.
        BrandInterview.objects.filter(pk=interview.pk).update(error=str(exc)[:2000])
        raise


def retry_question(interview: BrandInterview, client=None) -> InterviewTurn:
    """Ask the next question again after a failed call. Only when the founder spoke last."""
    last = interview.turns.last()
    if last is not None and last.role == InterviewTurn.Role.ANALYST:
        return last
    return ask_next(interview, client or get_client())


def draft(interview: BrandInterview, client=None) -> dict:
    if interview.status not in (BrandInterview.Status.ASKING, BrandInterview.Status.DRAFTED):
        raise AnalystError("This interview is closed.")
    if not interview.turns.filter(role=InterviewTurn.Role.FOUNDER).exists():
        raise AnalystError("Answer at least one question first: the draft is built from your answers.")
    prompt = prompts.render(prompts.BRAND_DRAFT, brief=interview.starting_brief,
                            conversation=conversation_text(interview))
    result, _ = _call(interview, client or get_client(), "draft", prompt, settings.FDW_ANALYST_DRAFT_TOKENS,
                      settings.FDW_ANALYST_DRAFT_EFFORT, schemas.validate_brand_draft)
    answers = "\n".join(interview.turns.filter(role=InterviewTurn.Role.FOUNDER).values_list("text", flat=True))
    result = check_draft(result, interview.starting_brief, answers)
    interview.draft = result
    interview.status = BrandInterview.Status.DRAFTED
    interview.error = ""
    interview.save(update_fields=["draft", "status", "error", "updated_at"])
    return result


def goals_from_form(data) -> list:
    """Goals from the page's goal rows: separate goal, measure and by-when fields per horizon.

    Rows whose goal is empty are dropped, so clearing a goal removes it."""
    goals = []
    for horizon in (SHORT, LONG):
        names, measures, dates = (data.getlist(f"{horizon}_{k}") for k in ("goal", "measure", "by"))
        for i, name in enumerate(names):
            name = name.strip()
            if not name:
                continue
            goals.append({
                "goal": name[:300],
                "measure": (measures[i] if i < len(measures) else "").strip()[:200],
                "by_when": (dates[i] if i < len(dates) else "").strip()[:100],
                "horizon": horizon,
            })
    return goals


def _closing_group(text: str):
    """The bracketed group that ends ``text``, allowing brackets inside it: (start, inner) or None."""
    text = text.rstrip()
    if not text.endswith(")"):
        return None
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            depth -= 1
            if depth == 0:
                return i, text[i + 1:-1]
    return None


def _top_level_split(text: str, sep: str = ";") -> list:
    parts, depth, current = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == sep and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
        depth = max(depth, 0)
    parts.append(current.strip())
    return parts


def repair_goal(goal: dict) -> dict:
    """Undo the old text format's parsing mistakes in a saved goal.

    Goals were once edited as 'goal (measure; by when)' lines. A date with its
    own brackets, such as 'December (year not stated)', left the whole line in
    the goal; a measure containing ';' split in the wrong place. The last part
    after a top-level ';' is the date, everything before it the measure."""
    goal = dict(goal)
    name, measure, by_when = goal.get("goal", ""), goal.get("measure", ""), goal.get("by_when", "")
    if not measure and not by_when:
        group = _closing_group(name)
        if group:
            start, inner = group
            parts = _top_level_split(inner)
            name = name[:start].strip()
            measure = "; ".join(parts[:-1]) if len(parts) > 1 else parts[0]
            by_when = parts[-1] if len(parts) > 1 else ""
    elif ";" in by_when:
        parts = _top_level_split(by_when)
        measure = "; ".join([measure] + parts[:-1]) if measure else "; ".join(parts[:-1])
        by_when = parts[-1]
    goal.update(goal=name, measure=measure, by_when=by_when)
    return goal


def approve(interview: BrandInterview, vision: str, mission: str, goals: list, brief_text: str) -> BrandBrief:
    """Make the founder's edited draft the brand brief that every prompt uses."""
    if interview.status != BrandInterview.Status.DRAFTED:
        raise AnalystError("There is no draft to approve yet.")
    if not brief_text.strip():
        raise AnalystError("The brief cannot be empty. Every prompt depends on it.")
    with transaction.atomic():
        brief = BrandBrief.current()
        brief.snapshot(BrandBriefVersion.Source.BEFORE, "Before the analyst interview was approved")
        brief.vision = vision.strip()
        brief.mission = mission.strip()
        brief.goals = goals
        brief.text = brief_text.strip()
        brief.save()
        brief.snapshot(BrandBriefVersion.Source.INTERVIEW, f"Approved from the interview of {interview.created_at:%d %b %Y}")
        interview.status = BrandInterview.Status.APPROVED
        interview.save(update_fields=["status", "updated_at"])
    return brief


def close(interview: BrandInterview) -> None:
    interview.status = BrandInterview.Status.CLOSED
    interview.save(update_fields=["status", "updated_at"])


# --- Checks on the draft, in code ------------------------------------------------
# The draft prompt asks for these; code makes sure, because the brief reaches every
# prompt and the rules below protect buyers from false claims.

PROTECTED_LINES = (
    "- Atelier: made by hand by Malina. May be described as handmade.",
    '- Studio: designed by Malina, made by a named type of supplier (for example "machine-knitted in a '
    'partner workshop"). Never described as handmade.',
    "- Never claim a piece is handmade unless its tier is Atelier.",
    "- Never state a fibre, origin, certification or sustainability claim that is not in the piece specification.",
    '- Every image or 3D view of a piece that does not physically exist yet is labelled "Concept render. '
    'The finished piece may differ slightly."',
)
RULE_HEADINGS = re.compile(r"^(hard rules|rules)\s*:?\s*$", re.I)
NUMBER = re.compile(r"(?<![\w#])[€$£]?\d[\d,.]*\s?(?:%|k\b|m\b)?", re.I)
VISION_WORDS, MISSION_WORDS = 25, 45


def _norm(line: str) -> str:
    return " ".join(line.split()).strip().lower()


def protected_rules(brief_text: str) -> list:
    """The rule lines in ``brief_text`` that must survive: the known buyer-protection
    lines, and every line under a 'Hard rules' or 'Rules' heading."""
    lines = [l.strip() for l in brief_text.splitlines()]
    known = {_norm(p) for p in PROTECTED_LINES}
    found, under_rules = [], False
    for line in lines:
        if not line:
            under_rules = False if not under_rules else under_rules
            continue
        if RULE_HEADINGS.match(line):
            under_rules = True
            continue
        if under_rules and not line.startswith("-"):
            under_rules = False
        if _norm(line) in known or (under_rules and line.startswith("-")):
            if line not in found:
                found.append(line)
    return found


def _numbers(text: str) -> set:
    out = set()
    for raw in NUMBER.findall(text or ""):
        value = raw.strip().lower().replace(",", "").replace(" ", "").rstrip(".")
        value = value.lstrip("€$£")
        if value.endswith("k"):
            try:
                value = str(int(float(value[:-1]) * 1000))
            except ValueError:
                pass
        value = value.rstrip("%")
        if value and len(value.replace(".", "")) >= 2:
            out.add(value)
    return out


def check_draft(draft: dict, starting_brief: str, founder_answers: str) -> dict:
    """Restore missing protected rules and flag what the founder should look at.

    Returns the draft with its brief possibly extended and a ``checks`` list of
    {"kind", "text"} the review page shows above the draft."""
    checks = []
    brief = draft["brief"]
    present = {_norm(l) for l in brief.splitlines()}
    missing = [r for r in protected_rules(starting_brief) if _norm(r) not in present]
    if missing:
        brief = brief.rstrip() + "\n\nRULES (restored from the current brief)\n" + "\n".join(missing)
        for rule in missing:
            checks.append({"kind": "restored", "text": f"Restored a rule the draft left out: {rule} "
                                                       "Delete it below only if you meant to change it."})
    allowed = _numbers(starting_brief) | _numbers(founder_answers)
    draft_text = " ".join([draft["vision"], draft["mission"], brief] +
                          [f"{g['goal']} {g['measure']} {g['by_when']}" for g in draft["goals"]])
    unsourced = sorted(_numbers(draft_text) - allowed)
    if unsourced:
        checks.append({"kind": "number", "text": "These figures are not in your answers or the current brief, so "
                       "the analyst may have invented them: " + ", ".join(unsourced) + ". Check each one."})
    for field, limit in (("vision", VISION_WORDS), ("mission", MISSION_WORDS)):
        words = len(draft[field].split())
        if words > limit:
            checks.append({"kind": "length", "text": f"The {field} is {words} words; aim for {limit} or fewer, "
                           "so it stays sharp in every prompt."})
    return dict(draft, brief=brief, checks=checks)
