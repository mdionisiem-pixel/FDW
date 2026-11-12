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
    for field in ("concept_id", "name", "theme", "target_customer", "signature_detail", "why_now"):
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
