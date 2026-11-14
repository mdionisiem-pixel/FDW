"""Data model for slice 1: demand signals (stage 1) and collection concepts (stage 2).

All workflow state lives here, so a cycle can wait at an approval gate for
days and resume, and every output can be traced to the calls that produced it.
"""
from decimal import Decimal

from django.db import models
from django.db.models import Sum

DEFAULT_BRAND_BRIEF = """BRAND BRIEF: MALINA

What Malina is: a fashion label built on craft techniques (hand embroidery, crochet, hand knitting) that designs unique, luxury pieces and proves demand before producing them.

Two production tiers, always labelled:
- Atelier: made by hand by Malina. May be described as handmade.
- Studio: designed by Malina, made by a named type of supplier (for example "machine-knitted in a partner workshop"). Never described as handmade.

Audience: 1,000 to 5,000 followers who came for handmade work and already ask to buy. They ask about embroidered clothing, crochet clothing, accessories and custom pieces.

Positioning: luxury means material quality, finish and scarcity. Malina follows demand wherever the market shows it, including new categories; pieces outside the current techniques can be outsourced.

Signature elements (To confirm): motifs, stitches or shapes that recur across pieces.
Colour palette (To confirm): named colours with hex values.
Materials Malina will and will not use (To confirm).
Price bands in euros for accessories, core pieces and hero pieces (To confirm).
Markets and languages for launch (To confirm).

Hard rules:
- Never claim a piece is handmade unless its tier is Atelier.
- Never state a fibre, origin, certification or sustainability claim that is not in the piece specification.
- Every image or 3D view of a piece that does not physically exist yet is labelled "Concept render. The finished piece may differ slightly."
"""


class BrandBrief(models.Model):
    """The single brand brief inserted into every prompt."""

    text = models.TextField(default=DEFAULT_BRAND_BRIEF)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "brand brief"
        verbose_name_plural = "brand brief"

    def __str__(self):
        return "Brand brief"

    @classmethod
    def current(cls) -> "BrandBrief":
        brief, _ = cls.objects.get_or_create(pk=1)
        return brief

    @property
    def unconfirmed_lines(self) -> int:
        return self.text.count("(To confirm)")


# Model calls whose step starts with this belong to stage 4 and count toward its budget.
VISUAL_STEP_PREFIX = "visual_"


class Cycle(models.Model):
    """One pass from market research to a chosen collection."""

    class Season(models.TextChoices):
        SPRING_SUMMER = "spring_summer", "Spring/Summer"
        AUTUMN_WINTER = "autumn_winter", "Autumn/Winter"
        YEAR_ROUND = "year_round", "Year-round"

    class Status(models.TextChoices):
        NEW = "new", "Ready for market research"
        BRIEF_PENDING = "brief_pending", "Research brief awaiting approval"
        BRIEF_APPROVED = "brief_approved", "Research brief approved"
        BRIEF_REJECTED = "brief_rejected", "Research brief rejected"
        CONCEPTS_PENDING = "concepts_pending", "Concepts awaiting selection"
        CONCEPTS_SELECTED = "concepts_selected", "Collection selected"
        SOURCING_PENDING = "sourcing_pending", "Sourcing sheet awaiting approval"
        SOURCING_APPROVED = "sourcing_approved", "Sourcing approved"
        VISUALS_IN_PROGRESS = "visuals_in_progress", "Visual prototypes in progress"
        VISUALS_APPROVED = "visuals_approved", "Visual prototypes approved"

    name = models.CharField(max_length=120)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.NEW)
    language = models.CharField(max_length=40, default="English")
    direction = models.TextField(
        blank=True,
        help_text="Optional. The founder's own direction or ideas for this collection.",
    )
    season = models.CharField(max_length=16, choices=Season.choices, blank=True)
    season_year = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def season_label(self) -> str:
        """The target season in words, or an empty string when none was chosen."""
        if not self.season:
            return ""
        label = self.get_season_display()
        if self.season_year and self.season != self.Season.YEAR_ROUND:
            label = f"{label} {self.season_year}"
        return label

    @property
    def spend_usd(self) -> Decimal:
        """Claude spend for stages 1 to 3, held to FDW_CYCLE_LIMIT_USD.

        Stage 4 has its own budget, FDW_VISUALS_LIMIT_USD, because images cost
        far more than text; see visuals_spend_usd.
        """
        total = self.model_calls.exclude(step__startswith=VISUAL_STEP_PREFIX).aggregate(
            total=Sum("cost_usd"))["total"]
        return total or Decimal("0")

    @property
    def visuals_spend_usd(self) -> Decimal:
        """Everything stage 4 has cost: images as reported, plus the Claude calls that write and review them."""
        claude = self.model_calls.filter(step__startswith=VISUAL_STEP_PREFIX).aggregate(
            total=Sum("cost_usd"))["total"] or Decimal("0")
        images = ShotAttempt.objects.filter(visuals__cycle=self).aggregate(
            total=Sum("cost_usd"))["total"] or Decimal("0")
        return claude + images

    @property
    def approved_brief(self):
        return self.briefs.filter(status=DemandBrief.Status.APPROVED).order_by("-created_at").first()

    @property
    def latest_brief(self):
        return self.briefs.order_by("-created_at").first()

    @property
    def stage_number(self) -> int:
        """Which of the seven stages the cycle is in."""
        if self.status in (self.Status.NEW, self.Status.BRIEF_PENDING, self.Status.BRIEF_REJECTED):
            return 1
        if self.status in (self.Status.BRIEF_APPROVED, self.Status.CONCEPTS_PENDING):
            return 2
        if self.status in (self.Status.CONCEPTS_SELECTED, self.Status.SOURCING_PENDING):
            return 3
        if self.status in (self.Status.SOURCING_APPROVED, self.Status.VISUALS_IN_PROGRESS):
            return 4
        return 5

    @property
    def selected_pieces(self):
        return Piece.objects.filter(concept__cycle=self, concept__status=Concept.Status.SELECTED)

    @property
    def latest_sourcing(self):
        return self.sourcing_checks.order_by("-created_at", "-id").first()


class RawItem(models.Model):
    """One untouched piece of evidence: a DM, a comment, a metric, a trend line."""

    class Source(models.TextChoices):
        DM = "dm", "Direct message"
        COMMENT = "comment", "Comment"
        POST_METRIC = "post_metric", "Post performance"
        PINTEREST = "pinterest", "Pinterest trend"
        GOOGLE_TRENDS = "google_trends", "Google Trends"
        ARTICLE = "article", "Article or report"

    cycle = models.ForeignKey(Cycle, on_delete=models.CASCADE, related_name="raw_items")
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.DM)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.get_source_display()}: {self.text[:60]}"


class Signal(models.Model):
    """A structured signal extracted from raw items by the model."""

    cycle = models.ForeignKey(Cycle, on_delete=models.CASCADE, related_name="signals")
    source = models.CharField(max_length=16)
    date = models.CharField(max_length=20, blank=True)
    product_type = models.CharField(max_length=200, blank=True)
    technique = models.CharField(max_length=40, blank=True)
    attributes = models.JSONField(default=list, blank=True)
    intent = models.CharField(max_length=12)
    strength = models.FloatField(null=True, blank=True)
    quote = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.intent}: {self.product_type or 'unspecified'}"


class TrendFinding(models.Model):
    """One finding from web research, tied to the page it came from."""

    cycle = models.ForeignKey(Cycle, on_delete=models.CASCADE, related_name="findings")
    theme = models.CharField(max_length=200)
    what_people_want = models.TextField()
    evidence = models.TextField()
    source_title = models.CharField(max_length=300, blank=True)
    source_url = models.URLField(max_length=1000)
    source_date = models.CharField(max_length=20, blank=True)
    relevance = models.TextField(blank=True)
    # recurring: seen in the same season in earlier years. current: rising now. both: each.
    timeframe = models.CharField(max_length=10, blank=True)
    seasons_observed = models.CharField(max_length=200, blank=True)
    category = models.CharField(max_length=20, blank=True)
    momentum = models.CharField(max_length=10, blank=True)
    # core: Malina's current techniques. adjacent: with a partner. outside: new category or supplier.
    malina_fit = models.CharField(max_length=10, blank=True)
    # Only figures whose number appears in the evidence text are stored.
    figures = models.JSONField(default=list, blank=True)
    forecast = models.JSONField(null=True, blank=True)
    # True only when the URL was among the search results the model received.
    verified = models.BooleanField(default=False)

    class Meta:
        ordering = ["-verified", "id"]

    def __str__(self):
        return self.theme


class DemandBrief(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    cycle = models.ForeignKey(Cycle, on_delete=models.CASCADE, related_name="briefs")
    # ``data`` is the structured brief shown on screen. ``text`` is the same
    # content as plain text, which is what the designer step reads.
    data = models.JSONField(null=True, blank=True)
    text = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    founder_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Demand brief for {self.cycle} ({self.get_status_display()})"


class Concept(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        UNRESOLVED = "unresolved", "Proposed with unresolved issues"
        SELECTED = "selected", "Selected"
        NOT_SELECTED = "not_selected", "Not selected"

    cycle = models.ForeignKey(Cycle, on_delete=models.CASCADE, related_name="concepts")
    concept_id = models.CharField(max_length=12)
    name = models.CharField(max_length=200)
    data = models.JSONField()
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.PROPOSED)
    revision_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["concept_id"]
        constraints = [
            models.UniqueConstraint(fields=["cycle", "concept_id"], name="one_concept_id_per_cycle"),
        ]

    def __str__(self):
        return f"{self.concept_id} {self.name}"

    @property
    def latest_review(self):
        return self.reviews.order_by("-created_at", "-id").first()

    @property
    def price_range(self):
        prices = [p.get("target_price_eur") for p in self.data.get("pieces", [])]
        prices = [p for p in prices if isinstance(p, (int, float))]
        return (min(prices), max(prices)) if prices else None


class ConceptReview(models.Model):
    concept = models.ForeignKey(Concept, on_delete=models.CASCADE, related_name="reviews")
    data = models.JSONField()
    overall = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Review of {self.concept}: {self.overall}"

    @property
    def average_score(self):
        scores = [v for v in self.data.get("scores", {}).values() if isinstance(v, (int, float))]
        return round(sum(scores) / len(scores), 1) if scores else None


class Piece(models.Model):
    """An approved piece. Its specification is the single source for all later stages."""

    concept = models.ForeignKey(Concept, on_delete=models.CASCADE, related_name="pieces")
    piece_id = models.CharField(max_length=20)
    name = models.CharField(max_length=200)
    role = models.CharField(max_length=12)
    production_route = models.CharField(max_length=12)
    target_price_eur = models.DecimalField(max_digits=9, decimal_places=2)
    spec = models.JSONField()

    class Meta:
        ordering = ["piece_id"]

    def __str__(self):
        return f"{self.piece_id} {self.name}"

    @property
    def tier(self) -> str:
        return "Atelier" if self.production_route == "atelier" else "Studio"


class Approval(models.Model):
    """A decision the founder made at a gate. Nothing moves forward without one."""

    cycle = models.ForeignKey(Cycle, on_delete=models.CASCADE, related_name="approvals")
    stage = models.PositiveSmallIntegerField()
    item = models.CharField(max_length=200)
    decision = models.CharField(max_length=20)
    notes = models.TextField(blank=True)
    decided_by = models.CharField(max_length=150, blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-decided_at"]

    def __str__(self):
        return f"Stage {self.stage}: {self.decision} ({self.item})"


class ModelCall(models.Model):
    """One call to a language model, kept for tracing and for the spend limit."""

    cycle = models.ForeignKey(Cycle, on_delete=models.CASCADE, related_name="model_calls")
    step = models.CharField(max_length=40)
    model = models.CharField(max_length=60)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    web_searches = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    prompt = models.TextField()
    response = models.TextField(blank=True)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True)
    dry_run = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.step} ({self.model})"


# --- Stage 3: sourcing check -------------------------------------------------


class Supplier(models.Model):
    """One supplier the founder has looked up. The sourcing check uses no other supplier facts.

    Every field is something the founder read on the supplier's own page or
    heard from the supplier. Leave a field empty rather than guess: the
    sourcing check is told to treat an empty field as unknown.
    """

    class Route(models.TextChoices):
        ON_DEMAND = "on_demand", "On-demand supplier"
        SPECIALIST = "specialist", "Specialist manufacturer"

    name = models.CharField(max_length=120, unique=True)
    route = models.CharField(max_length=12, choices=Route.choices)
    makes = models.TextField(help_text="Product types and categories the supplier states it makes.")
    techniques = models.TextField(blank=True, help_text="Techniques and machines, for example 4-colour jacquard knit, cut and sew.")
    materials = models.TextField(blank=True, help_text="Fibres and fabrics offered, as stated. Empty means not stated.")
    limits = models.TextField(blank=True, help_text="Colour limits, size range, anything it cannot do.")
    minimum_order = models.CharField(max_length=120, blank=True)
    lead_time = models.CharField(max_length=120, blank=True)
    prices = models.TextField(blank=True, help_text="Unit prices as stated, with what they are for. Empty means not stated.")
    price_currency = models.CharField(max_length=3, default="EUR")
    location = models.CharField(max_length=120, blank=True)
    source_url = models.URLField(max_length=500, blank=True)
    checked_on = models.DateField(null=True, blank=True, help_text="When you last read the source.")
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True, help_text="Only active suppliers are sent to the sourcing check.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["route", "name"]

    def __str__(self):
        return self.name

    def as_prompt_data(self) -> dict:
        """The supplier as the sourcing check sees it. Empty fields are sent as 'not stated'."""
        def value(text):
            return text.strip() if text and text.strip() else "not stated"

        return {
            "name": self.name,
            "route": self.route,
            "makes": value(self.makes),
            "techniques": value(self.techniques),
            "materials": value(self.materials),
            "limits": value(self.limits),
            "minimum_order": value(self.minimum_order),
            "lead_time": value(self.lead_time),
            "prices": value(self.prices),
            "price_currency": self.price_currency,
            "location": value(self.location),
            "checked_on": self.checked_on.isoformat() if self.checked_on else "not stated",
        }


class SourcingCheck(models.Model):
    """One run of the sourcing check for a cycle's approved pieces."""

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting your decision"
        APPROVED = "approved", "Approved"
        SUPERSEDED = "superseded", "Replaced by a later check"

    cycle = models.ForeignKey(Cycle, on_delete=models.CASCADE, related_name="sourcing_checks")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    summary = models.TextField()
    # The supplier table exactly as it was sent, so the result can be traced
    # even after the founder edits a supplier.
    suppliers_sent = models.JSONField(default=list)
    founder_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Sourcing check for {self.cycle} ({self.get_status_display()})"


class SourcingResult(models.Model):
    """The sourcing verdict for one piece, and the founder's decision on it."""

    class Verdict(models.TextChoices):
        AS_SPECIFIED = "makeable_as_specified", "Makeable as specified"
        WITH_CHANGES = "makeable_with_changes", "Makeable with changes"
        NEEDS_SOURCING = "needs_sourcing", "Needs a supplier"

    class Decision(models.TextChoices):
        UNDECIDED = "", "Not decided"
        APPROVED = "approved", "Continue"
        ACCEPT_RISK = "accept_risk", "Continue without a supplier (waitlist only)"
        DROPPED = "dropped", "Drop from the collection"

    sourcing_check = models.ForeignKey(SourcingCheck, on_delete=models.CASCADE, related_name="results")
    piece = models.ForeignKey(Piece, on_delete=models.CASCADE, related_name="sourcing_results")
    route = models.CharField(max_length=12)
    verdict = models.CharField(max_length=24, choices=Verdict.choices)
    changes = models.JSONField(default=list, blank=True)
    candidate_suppliers = models.JSONField(default=list, blank=True)
    questions = models.JSONField(default=list, blank=True)
    # Unit cost and margin are kept only when code could trace the cost to a
    # price in the supplier table; the model's own arithmetic is kept as text.
    unit_cost = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    cost_basis = models.TextField(blank=True)
    margin = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    decision = models.CharField(max_length=12, choices=Decision.choices, blank=True, default="")

    class Meta:
        ordering = ["piece__piece_id"]
        constraints = [
            models.UniqueConstraint(fields=["sourcing_check", "piece"], name="one_result_per_piece_per_check"),
        ]

    def __str__(self):
        return f"{self.piece}: {self.get_verdict_display()}"

    @property
    def margin_percent(self):
        return None if self.margin is None else round(self.margin * 100)

    @property
    def continues(self) -> bool:
        return self.decision in (self.Decision.APPROVED, self.Decision.ACCEPT_RISK)

    @property
    def waitlist_only(self) -> bool:
        """A piece without a confirmed supplier may be shown, but never pre-sold."""
        return self.decision == self.Decision.ACCEPT_RISK


# --- Image model bake-off ------------------------------------------------------


class BakeoffPiece(models.Model):
    """A finished Malina piece, photographed, used to compare image models."""

    name = models.CharField(max_length=120)
    notes = models.TextField(blank=True, help_text="Optional facts the photos do not show: fibre, size, technique.")
    photo_front = models.FileField(upload_to="bakeoff/photos/")
    photo_back = models.FileField(upload_to="bakeoff/photos/", blank=True)
    photo_detail = models.FileField(upload_to="bakeoff/photos/", blank=True)
    photo_worn = models.FileField(upload_to="bakeoff/photos/", blank=True)
    # Written by Claude from the photos: description, checklist and shot prompts.
    spec = models.JSONField(null=True, blank=True)
    prompt_cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return self.name

    def photos(self):
        """(field name, label, file) for every photo that was uploaded."""
        labels = (("photo_front", "Front"), ("photo_back", "Back"),
                  ("photo_detail", "Detail"), ("photo_worn", "Worn"))
        return [(field, label, getattr(self, field)) for field, label in labels if getattr(self, field)]


class BakeoffImage(models.Model):
    """One image made by one model for one shot of a piece, and the founder's scores for it."""

    class Shot(models.TextChoices):
        FRONT = "A", "Front packshot"
        DETAIL = "C", "Detail close-up"
        WORN = "D", "On a model"

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        DONE = "done", "Made"
        FAILED = "failed", "Failed"

    SCORE_FIELDS = ("matches_piece", "material_realism", "colour_accuracy", "photo_realism")

    piece = models.ForeignKey(BakeoffPiece, on_delete=models.CASCADE, related_name="images")
    model = models.CharField(max_length=120)
    shot = models.CharField(max_length=1, choices=Shot.choices)
    # A neutral label shown while scoring, so the model's name cannot sway the score.
    code = models.CharField(max_length=4)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    prompt = models.TextField(blank=True)
    file = models.FileField(upload_to="bakeoff/generated/", blank=True)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    error = models.TextField(blank=True)
    seconds = models.FloatField(null=True, blank=True)
    matches_piece = models.PositiveSmallIntegerField(null=True, blank=True)
    material_realism = models.PositiveSmallIntegerField(null=True, blank=True)
    colour_accuracy = models.PositiveSmallIntegerField(null=True, blank=True)
    photo_realism = models.PositiveSmallIntegerField(null=True, blank=True)
    scored_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["shot", "code"]
        constraints = [
            models.UniqueConstraint(fields=["piece", "model", "shot"], name="one_image_per_model_and_shot"),
        ]

    def __str__(self):
        return f"{self.piece} {self.shot} {self.code}"

    @property
    def is_scored(self) -> bool:
        return all(getattr(self, f) is not None for f in self.SCORE_FIELDS)

    @property
    def average_score(self):
        values = [getattr(self, f) for f in self.SCORE_FIELDS if getattr(self, f) is not None]
        return round(sum(values) / len(values), 2) if values else None


# --- Stage 4: visual prototypes ------------------------------------------------

SHOTS = (
    ("A", "Front packshot"),
    ("B", "Back packshot"),
    ("C", "Detail macro"),
    ("D", "On-model, studio"),
    ("E", "On-model, lifestyle"),
)


class PieceVisuals(models.Model):
    """The five shots of one piece in one cycle, and the founder's decision on them at gate 4."""

    class Status(models.TextChoices):
        WRITING = "writing", "Writing shot prompts"
        MAKING = "making", "Making and reviewing images"
        READY = "ready", "Awaiting your decision"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    cycle = models.ForeignKey(Cycle, on_delete=models.CASCADE, related_name="visuals")
    piece = models.ForeignKey(Piece, on_delete=models.CASCADE, related_name="visuals")
    # A request for changes at gate 4 starts a new round; earlier rounds stay in the record.
    round = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.WRITING)
    # Written by the art director step.
    prompts = models.JSONField(default=dict, blank=True)
    checklist = models.JSONField(default=list, blank=True)
    missing_information = models.JSONField(default=list, blank=True)
    # Copied from gate 3, so the images show the piece as it will be made.
    accepted_changes = models.JSONField(default=list, blank=True)
    waitlist_only = models.BooleanField(default=False)
    founder_notes = models.TextField(blank=True)
    error = models.TextField(blank=True)
    # Set while a worker is writing the prompts, so two workers never write them twice.
    claimed_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["piece__piece_id"]
        constraints = [
            models.UniqueConstraint(fields=["cycle", "piece"], name="one_visual_set_per_piece"),
        ]

    def __str__(self):
        return f"{self.piece} visuals, round {self.round}"

    def current_attempts(self):
        return self.attempts.filter(round=self.round)


class ShotAttempt(models.Model):
    """One generated image for one shot, and the reviewer's verdict on it.

    Its key (piece set, round, shot, attempt) is unique, so a step that runs
    again after a failure finds the existing row instead of paying twice.
    """

    MAX_ERRORS = 2

    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting"
        RUNNING = "running", "Being made"
        PASSED = "passed", "Passed review"
        FAILED = "failed", "Failed review"

    visuals = models.ForeignKey(PieceVisuals, on_delete=models.CASCADE, related_name="attempts")
    round = models.PositiveSmallIntegerField(default=1)
    shot = models.CharField(max_length=1, choices=SHOTS)
    attempt = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.WAITING)
    prompt = models.TextField()
    model = models.CharField(max_length=120, blank=True)
    # Primary keys of the attempts sent to the image model as reference images.
    references = models.JSONField(default=list, blank=True)
    file = models.FileField(upload_to="visuals/", blank=True)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    cost_estimated = models.BooleanField(default=False)
    seconds = models.FloatField(null=True, blank=True)
    review = models.JSONField(null=True, blank=True)
    correction_note = models.TextField(blank=True)
    error = models.TextField(blank=True)
    error_count = models.PositiveSmallIntegerField(default=0)
    claimed_at = models.DateTimeField(null=True, blank=True)
    # The founder kept this image at gate 4 although it did not pass review.
    accepted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["round", "shot", "attempt"]
        constraints = [
            models.UniqueConstraint(fields=["visuals", "round", "shot", "attempt"], name="one_row_per_attempt"),
        ]

    def __str__(self):
        return f"{self.visuals.piece.piece_id} {self.shot} round {self.round} attempt {self.attempt}"

    @property
    def matches(self) -> int:
        results = (self.review or {}).get("checklist_results") or []
        return sum(1 for r in results if r.get("result") == "match")

    @property
    def mismatches(self) -> int:
        results = (self.review or {}).get("checklist_results") or []
        return sum(1 for r in results if r.get("result") == "mismatch")
