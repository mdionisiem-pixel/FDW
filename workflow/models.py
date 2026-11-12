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
        total = self.model_calls.aggregate(total=Sum("cost_usd"))["total"]
        return total or Decimal("0")

    @property
    def approved_brief(self):
        return self.briefs.filter(status=DemandBrief.Status.APPROVED).order_by("-created_at").first()

    @property
    def latest_brief(self):
        return self.briefs.order_by("-created_at").first()

    @property
    def stage_number(self) -> int:
        """Which of the seven stages the cycle is in (slice 1 covers 1 and 2)."""
        if self.status in (self.Status.NEW, self.Status.BRIEF_PENDING, self.Status.BRIEF_REJECTED):
            return 1
        if self.status in (self.Status.BRIEF_APPROVED, self.Status.CONCEPTS_PENDING):
            return 2
        return 3


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
