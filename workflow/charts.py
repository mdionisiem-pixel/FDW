"""Data preparation and drawing for the market report dashboard.

Charts are drawn on the server as plain SVG and HTML, so they work offline,
need no chart library, and every label is escaped. Every number shown comes
from a research finding whose figure appears in the source's own text; the
judgement-based parts (opportunity scores, timeline levels, outlook) are
labelled as assessments on the page.
"""
from collections import Counter, defaultdict
from urllib.parse import urlparse

from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import mark_safe

from .schemas import CATEGORIES, FITS

CATEGORY_LABELS = {
    "knitwear": "Knitwear",
    "apparel": "Apparel",
    "outerwear": "Outerwear",
    "accessories": "Accessories",
    "bags": "Bags",
    "footwear": "Footwear",
    "jewellery": "Jewellery",
    "other": "Other",
}
FIT_LABELS = {"core": "Current range", "adjacent": "With a partner", "outside": "New category"}
LEVEL_LABELS = {
    "absent": "Absent",
    "emerging": "Emerging",
    "established": "Established",
    "peak": "Peak",
    "declining": "Declining",
    "unknown": "No data",
}
MOMENTUM_LABELS = {"rising": "Rising", "stable": "Stable", "fading": "Fading"}

# Opportunity matrix geometry, in SVG units.
_W, _H = 620, 520
_LEFT, _RIGHT, _TOP, _BOTTOM = 64, 600, 40, 420
_LOW, _HIGH = 0.5, 5.5


def _x(score):
    return _LEFT + (score - _LOW) / (_HIGH - _LOW) * (_RIGHT - _LEFT)


def _y(score):
    return _BOTTOM - (score - _LOW) / (_HIGH - _LOW) * (_BOTTOM - _TOP)


def _spread(themes):
    """Place themes that share a cell around it instead of on top of each other."""
    offsets = [(0, 0), (-28, 0), (0, 28), (-28, 28), (28, 0), (0, -28)]
    seen = defaultdict(int)
    placed = []
    for number, theme in themes:
        key = (theme["fit"], theme["attractiveness"])
        dx, dy = offsets[seen[key] % len(offsets)]
        seen[key] += 1
        placed.append((number, theme, _x(theme["fit"]) + dx, _y(theme["attractiveness"]) + dy))
    return placed


def opportunity_matrix(themes) -> str:
    """A 2x2 of market attractiveness against fit with Malina, as SVG."""
    mid_x, mid_y = _x(3), _y(3)
    parts = [
        format_html(
            '<svg class="matrix" viewBox="0 0 {} {}" role="img" aria-labelledby="matrix-title matrix-desc">',
            _W, _H,
        ),
        '<title id="matrix-title">Opportunity matrix</title>',
        '<desc id="matrix-desc">Each numbered circle is a demand theme, placed by market '
        "attractiveness and by fit with Malina. Colour shows momentum.</desc>",
        format_html(
            '<rect class="mx-quad mx-quad-best" x="{}" y="{}" width="{}" height="{}"/>',
            mid_x, _TOP, _RIGHT - mid_x, mid_y - _TOP,
        ),
        format_html(
            '<rect class="mx-quad mx-quad-partner" x="{}" y="{}" width="{}" height="{}"/>',
            _LEFT, _TOP, mid_x - _LEFT, mid_y - _TOP,
        ),
        format_html('<rect class="mx-frame" x="{}" y="{}" width="{}" height="{}"/>',
                    _LEFT, _TOP, _RIGHT - _LEFT, _BOTTOM - _TOP),
        format_html('<line class="mx-mid" x1="{}" y1="{}" x2="{}" y2="{}"/>', mid_x, _TOP, mid_x, _BOTTOM),
        format_html('<line class="mx-mid" x1="{}" y1="{}" x2="{}" y2="{}"/>', _LEFT, mid_y, _RIGHT, mid_y),
        # Quadrant names sit outside the plot, so no point can cover them.
        format_html('<text class="mx-quad-label mx-good" x="{}" y="{}" text-anchor="middle">Build now</text>', (mid_x + _RIGHT) / 2, _TOP - 12),
        format_html('<text class="mx-quad-label" x="{}" y="{}" text-anchor="middle">Enter with a partner</text>', (_LEFT + mid_x) / 2, _TOP - 12),
        format_html('<text class="mx-quad-label" x="{}" y="{}" text-anchor="middle">Keep in range</text>', (mid_x + _RIGHT) / 2, _BOTTOM + 50),
        format_html('<text class="mx-quad-label" x="{}" y="{}" text-anchor="middle">Deprioritise</text>', (_LEFT + mid_x) / 2, _BOTTOM + 50),
    ]
    for score in range(1, 6):
        parts.append(format_html('<text class="mx-tick" x="{}" y="{}" text-anchor="middle">{}</text>', _x(score), _BOTTOM + 22, score))
        parts.append(format_html('<text class="mx-tick" x="{}" y="{}" text-anchor="end">{}</text>', _LEFT - 10, _y(score) + 4, score))
    parts.append(format_html(
        '<text class="mx-axis" x="{}" y="{}" text-anchor="middle">Fit with Malina: from new category (1) to current range (5)</text>',
        (_LEFT + _RIGHT) / 2, _BOTTOM + 86,
    ))
    parts.append(format_html(
        '<text class="mx-axis" transform="translate(18 {}) rotate(-90)" text-anchor="middle">Market attractiveness</text>',
        (_TOP + _BOTTOM) / 2,
    ))
    for number, theme, cx, cy in _spread(list(enumerate(themes, 1))):
        parts.append(format_html(
            '<g class="mx-point mx-{}"><title>{}. {}: attractiveness {}, fit {}, {}</title>'
            '<circle cx="{}" cy="{}" r="15"/><text x="{}" y="{}" text-anchor="middle">{}</text></g>',
            theme["momentum"], number, theme["name"], theme["attractiveness"], theme["fit"],
            MOMENTUM_LABELS.get(theme["momentum"], ""), cx, cy, cx, cy + 5, number,
        ))
    parts.append("</svg>")
    return mark_safe("".join(str(p) for p in parts))


def domain(url) -> str:
    host = urlparse(str(url)).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def kpis(findings, data) -> list:
    """Headline numbers for the top of the dashboard."""
    verified = [f for f in findings if f.verified]
    return [
        {"value": len(data.get("themes", [])), "label": "demand themes"},
        {"value": len({domain(f.source_url) for f in verified}), "label": "independent sources"},
        {"value": len({f.category for f in verified if f.category}), "label": "categories covered"},
        {"value": sum(1 for f in verified if f.momentum == "rising"), "label": "rising trends found"},
        {"value": sum(1 for f in verified if f.forecast), "label": "published forecasts"},
    ]


def category_mix(findings) -> list:
    """Verified findings per category, split by fit with Malina, largest first."""
    counts = defaultdict(Counter)
    for f in findings:
        if f.verified and f.category in CATEGORIES:
            counts[f.category][f.malina_fit or "outside"] += 1
    if not counts:
        return []
    largest = max(sum(c.values()) for c in counts.values())
    rows = []
    for category, by_fit in sorted(counts.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(by_fit.values())
        rows.append({
            "label": CATEGORY_LABELS[category],
            "total": total,
            "segments": [
                {"fit": fit, "label": FIT_LABELS[fit], "count": by_fit[fit],
                 "width": round(by_fit[fit] / largest * 100, 1)}
                for fit in FITS if by_fit[fit]
            ],
        })
    return rows


def reported_figures(findings):
    """Split the source-quoted figures into percentage changes (charted) and the rest (listed)."""
    percentages, others = [], []
    for f in findings:
        if not f.verified:
            continue
        for figure in f.figures or []:
            row = {**figure, "theme": f.theme, "source_title": f.source_title or domain(f.source_url),
                   "source_url": f.source_url}
            (percentages if figure.get("unit") == "%" else others).append(row)
    percentages.sort(key=lambda r: -r["value"])
    largest = max((abs(r["value"]) for r in percentages), default=0) or 1
    for row in percentages:
        row["width"] = round(abs(row["value"]) / largest * 100, 1)
        row["negative"] = row["value"] < 0
    return percentages, others


def published_forecasts(findings) -> list:
    return [
        {**f.forecast, "theme": f.theme, "source_title": f.source_title or domain(f.source_url),
         "source_url": f.source_url}
        for f in findings
        if f.verified and f.forecast
    ]


def timeline_rows(themes, periods) -> list:
    rows = []
    for number, theme in enumerate(themes, 1):
        cells = [{"level": h["level"], "label": LEVEL_LABELS[h["level"]]} for h in theme.get("history", [])]
        outlook = theme.get("outlook", {})
        rows.append({
            "number": number,
            "name": theme["name"],
            "cells": cells,
            "outlook": {"level": outlook.get("level", "unknown"),
                        "label": LEVEL_LABELS.get(outlook.get("level"), "No data"),
                        "reason": outlook.get("reason", "")},
        })
    return rows


def legend(themes) -> str:
    return format_html_join(
        "", '<li><span class="mx-key mx-{}">{}</span>{}</li>',
        ((t["momentum"], n, t["name"]) for n, t in enumerate(themes, 1)),
    )


__all__ = [
    "CATEGORY_LABELS", "FIT_LABELS", "LEVEL_LABELS", "MOMENTUM_LABELS",
    "category_mix", "domain", "escape", "kpis", "legend", "opportunity_matrix",
    "published_forecasts", "reported_figures", "timeline_rows",
]
