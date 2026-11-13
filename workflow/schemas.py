"""Validation of model output.

Model output is never trusted as-is. Every JSON response is checked against
the structure its stage expects before anything is saved, and a few rules that
must never be broken are checked in code as well as by the reviewer model.
"""
import re

SIGNAL_SOURCES = {"dm", "comment", "post_metric", "pinterest", "google_trends", "article"}
SIGNAL_INTENTS = {"purchase", "interest", "trend"}
ROLES = {"hero", "core", "accessory"}
ROUTES = {"atelier", "on_demand", "specialist"}
REVIEW_CRITERIA = (
    "demand_evidence",
    "coherence",
    "uniqueness",
    "makeability",
    "price_fit",
    "spec_completeness",
)
REVIEW_OVERALL = {"recommend", "revise", "reject"}
FINDING_TIMEFRAMES = {"recurring", "current", "both"}
CATEGORIES = ("knitwear", "apparel", "outerwear", "accessories", "bags", "footwear", "jewellery", "other")
MOMENTUM = {"rising", "stable", "fading"}
FITS = ("core", "adjacent", "outside")
HISTORY_LEVELS = ("absent", "emerging", "established", "peak", "declining", "unknown")
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
HANDMADE_WORDS = re.compile(r"\bhand[\s-]?made\b", re.IGNORECASE)

PIECE_TEXT_FIELDS = (
    "piece_id",
    "name",
    "category",
    "silhouette",
    "technique",
    "construction",
    "materials",
    "surface_detail",
)


class SchemaError(Exception):
    """Raised with a list of human-readable problems found in model output."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _is_text(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_signals(data):
    problems = []
    if not isinstance(data, list):
        raise SchemaError(["Expected a JSON array of signals."])
    for i, item in enumerate(data):
        where = f"signal {i + 1}"
        if not isinstance(item, dict):
            problems.append(f"{where}: must be an object")
            continue
        if item.get("source") not in SIGNAL_SOURCES:
            problems.append(f"{where}: source must be one of {sorted(SIGNAL_SOURCES)}")
        if item.get("intent") not in SIGNAL_INTENTS:
            problems.append(f"{where}: intent must be one of {sorted(SIGNAL_INTENTS)}")
        strength = item.get("strength")
        if strength is not None and not _is_number(strength):
            problems.append(f"{where}: strength must be a number or null")
        attributes = item.get("attributes")
        if attributes is not None and not isinstance(attributes, (list, str)):
            problems.append(f"{where}: attributes must be a list, a string or null")
    if problems:
        raise SchemaError(problems)
    return data


def normalise_url(url) -> str:
    """Reduce a URL to a form that can be compared: no scheme, no trailing slash."""
    text = str(url or "").strip()
    for prefix in ("https://", "http://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    if text.lower().startswith("www."):
        text = text[4:]
    text = text.split("#", 1)[0].rstrip("/")
    host, _, rest = text.partition("/")
    return host.lower() + ("/" + rest if rest else "")


def _number_forms(value) -> set:
    """Ways a number can be written in a source: 40, 40.0, 40,5, 1,200, 1 200, 1200."""
    number = abs(float(value))
    forms = set()
    if number == int(number):
        whole = int(number)
        forms.update({str(whole), f"{whole:,}", f"{whole:,}".replace(",", " "), f"{whole:,}".replace(",", ".")})
    else:
        text = f"{number:.10g}"
        forms.update({text, text.replace(".", ",")})
        rounded = f"{number:.1f}"
        forms.update({rounded, rounded.replace(".", ",")})
    return forms


def number_in_text(value, text) -> bool:
    """True when ``value`` is written in ``text`` as a whole number, not inside a longer one."""
    import re as _re

    haystack = str(text or "")
    for form in _number_forms(value):
        if _re.search(r"(?<![\d.,])" + _re.escape(form) + r"(?![\d]|[.,]\d)", haystack):
            return True
    return False


def supported_figures(finding) -> list:
    """Keep only the figures whose number appears in the finding's own evidence text.

    This is how a chart is kept honest: a number the model added without
    quoting it from the source never reaches the dashboard.
    """
    kept = []
    for figure in finding.get("figures") or []:
        if number_in_text(figure["value"], finding.get("evidence")):
            kept.append({
                "label": str(figure["label"])[:200],
                "value": float(figure["value"]),
                "unit": str(figure.get("unit") or "")[:20],
                "period": str(figure.get("period") or "")[:60],
            })
    return kept


def supported_forecast(finding):
    """The finding's forecast, with its number removed unless the statement contains it."""
    forecast = finding.get("forecast")
    if not isinstance(forecast, dict) or not _is_text(forecast.get("statement")):
        return None
    value = forecast.get("value")
    keep_value = _is_number(value) and number_in_text(value, forecast["statement"])
    return {
        "statement": str(forecast["statement"])[:500],
        "horizon": str(forecast.get("horizon") or "")[:60],
        "value": float(value) if keep_value else None,
        "unit": str(forecast.get("unit") or "")[:20] if keep_value else "",
    }


def validate_findings(data):
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise SchemaError(['Expected an object with a "findings" list.'])
    problems = []
    for i, item in enumerate(data["findings"]):
        where = f"finding {i + 1}"
        if not isinstance(item, dict):
            problems.append(f"{where}: must be an object")
            continue
        for field in ("theme", "what_people_want", "evidence"):
            if not _is_text(item.get(field)):
                problems.append(f"{where}: {field} is missing or empty")
        url = str(item.get("source_url") or "")
        if not url.lower().startswith(("http://", "https://")):
            problems.append(f"{where}: source_url must be a full web address")
        if item.get("timeframe") not in FINDING_TIMEFRAMES:
            problems.append(f"{where}: timeframe must be one of {sorted(FINDING_TIMEFRAMES)}")
        if item.get("category") not in CATEGORIES:
            problems.append(f"{where}: category must be one of {list(CATEGORIES)}")
        if item.get("momentum") not in MOMENTUM:
            problems.append(f"{where}: momentum must be one of {sorted(MOMENTUM)}")
        if item.get("malina_fit") not in FITS:
            problems.append(f"{where}: malina_fit must be one of {list(FITS)}")
        figures = item.get("figures", [])
        if not isinstance(figures, list):
            problems.append(f"{where}: figures must be a list")
        else:
            for figure in figures:
                if not isinstance(figure, dict) or not _is_text(figure.get("label")) or not _is_number(figure.get("value")):
                    problems.append(f"{where}: every figure needs a label and a numeric value")
        forecast = item.get("forecast")
        if forecast is not None and (not isinstance(forecast, dict) or not _is_text(forecast.get("statement"))):
            problems.append(f"{where}: forecast must be null or an object with a statement")
    if problems:
        raise SchemaError(problems)
    return data


BRIEF_STRENGTHS = ("strong", "moderate", "weak", "untested")
BRIEF_TIMEFRAMES = {"recurring", "current", "both", "not_applicable"}
BRIEF_BASES = {"web_research", "audience_signals", "founder_direction"}
DIRECTION_VERDICTS = {"supported", "mixed", "contradicted", "untested", "none_given"}
OUTLOOK_LEVELS = ("emerging", "established", "peak", "declining", "unknown")


def _string_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _score(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5


def _object_list(value, keys) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict) and all(_is_text(item.get(k)) for k in keys) for item in value
    )


def validate_brief(data, allowed_urls=None):
    """Validate the strategic market report.

    ``allowed_urls`` is the set of normalised URLs of the verified findings.
    A source in the report must be one of them, so it cannot cite a page that
    the research did not actually return.
    """
    if not isinstance(data, dict):
        raise SchemaError(["Expected a JSON object for the report."])
    problems = []
    for field in ("season", "headline", "past_tests"):
        if not _is_text(data.get(field)):
            problems.append(f"{field} is missing or empty")
    takeaways = data.get("key_takeaways")
    if not _object_list(takeaways, ("headline", "so_what")) or not 2 <= len(takeaways) <= 4:
        problems.append("key_takeaways must be two to four items, each with a headline and a so_what")
    periods = data.get("timeline_periods")
    if not _string_list(periods) or not 2 <= len(periods) <= 5 or not all(p.strip() for p in periods):
        problems.append("timeline_periods must be two to five non-empty labels")
        periods = None
    themes = data.get("themes")
    if not isinstance(themes, list) or not 1 <= len(themes) <= 6:
        problems.append("themes must be a list of one to six themes")
        themes = []
    for i, theme in enumerate(themes):
        where = f"theme {i + 1}"
        if not isinstance(theme, dict):
            problems.append(f"{where}: must be an object")
            continue
        for field in ("name", "what_people_want", "strength_reason", "fit_note"):
            if not _is_text(theme.get(field)):
                problems.append(f"{where}: {field} is missing or empty")
        if theme.get("category") not in CATEGORIES:
            problems.append(f"{where}: category must be one of {list(CATEGORIES)}")
        if theme.get("strength") not in BRIEF_STRENGTHS:
            problems.append(f"{where}: strength must be one of {list(BRIEF_STRENGTHS)}")
        if theme.get("timeframe") not in BRIEF_TIMEFRAMES:
            problems.append(f"{where}: timeframe must be one of {sorted(BRIEF_TIMEFRAMES)}")
        if theme.get("momentum") not in MOMENTUM:
            problems.append(f"{where}: momentum must be one of {sorted(MOMENTUM)}")
        for score in ("attractiveness", "fit"):
            if not _score(theme.get(score)):
                problems.append(f"{where}: {score} must be a whole number from 1 to 5")
        history = theme.get("history")
        if not isinstance(history, list) or not all(isinstance(h, dict) for h in history):
            problems.append(f"{where}: history must be a list")
        else:
            if periods is not None and [h.get("period") for h in history] != periods:
                problems.append(f"{where}: history must have one entry per timeline period, in the same order: {periods}")
            if any(h.get("level") not in HISTORY_LEVELS for h in history):
                problems.append(f"{where}: each history level must be one of {list(HISTORY_LEVELS)}")
        outlook = theme.get("outlook")
        if not isinstance(outlook, dict) or outlook.get("level") not in OUTLOOK_LEVELS or not _is_text(outlook.get("reason")):
            problems.append(f"{where}: outlook needs a level from {list(OUTLOOK_LEVELS)} and a reason")
        basis = theme.get("basis")
        if not isinstance(basis, list) or not basis or not set(basis) <= BRIEF_BASES:
            problems.append(f"{where}: basis must list one or more of {sorted(BRIEF_BASES)}")
            basis = []
        sources = theme.get("sources")
        if not isinstance(sources, list):
            problems.append(f"{where}: sources must be a list")
            sources = []
        for source in sources:
            if not isinstance(source, dict) or not _is_text(source.get("title")) or not _is_text(source.get("url")):
                problems.append(f"{where}: every source needs a title and a url")
            elif allowed_urls is not None and normalise_url(source["url"]) not in allowed_urls:
                problems.append(
                    f"{where}: source {source['url']} is not one of the research findings; "
                    "cite only URLs that appear in the findings"
                )
        if "web_research" in basis and not sources:
            problems.append(f"{where}: a theme resting on web research must list its sources")
    if not _object_list(data.get("market_shifts"), ("from", "to", "evidence")):
        problems.append("market_shifts must be a list of items with from, to and evidence")
    season_view = data.get("season_view")
    if not isinstance(season_view, dict):
        problems.append("season_view must be an object")
    else:
        for key in ("returns_every_year", "new_this_year", "fading"):
            if not _string_list(season_view.get(key)):
                problems.append(f"season_view.{key} must be a list of short strings")
    if not _object_list(data.get("beyond_current_range"), ("opportunity", "why", "route")):
        problems.append("beyond_current_range must be a list of items with opportunity, why and route")
    implications = data.get("implications")
    if not _object_list(implications, ("title", "action")) or not 1 <= len(implications) <= 6:
        problems.append("implications must be one to six items, each with a title and an action")
    direction = data.get("direction")
    if not isinstance(direction, dict) or direction.get("verdict") not in DIRECTION_VERDICTS:
        problems.append(f"direction.verdict must be one of {sorted(DIRECTION_VERDICTS)}")
    if not _string_list(data.get("gaps")):
        problems.append("gaps must be a list of strings")
    if problems:
        raise SchemaError(problems)
    return data


def _validate_piece(piece, where, problems):
    if not isinstance(piece, dict):
        problems.append(f"{where}: must be an object")
        return
    for field in PIECE_TEXT_FIELDS:
        if not _is_text(piece.get(field)):
            problems.append(f"{where}: {field} is missing or empty")
    if piece.get("role") not in ROLES:
        problems.append(f"{where}: role must be one of {sorted(ROLES)}")
    if piece.get("production_route") not in ROUTES:
        problems.append(f"{where}: production_route must be one of {sorted(ROUTES)}")
    price = piece.get("target_price_eur")
    if not _is_number(price) or price <= 0:
        problems.append(f"{where}: target_price_eur must be a number above zero")
    colours = piece.get("colours")
    if not isinstance(colours, list) or not colours:
        problems.append(f"{where}: colours must be a non-empty list")
    else:
        for colour in colours:
            if not isinstance(colour, dict) or not _is_text(colour.get("name")):
                problems.append(f"{where}: each colour needs a name")
            elif not HEX.match(str(colour.get("hex", ""))):
                problems.append(f"{where}: colour {colour.get('name')!r} needs a hex value like #A1B2C3")
    dimensions = piece.get("dimensions_cm")
    if not isinstance(dimensions, dict) or not dimensions:
        problems.append(f"{where}: dimensions_cm must list at least one measurement")
    evidence = piece.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        problems.append(f"{where}: evidence must name at least one demand theme")


def validate_concept(concept, where="concept"):
    """Validate one concept object. Returns the list of problems found."""
    problems = []
    if not isinstance(concept, dict):
        return [f"{where}: must be an object"]
    for field in ("concept_id", "name", "theme", "target_customer", "signature_detail", "why_now", "basis"):
        if not _is_text(concept.get(field)):
            problems.append(f"{where}: {field} is missing or empty")
    palette = concept.get("palette")
    if not isinstance(palette, list) or not palette:
        problems.append(f"{where}: palette must be a non-empty list")
    else:
        for colour in palette:
            if not isinstance(colour, dict) or not HEX.match(str(colour.get("hex", ""))):
                problems.append(f"{where}: every palette colour needs a name and a hex value")
                break
    pieces = concept.get("pieces")
    if not isinstance(pieces, list) or not 4 <= len(pieces) <= 6:
        problems.append(f"{where}: must have four to six pieces")
        return problems
    for i, piece in enumerate(pieces):
        _validate_piece(piece, f"{where}, piece {i + 1}", problems)
    roles = [p.get("role") for p in pieces if isinstance(p, dict)]
    if roles.count("hero") != 1:
        problems.append(f"{where}: must have exactly one hero piece")
    if roles.count("accessory") < 1:
        problems.append(f"{where}: must have at least one accessory")
    ids = [p.get("piece_id") for p in pieces if isinstance(p, dict)]
    if len(set(ids)) != len(ids):
        problems.append(f"{where}: piece_id values must be unique")
    return problems


def validate_concepts(data):
    if not isinstance(data, dict) or not isinstance(data.get("concepts"), list):
        raise SchemaError(['Expected an object with a "concepts" list.'])
    concepts = data["concepts"]
    problems = []
    if len(concepts) != 3:
        problems.append(f"Expected exactly three concepts, got {len(concepts)}.")
    for i, concept in enumerate(concepts):
        problems.extend(validate_concept(concept, f"concept {i + 1}"))
    ids = [c.get("concept_id") for c in concepts if isinstance(c, dict)]
    if len(set(ids)) != len(ids):
        problems.append("concept_id values must be unique.")
    if problems:
        raise SchemaError(problems)
    return data


PLAN_PIECE_FIELDS = ("piece_id", "name", "category", "technique", "idea")


def validate_concept_plans(data):
    """Validate the three concept plans from the planning step."""
    if not isinstance(data, dict) or not isinstance(data.get("concepts"), list):
        raise SchemaError(['Expected an object with a "concepts" list.'])
    concepts = data["concepts"]
    problems = []
    if len(concepts) != 3:
        problems.append(f"Expected exactly three concepts, got {len(concepts)}.")
    for i, concept in enumerate(concepts):
        where = f"concept {i + 1}"
        if not isinstance(concept, dict):
            problems.append(f"{where}: must be an object")
            continue
        for field in ("concept_id", "name", "theme", "target_customer", "signature_detail", "why_now", "basis"):
            if not _is_text(concept.get(field)):
                problems.append(f"{where}: {field} is missing or empty")
        palette = concept.get("palette")
        if not isinstance(palette, list) or not palette or not all(
            isinstance(c, dict) and _is_text(c.get("name")) and HEX.match(str(c.get("hex", ""))) for c in palette
        ):
            problems.append(f"{where}: every palette colour needs a name and a hex value like #A1B2C3")
        pieces = concept.get("pieces")
        if not isinstance(pieces, list) or not 4 <= len(pieces) <= 6:
            problems.append(f"{where}: must have four to six pieces")
            continue
        for j, piece in enumerate(pieces):
            piece_where = f"{where}, piece {j + 1}"
            if not isinstance(piece, dict):
                problems.append(f"{piece_where}: must be an object")
                continue
            for field in PLAN_PIECE_FIELDS:
                if not _is_text(piece.get(field)):
                    problems.append(f"{piece_where}: {field} is missing or empty")
            if piece.get("role") not in ROLES:
                problems.append(f"{piece_where}: role must be one of {sorted(ROLES)}")
            if piece.get("production_route") not in ROUTES:
                problems.append(f"{piece_where}: production_route must be one of {sorted(ROUTES)}")
            price = piece.get("target_price_eur")
            if not _is_number(price) or price <= 0:
                problems.append(f"{piece_where}: target_price_eur must be a number above zero")
        roles = [p.get("role") for p in pieces if isinstance(p, dict)]
        if roles.count("hero") != 1:
            problems.append(f"{where}: must have exactly one hero piece")
        if roles.count("accessory") < 1:
            problems.append(f"{where}: must have at least one accessory")
        ids = [p.get("piece_id") for p in pieces if isinstance(p, dict)]
        if len(set(ids)) != len(ids):
            problems.append(f"{where}: piece_id values must be unique")
    ids = [c.get("concept_id") for c in concepts if isinstance(c, dict)]
    if len(set(ids)) != len(ids):
        problems.append("concept_id values must be unique.")
    if problems:
        raise SchemaError(problems)
    return data


def validate_concept_against_plan(data, plan):
    """Validate a fully specified concept and check it kept to its plan."""
    problems = validate_concept(data)
    if isinstance(data, dict) and not problems:
        if data.get("concept_id") != plan["concept_id"]:
            problems.append(f"concept_id must stay {plan['concept_id']}")
        planned = {p["piece_id"] for p in plan["pieces"]}
        written = {p.get("piece_id") for p in data.get("pieces", [])}
        if planned != written:
            problems.append(
                "the pieces must be exactly those in the plan: "
                f"expected {sorted(planned)}, got {sorted(str(w) for w in written)}"
            )
    if problems:
        raise SchemaError(problems)
    return data


def validate_single_concept(data):
    problems = validate_concept(data)
    if problems:
        raise SchemaError(problems)
    return data


def validate_review(data):
    problems = []
    if not isinstance(data, dict):
        raise SchemaError(["Expected a JSON object for the review."])
    scores = data.get("scores")
    if not isinstance(scores, dict):
        problems.append("scores must be an object")
    else:
        for criterion in REVIEW_CRITERIA:
            value = scores.get(criterion)
            if not _is_number(value) or not 1 <= value <= 5:
                problems.append(f"scores.{criterion} must be a number from 1 to 5")
    if not isinstance(data.get("reasons"), dict):
        problems.append("reasons must be an object")
    for field in ("blocking_issues", "risks"):
        if not isinstance(data.get(field), list):
            problems.append(f"{field} must be a list")
    if data.get("overall") not in REVIEW_OVERALL:
        problems.append(f"overall must be one of {sorted(REVIEW_OVERALL)}")
    if problems:
        raise SchemaError(problems)
    return data


def _piece_text(piece) -> str:
    parts = []
    for value in piece.values():
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


def code_blocking_issues(concept) -> list:
    """Rules enforced in code, whatever the reviewer model says.

    1. A hand technique cannot be assigned to an on-demand machine supplier.
    2. Only atelier pieces may be described as handmade.
    """
    issues = []
    for piece in concept.get("pieces", []):
        if not isinstance(piece, dict):
            continue
        label = f"{piece.get('piece_id', '?')} {piece.get('name', '')}".strip()
        route = piece.get("production_route")
        technique = str(piece.get("technique", "")).lower()
        if route == "on_demand" and ("hand" in technique or "crochet" in technique):
            issues.append(
                f"{label}: technique '{piece.get('technique')}' cannot be made by an on-demand "
                "machine supplier. Use the atelier or specialist route, or change the technique."
            )
        if route != "atelier" and HANDMADE_WORDS.search(_piece_text(piece)):
            issues.append(
                f"{label}: described as handmade, but only atelier pieces may be called handmade."
            )
    return issues


# --- Stage 3: sourcing check -------------------------------------------------

SOURCING_STATUSES = ("makeable_as_specified", "makeable_with_changes", "needs_sourcing")
CURRENCY = re.compile(r"^[A-Z]{3}$")


def _hand_technique(technique) -> bool:
    text = str(technique or "").lower()
    return "hand" in text or "crochet" in text


def validate_sourcing(data, pieces, suppliers):
    """Validate the sourcing check against the pieces and the supplier table it was given.

    ``pieces`` maps piece_id to the piece specification. ``suppliers`` is the
    list of supplier dictionaries that were sent. A supplier the table does
    not contain, a piece left out, or a hand technique given to a machine
    supplier is sent back to the model to correct.
    """
    if not isinstance(data, dict) or not isinstance(data.get("pieces"), list):
        raise SchemaError(['Expected an object with a "pieces" list.'])
    problems = []
    if not _is_text(data.get("collection_summary")):
        problems.append("collection_summary is missing or empty")
    routes_by_name = {s["name"].strip().lower(): s["route"] for s in suppliers}
    seen = []
    for i, item in enumerate(data["pieces"]):
        where = f"piece entry {i + 1}"
        if not isinstance(item, dict):
            problems.append(f"{where}: must be an object")
            continue
        piece_id = item.get("piece_id")
        if piece_id not in pieces:
            problems.append(f"{where}: piece_id {piece_id!r} is not one of the approved pieces {sorted(pieces)}")
            continue
        where = f"piece {piece_id}"
        seen.append(piece_id)
        route = item.get("route")
        status = item.get("status")
        if route not in ROUTES:
            problems.append(f"{where}: route must be one of {sorted(ROUTES)}")
        if status not in SOURCING_STATUSES:
            problems.append(f"{where}: status must be one of {list(SOURCING_STATUSES)}")
        if route == "on_demand" and _hand_technique(pieces[piece_id].get("technique")):
            problems.append(
                f"{where}: the technique '{pieces[piece_id].get('technique')}' cannot be made by an "
                "on-demand machine supplier; use atelier or specialist"
            )
        changes = item.get("changes")
        if not _string_list(changes):
            problems.append(f"{where}: changes must be a list of strings")
        elif status == "makeable_with_changes" and not [c for c in changes if c.strip()]:
            problems.append(f"{where}: makeable_with_changes must list the changes")
        if not _string_list(item.get("questions_for_supplier")):
            problems.append(f"{where}: questions_for_supplier must be a list of strings")
        candidates = item.get("candidate_suppliers")
        if not isinstance(candidates, list):
            problems.append(f"{where}: candidate_suppliers must be a list")
            candidates = []
        matching_route = 0
        for candidate in candidates:
            name = candidate.get("name") if isinstance(candidate, dict) else None
            if not _is_text(name):
                problems.append(f"{where}: every candidate supplier needs a name")
                continue
            table_route = routes_by_name.get(name.strip().lower())
            if table_route is None:
                problems.append(
                    f"{where}: supplier {name!r} is not in the supplier table; name only suppliers "
                    "from the table, written exactly as there"
                )
            elif table_route == route:
                matching_route += 1
        if route == "atelier" and candidates:
            problems.append(f"{where}: an atelier piece is made by Malina, so candidate_suppliers must be empty")
        if route in ("on_demand", "specialist") and status in SOURCING_STATUSES[:2] and not matching_route:
            problems.append(
                f"{where}: a {route} piece can only be {status} if a supplier in the table with route "
                f"{route} can make it; otherwise use needs_sourcing"
            )
        cost = item.get("unit_cost")
        if cost is not None and (not _is_number(cost) or cost <= 0):
            problems.append(f"{where}: unit_cost must be null or a number above zero")
        if cost is not None and not CURRENCY.match(str(item.get("currency") or "")):
            problems.append(f"{where}: currency must be a three-letter code such as EUR when unit_cost is given")
    missing = sorted(set(pieces) - set(seen))
    if missing:
        problems.append(f"every approved piece needs exactly one entry; missing: {missing}")
    duplicates = sorted({p for p in seen if seen.count(p) > 1})
    if duplicates:
        problems.append(f"each piece must appear once; repeated: {duplicates}")
    if problems:
        raise SchemaError(problems)
    return data


def sourcing_code_checks(item, piece, suppliers, eur_rates, min_margin):
    """Rules applied in code to one validated sourcing entry, whatever the model concluded.

    The unit cost is kept only if the number is written in the price text of
    a supplier the model named; the margin is computed here, never taken from
    the model; and anything the founder should know is returned as a warning.

    Returns a dict with unit_cost, currency, margin and warnings.
    """
    from decimal import Decimal, ROUND_HALF_UP

    warnings = []
    named = {
        c["name"].strip().lower()
        for c in item.get("candidate_suppliers", [])
        if isinstance(c, dict) and _is_text(c.get("name"))
    }
    named_suppliers = [s for s in suppliers if s["name"].strip().lower() in named]

    unit_cost, currency = None, ""
    cost = item.get("unit_cost")
    if cost is not None:
        source = next((s for s in named_suppliers if number_in_text(cost, s["prices"])), None)
        if source is None:
            warnings.append(
                f"The model gave a unit cost of {cost} {item.get('currency', '')}, but that number is not in "
                "the price of any supplier it named, so it was not used."
            )
        else:
            unit_cost = Decimal(str(cost)).quantize(Decimal("0.01"))
            currency = source["price_currency"]

    price = Decimal(str(piece["target_price_eur"]))
    margin = None
    if unit_cost is not None:
        rate = eur_rates.get(currency)
        if rate is None:
            warnings.append(
                f"The cost is in {currency} and no exchange rate is set, so the margin was not worked out. "
                f"Add {currency} to FDW_EUR_RATES to see it."
            )
        elif price > 0:
            margin = ((price - unit_cost * rate) / price).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            if margin <= 0:
                warnings.append("At this cost the piece sells at a loss.")
            elif margin < min_margin:
                warnings.append(
                    f"Gross margin of {round(margin * 100)}% is below your {round(min_margin * 100)}% threshold. "
                    "Shipping, payment fees and returns are not included."
                )
    elif item.get("route") in ("on_demand", "specialist") and item.get("status") != "needs_sourcing":
        warnings.append("No unit cost could be traced to the supplier table, so the margin is unknown.")

    specified = piece.get("production_route")
    if item.get("route") != specified:
        warnings.append(f"Route changed from {specified} to {item.get('route')}.")
        if specified == "atelier" and HANDMADE_WORDS.search(_piece_text(piece)):
            warnings.append("The specification calls the piece handmade, which will no longer be true.")
    if item.get("status") == "needs_sourcing":
        warnings.append(
            "No supplier in your table can make this. It can go on to images only if you accept the risk, "
            "and then only to the waitlist until a supplier is confirmed."
        )
    return {"unit_cost": unit_cost, "currency": currency, "margin": margin, "warnings": warnings}
