"""Admin: a plain view of every record, for inspection and repair.

Day-to-day work happens on the workflow screens. Records that capture a
decision or a model call are read-only here, so the audit trail stays intact.
"""
from django.contrib import admin

from .models import (
    Approval,
    BakeoffImage,
    BakeoffPiece,
    BrandBrief,
    Concept,
    ConceptReview,
    Cycle,
    DemandBrief,
    ModelCall,
    Piece,
    RawItem,
    Signal,
    TrendFinding,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BrandBrief)
class BrandBriefAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not BrandBrief.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class RawItemInline(admin.TabularInline):
    model = RawItem
    extra = 0


@admin.register(Cycle)
class CycleAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "spend_usd", "created_at")
    readonly_fields = ("status",)
    inlines = [RawItemInline]


@admin.register(Signal)
class SignalAdmin(ReadOnlyAdmin):
    list_display = ("cycle", "intent", "product_type", "technique", "source", "quote")
    list_filter = ("intent", "source", "cycle")


@admin.register(TrendFinding)
class TrendFindingAdmin(ReadOnlyAdmin):
    list_display = ("theme", "cycle", "timeframe", "source_title", "source_date", "verified")
    list_filter = ("verified", "cycle")


@admin.register(DemandBrief)
class DemandBriefAdmin(ReadOnlyAdmin):
    list_display = ("cycle", "status", "created_at", "decided_at")


@admin.register(Concept)
class ConceptAdmin(ReadOnlyAdmin):
    list_display = ("concept_id", "name", "cycle", "status", "revision_count")
    list_filter = ("status", "cycle")


@admin.register(ConceptReview)
class ConceptReviewAdmin(ReadOnlyAdmin):
    list_display = ("concept", "overall", "average_score", "created_at")


@admin.register(Piece)
class PieceAdmin(ReadOnlyAdmin):
    list_display = ("piece_id", "name", "role", "production_route", "target_price_eur", "concept")
    list_filter = ("production_route", "role")


@admin.register(Approval)
class ApprovalAdmin(ReadOnlyAdmin):
    list_display = ("cycle", "stage", "decision", "item", "decided_by", "decided_at")


@admin.register(ModelCall)
class ModelCallAdmin(ReadOnlyAdmin):
    list_display = ("step", "cycle", "model", "input_tokens", "output_tokens", "web_searches", "cost_usd", "ok", "dry_run", "created_at")
    list_filter = ("step", "ok", "dry_run", "cycle")


@admin.register(BakeoffPiece)
class BakeoffPieceAdmin(ReadOnlyAdmin):
    list_display = ("name", "prompt_cost_usd", "created_at")


@admin.register(BakeoffImage)
class BakeoffImageAdmin(ReadOnlyAdmin):
    list_display = ("piece", "shot", "code", "model", "status", "cost_usd", "matches_piece", "scored_at")
    list_filter = ("model", "status", "shot")
