"""What FDW already knows about Malina's market, for the brand analyst.

The brand interview used to know only what the founder told it. FDW holds
better evidence than memory: the customer signals it collected, the research
briefs the founder approved, and what the founder chose or rejected at each
gate. This module turns that into a short pack of numbered items the analyst
reads before asking questions and cites when it writes the draft.

Every item has an id (S1 for a group of customer signals, R1 for a research
brief, G1 for a gate decision, T1 for a blind brief test), so a claim in the
draft can be traced to its source and a made-up id can be caught. Test copies
from the brief test are left out: their designs are not decisions.

Customer quotes are untrusted text. They are trimmed, and the prompts tell the
analyst to treat everything in the pack as data, never as instructions.
"""
import re
from collections import defaultdict

from .models import Approval, BriefTest, DemandBrief, Signal

MAX_SIGNAL_GROUPS = 15
MAX_QUOTES_PER_GROUP = 2
MAX_QUOTE_CHARS = 160
MAX_RESEARCH_BRIEFS = 3
MAX_RESEARCH_CHARS = 900
MAX_DECISIONS = 20
MAX_TESTS = 3
MAX_PACK_CHARS = 9000
ID = re.compile(r"\b([SRGT]\d{1,3})\b")

INTENT_WORDS = {"purchase": "asked to buy or the price", "interest": "showed interest", "trend": "trend data"}


def _clean(text: str, limit: int) -> str:
    """One line, no angle brackets (so a quote cannot close the prompt's tags), within the limit."""
    text = re.sub(r"\s+", " ", str(text or "")).strip().replace("<", "(").replace(">", ")")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _signal_items():
    groups = defaultdict(list)
    for s in Signal.objects.filter(cycle__is_test=False).select_related("cycle"):
        key = (s.intent, (s.product_type or "unspecified").strip().lower(), (s.technique or "").strip().lower())
        groups[key].append(s)
    # Purchase intent first: it is the strongest evidence of demand. Then by how often it came up.
    order = sorted(groups.items(), key=lambda kv: (kv[0][0] != "purchase", -len(kv[1]), kv[0]))
    items = []
    for (intent, product, technique), signals in order[:MAX_SIGNAL_GROUPS]:
        sources = sorted({s.source for s in signals if s.source})
        cycles = sorted({s.cycle.name for s in signals})
        what = product + (f", {technique}" if technique else "")
        quotes = [f'"{_clean(s.quote, MAX_QUOTE_CHARS)}"'
                  for s in sorted(signals, key=lambda s: -(s.strength or 0)) if s.quote][:MAX_QUOTES_PER_GROUP]
        line = (f"{len(signals)} signal{'s' if len(signals) != 1 else ''}: {INTENT_WORDS.get(intent, intent)} about "
                f"{what} (from {', '.join(sources) or 'unknown source'}; cycle {', '.join(cycles)})")
        if quotes:
            line += ". Quotes: " + " ".join(quotes)
        items.append(("S", line, f"{len(signals)} × {INTENT_WORDS.get(intent, intent)}: {what}"))
    return items


def _research_items():
    items = []
    briefs = (DemandBrief.objects.filter(cycle__is_test=False, status=DemandBrief.Status.APPROVED)
              .select_related("cycle").order_by("-decided_at", "-created_at")[:MAX_RESEARCH_BRIEFS])
    for brief in briefs:
        data = brief.data or {}
        parts = []
        if data.get("headline"):
            parts.append(f"Headline: {data['headline']}")
        for t in (data.get("key_takeaways") or [])[:3]:
            if isinstance(t, dict):
                parts.append(f"Takeaway: {t.get('headline', '')} {t.get('so_what', '')}".strip())
            else:
                parts.append(f"Takeaway: {t}")
        themes = [t.get("name") for t in (data.get("themes") or []) if isinstance(t, dict) and t.get("name")]
        if themes:
            parts.append("Demand themes, strongest first: " + "; ".join(themes[:5]))
        for i in (data.get("implications") or [])[:3]:
            if isinstance(i, dict) and i.get("title"):
                parts.append(f"Implication: {i['title']}")
        body = " | ".join(str(p) for p in parts) or brief.text
        line = f"Approved research brief for {brief.cycle.name}: {_clean(body, MAX_RESEARCH_CHARS)}"
        if brief.founder_notes:
            line += f" Founder's note: {_clean(brief.founder_notes, 300)}"
        items.append(("R", line, f"Research brief, {brief.cycle.name}"))
    rejected = (DemandBrief.objects.filter(cycle__is_test=False, status=DemandBrief.Status.REJECTED)
                .exclude(founder_notes="").select_related("cycle").order_by("-decided_at")[:MAX_RESEARCH_BRIEFS])
    for brief in rejected:
        items.append(("G", f"Founder rejected a research brief for {brief.cycle.name}, saying: "
                           f"{_clean(brief.founder_notes, 300)}", f"Rejected research brief, {brief.cycle.name}"))
    return items


def _decision_items():
    items = []
    approvals = (Approval.objects.filter(cycle__is_test=False).select_related("cycle")
                 .order_by("-decided_at")[:MAX_DECISIONS])
    for a in approvals:
        line = f"Stage {a.stage}, {a.cycle.name}: {a.decision} {_clean(a.item, 120)}"
        if a.notes:
            line += f". Founder's note: {_clean(a.notes, 240)}"
        items.append(("G", line, f"Stage {a.stage}: {a.decision} {_clean(a.item, 60)}"))
    return items


def _test_items():
    from . import brief_test  # avoids an import cycle at module load

    items = []
    for test in BriefTest.objects.filter(status=BriefTest.Status.JUDGED).select_related("source_cycle")[:MAX_TESTS]:
        score = brief_test.outcome(test)["score"]
        line = (f"Blind brief test on {test.source_cycle.name}, {test.created_at:%d %b %Y}: the founder preferred "
                f"designs from the then-current brief on {score['B']} of 5 questions and from an earlier brief on "
                f"{score['A']}")
        if test.notes:
            line += f". Founder's note: {_clean(test.notes, 240)}"
        items.append(("T", line, f"Brief test {test.created_at:%d %b}: current {score['B']}, earlier {score['A']}"))
    return items


def build_pack() -> dict:
    """The evidence pack: {"text": prompt text, "items": {id: short label}, "counts": {...}}.

    Items are numbered per kind in order. If the whole pack is too long, the
    least important items (last research briefs, oldest decisions) are dropped
    so the most telling evidence always fits."""
    raw = _signal_items() + _research_items() + _decision_items() + _test_items()
    numbered, counters = [], defaultdict(int)
    for kind, line, label in raw:
        counters[kind] += 1
        numbered.append((f"{kind}{counters[kind]}", line, label))
    lines, items, total = [], {}, 0
    for item_id, line, label in numbered:
        entry = f"[{item_id}] {line}"
        if total + len(entry) > MAX_PACK_CHARS:
            continue
        lines.append(entry)
        items[item_id] = label
        total += len(entry) + 1
    counts = {k: sum(1 for i in items if i.startswith(k)) for k in "SRGT"}
    if not lines:
        text = "(FDW holds no evidence yet: no customer signals, approved research briefs or gate decisions.)"
    else:
        text = "\n".join(lines)
    return {"text": text, "items": items, "counts": counts}


def known_ids(pack: dict) -> set:
    return set((pack or {}).get("items", {}))


def cited(ids, pack: dict) -> list:
    """The ids that really are in the pack, in order, without repeats."""
    known, out = known_ids(pack), []
    for raw in ids or []:
        for match in ID.findall(str(raw)):
            if match in known and match not in out:
                out.append(match)
    return out


def unknown(ids, pack: dict) -> list:
    known, out = known_ids(pack), []
    for raw in ids or []:
        for match in ID.findall(str(raw)):
            if match not in known and match not in out:
                out.append(match)
    return out


def summary(pack: dict) -> str:
    """One line for the page: what the analyst read."""
    c = (pack or {}).get("counts") or {}
    parts = [(c.get("S", 0), "group", "groups", "of customer signals"), (c.get("R", 0), "research brief", "research briefs", ""),
             (c.get("G", 0), "gate decision", "gate decisions", ""), (c.get("T", 0), "brief test", "brief tests", "")]
    words = [f"{n} {one if n == 1 else many}{(' ' + tail) if tail else ''}" for n, one, many, tail in parts if n]
    return ", ".join(words) if words else "nothing yet"
