"""Sample responses for dry-run mode.

With FDW_DRY_RUN=1 no model is called and nothing is spent. These responses
are simple, rule-based stand-ins so the screens and gates can be tried end to
end. Everything they produce is labelled as a sample, because it says nothing
about what real customers want.
"""
import json
import re

PRODUCT_WORDS = (
    "cardigan", "sweater", "jumper", "top", "dress", "skirt", "jacket", "vest",
    "hat", "beanie", "gloves", "mittens", "scarf", "bag", "collar", "shirt",
)
TECHNIQUE_WORDS = (
    ("embroider", "embroidery"),
    ("crochet", "crochet"),
    ("knit", "hand knit"),
)
PURCHASE_WORDS = ("price", "how much", "buy", "order", "cost", "sell", "available")


def _between(prompt: str, tag: str) -> str:
    match = re.search(rf"<{tag}>\n?(.*?)\n?</{tag}>", prompt, re.DOTALL)
    return match.group(1) if match else ""


def _extract_signals(prompt: str) -> str:
    signals = []
    for line in _between(prompt, "items").splitlines():
        match = re.match(r"\[\d+\] \(([a-z_]+)\) (.*)", line.strip())
        if not match:
            continue
        source, text = match.group(1), match.group(2)
        lower = text.lower()
        product = next((w for w in PRODUCT_WORDS if re.search(rf"\b{w}s?\b", lower)), None)
        technique = next((name for word, name in TECHNIQUE_WORDS if word in lower), None)
        external = source in ("pinterest", "google_trends", "article")
        if external:
            intent = "trend"
        elif any(word in lower for word in PURCHASE_WORDS):
            intent = "purchase"
        else:
            intent = "interest"
        signals.append({
            "source": source,
            "date": None,
            "product_type": product,
            "technique": technique,
            "attributes": [],
            "intent": intent,
            "strength": None if external else 1,
            "quote": " ".join(text.split()[:12]) or None,
        })
    return json.dumps(signals)


# Sample findings: (theme, category, want, evidence, timeframe, momentum, seasons, figures, forecast, fit, title, url, date)
SAMPLE_FINDINGS = [
    ("Chunky hand-knit accessories", "accessories",
     "Sample finding. Oversized hand-knit hats, balaclavas and mittens in undyed wool.",
     "Sample evidence: a marketplace trend report says searches for hand-knit winter accessories grew 38% year on year.",
     "recurring", "rising", "Sample: Autumn/Winter 2024 and 2025",
     [{"label": "search growth year on year", "value": 38, "unit": "%", "period": "Autumn/Winter 2025"}], None,
     "core", "Sample marketplace trend report", "https://marketplace.example/sample/trends", "2026-10"),
    ("Embroidered everyday knitwear", "knitwear",
     "Sample finding. Plain knit cardigans and vests with one small embroidered motif.",
     "Sample evidence: a retailer reports embroidered knitwear sales up 22% and selling out ahead of plain styles.",
     "both", "rising", "Sample: Autumn/Winter 2025 and this year",
     [{"label": "sales growth", "value": 22, "unit": "%", "period": "this season"}],
     {"statement": "Sample forecast: the retailer expects embroidered knitwear to keep growing through 2027.",
      "horizon": "2027", "value": None, "unit": None},
     "core", "Sample retailer season report", "https://retailer.example/sample/season-report", "2026-09"),
    ("Structured suede shoulder bags", "bags",
     "Sample finding. Compact structured shoulder bags in brown suede, priced 150 to 300 euros.",
     "Sample evidence: a resale platform reports demand for suede bags up 64% on last year.",
     "current", "rising", None,
     [{"label": "resale demand growth", "value": 64, "unit": "%", "period": "last 12 months"}],
     {"statement": "Sample forecast: the platform projects suede accessories to grow 15% in 2027.",
      "horizon": "2027", "value": 15, "unit": "%"},
     "outside", "Sample resale platform data", "https://resale.example/sample/data", "2026-08"),
    ("Quilted liner jackets", "outerwear",
     "Sample finding. Lightweight quilted liner jackets worn as a layer, in muted colours.",
     "Sample evidence: a trade article reports quilted outerwear has held steady for three winters.",
     "recurring", "stable", "Sample: Autumn/Winter 2023 to 2025", [], None,
     "adjacent", "Sample trade press article", "https://tradepress.example/sample/article", "2026-10"),
    ("Open crochet layering pieces", "apparel",
     "Sample finding. Crochet tops and collars worn over plain clothes.",
     "Sample evidence: a search report says crochet searches fell 12% after two strong summers.",
     "both", "fading", "Sample: Spring/Summer 2024 and 2025",
     [{"label": "search change", "value": -12, "unit": "%", "period": "year on year"}], None,
     "core", "Sample search trend report", "https://search-trends.example/sample/report", "2026-07"),
]


def sample_source_urls() -> list:
    return [f[11] for f in SAMPLE_FINDINGS]


def _research(prompt: str) -> str:
    findings = [
        {"theme": t, "category": cat, "what_people_want": w, "evidence": e, "timeframe": tf,
         "momentum": mom, "seasons_observed": seasons, "figures": figs, "forecast": fc,
         "malina_fit": fit, "source_title": title, "source_url": url, "source_date": date,
         "relevance": "Sample relevance."}
        for t, cat, w, e, tf, mom, seasons, figs, fc, fit, title, url, date in SAMPLE_FINDINGS
    ]
    return json.dumps({"findings": findings, "searches_summary": "Sample searches; no web search was run."})


def _demand_brief(prompt: str) -> str:
    def load(tag):
        try:
            return json.loads(_between(prompt, tag) or "[]")
        except json.JSONDecodeError:
            return []

    signals, findings = load("audience_signals"), load("trend_findings")
    direction = _between(prompt, "founder_direction").strip()
    season = (_between(prompt, "target_season").strip().splitlines() or ["Not set"])[0]
    has_direction = bool(direction) and not direction.lower().startswith("none given")
    purchase = sum(1 for s in signals if s.get("intent") == "purchase")
    periods = ["AW 2023", "AW 2024", "AW 2025", "Now"]
    fit_score = {"core": 5, "adjacent": 3, "outside": 2}
    history_for = {
        ("recurring", "rising"): ["emerging", "established", "established", "peak"],
        ("both", "rising"): ["unknown", "emerging", "established", "peak"],
        ("current", "rising"): ["absent", "absent", "emerging", "established"],
        ("recurring", "stable"): ["established", "established", "established", "established"],
        ("both", "fading"): ["emerging", "peak", "peak", "declining"],
    }
    themes = []
    for f in findings[:5]:
        key = (f.get("timeframe"), f.get("momentum"))
        levels = history_for.get(key, ["unknown"] * 4)
        recurring = f.get("timeframe") in ("recurring", "both")
        themes.append({
            "name": f.get("theme"),
            "category": f.get("category") or "other",
            "what_people_want": f.get("what_people_want"),
            "strength": "moderate" if recurring else "weak",
            "strength_reason": ("Sample rating: seen in more than one past season."
                                if recurring else "Sample rating: a single source."),
            "timeframe": f.get("timeframe") or "current",
            "momentum": f.get("momentum") or "stable",
            "attractiveness": {"rising": 4, "stable": 3, "fading": 2}.get(f.get("momentum"), 3) + (1 if f.get("figures") and f.get("momentum") == "rising" else 0),
            "fit": fit_score.get(f.get("malina_fit"), 3),
            "fit_note": {"core": "Sample note: Malina can make this in house.",
                         "adjacent": "Sample note: needs a partner workshop.",
                         "outside": "Sample note: needs a new supplier in a new category."}.get(f.get("malina_fit"), "Sample note."),
            "history": [{"period": p, "level": l} for p, l in zip(periods, levels)],
            "outlook": {"level": {"rising": "established", "stable": "established", "fading": "declining"}.get(f.get("momentum"), "unknown"),
                        "reason": "Sample outlook: based on momentum in the findings."},
            "basis": ["web_research"],
            "sources": [{"title": f.get("source_title") or "Source", "url": f.get("source_url")}],
            "audience_signals": None,
        })
    if signals and len(themes) < 6:
        themes.append({
            "name": "What your audience asks for", "category": "other",
            "what_people_want": "Sample theme built from the messages you added.",
            "strength": "strong" if purchase else "weak",
            "strength_reason": "Sample rating: based on messages from your own audience.",
            "timeframe": "not_applicable", "momentum": "stable", "attractiveness": 3, "fit": 5,
            "fit_note": "Sample note: Malina already makes these.",
            "history": [{"period": p, "level": "unknown"} for p in periods],
            "outlook": {"level": "unknown", "reason": "Sample outlook: no forecast found."},
            "basis": ["audience_signals"], "sources": [],
            "audience_signals": f"{purchase} asked to buy",
        })
    if has_direction and len(themes) < 6:
        themes.append({
            "name": "Your direction", "category": "other", "what_people_want": direction[:200],
            "strength": "untested", "strength_reason": "Sample rating: the price test decides.",
            "timeframe": "not_applicable", "momentum": "stable", "attractiveness": 3, "fit": 4,
            "fit_note": "Sample note: close to Malina's current range.",
            "history": [{"period": p, "level": "unknown"} for p in periods],
            "outlook": {"level": "unknown", "reason": "Sample outlook: nothing found."},
            "basis": ["founder_direction"], "sources": [], "audience_signals": None,
        })
    if not themes:
        themes.append({
            "name": "No themes found", "category": "other", "what_people_want": "The sample research returned nothing.",
            "strength": "untested", "strength_reason": "Sample rating: no evidence.", "timeframe": "not_applicable",
            "momentum": "stable", "attractiveness": 1, "fit": 1, "fit_note": "Sample note.",
            "history": [{"period": p, "level": "unknown"} for p in periods],
            "outlook": {"level": "unknown", "reason": "Sample outlook."},
            "basis": ["founder_direction"], "sources": [], "audience_signals": None,
        })
    for t in themes:
        t["attractiveness"] = max(1, min(5, t["attractiveness"]))
    themes.sort(key=lambda t: -t["attractiveness"])
    outside = [f for f in findings if f.get("malina_fit") == "outside"]
    return json.dumps({
        "season": season if not season.startswith("Not set") else "Next season (sample)",
        "headline": "Sample headline: grow from knitwear into accessories, where demand is rising fastest.",
        "key_takeaways": [
            {"headline": "Sample takeaway: hand-knit accessories recur every winter and are still growing.",
             "so_what": "Sample: a safe core for the collection."},
            {"headline": "Sample takeaway: the fastest growth is in a category Malina does not make yet.",
             "so_what": "Sample: worth testing with a partner."},
            {"headline": "Sample takeaway: crochet layering is fading after two strong summers.",
             "so_what": "Sample: avoid building a collection on it."},
        ],
        "timeline_periods": periods,
        "themes": themes[:6],
        "market_shifts": [
            {"from": "Sample: statement crochet", "to": "Sample: quiet knitwear with one detail",
             "evidence": "Sample: search and retailer reports."},
        ],
        "season_view": {
            "returns_every_year": [f.get("theme") for f in findings if f.get("timeframe") in ("recurring", "both") and f.get("momentum") != "fading"],
            "new_this_year": [f.get("theme") for f in findings if f.get("timeframe") == "current"],
            "fading": [f.get("theme") for f in findings if f.get("momentum") == "fading"],
        },
        "beyond_current_range": [
            {"opportunity": f.get("theme"), "why": f.get("evidence"),
             "route": "Sample route: work with a specialist supplier."}
            for f in outside
        ],
        "implications": [
            {"title": "Sample: anchor on recurring knit accessories", "action": "Sample: build the core of the collection on demand that returns every winter."},
            {"title": "Sample: test one piece outside the range", "action": "Sample: add a partner-made accessory to the price test."},
            {"title": "Sample: drop fading themes", "action": "Sample: leave crochet layering out this season."},
        ],
        "direction": {
            "verdict": "untested" if has_direction else "none_given",
            "explanation": "Sample verdict: the dry run cannot judge a direction." if has_direction else None,
        },
        "past_tests": "None yet.",
        "gaps": ["Sample gap: no prices have been shown to the audience, so willingness to pay is unknown."],
    })


def _piece(cid, n, name, role, category, silhouette, technique, construction, materials,
           detail, colours, dims, trims, route, price, evidence, notes=""):
    return {
        "piece_id": f"{cid}-P{n}", "name": name, "role": role, "category": category,
        "silhouette": silhouette, "technique": technique, "construction": construction,
        "materials": materials, "surface_detail": detail, "colours": colours,
        "dimensions_cm": dims, "trims": trims, "production_route": route,
        "target_price_eur": price, "evidence": evidence, "feasibility_notes": notes,
    }


def _sample_concepts() -> dict:
    ev = ["Sample theme from the dry-run research brief"]
    c1 = {
        "concept_id": "C1",
        "name": "Sample: Hedgerow",
        "theme": "Sample concept. Winter knits in undyed wool, each carrying a small hand-embroidered "
                 "sprig of rosehip at one cuff or edge. Quiet pieces with one bright detail.",
        "target_customer": "Followers who asked for embroidered knitwear to wear daily in cold months.",
        "signature_detail": "A 4 cm rosehip sprig in satin stitch, always placed off-centre at an edge.",
        "palette": [{"name": "Undyed ecru", "hex": "#E8E2D4"}, {"name": "Rosehip red", "hex": "#B3262E"},
                    {"name": "Bramble green", "hex": "#3F5B45"}],
        "why_now": "Sample evidence: purchase-intent messages about knitwear and embroidery.",
        "basis": "Sample basis: web research on embroidered knitwear and hand-knit accessories; evidence weak, single sources.",
        "pieces": [
            _piece("C1", 1, "Hedgerow cardigan", "hero", "Cardigan", "Relaxed fit, hip length, V neck, drop shoulder",
                   "hand knit with hand embroidery", "Knitted flat in four panels, mattress-stitched seams, 6 cm 2x2 rib at hem and cuffs",
                   "100% wool, aran weight, matte, soft hand", "Rosehip sprig, satin and stem stitch, 4 cm, left cuff",
                   [{"name": "Undyed ecru", "hex": "#E8E2D4", "where": "body"}, {"name": "Rosehip red", "hex": "#B3262E", "where": "embroidery"},
                    {"name": "Bramble green", "hex": "#3F5B45", "where": "embroidery stem"}],
                   {"body_length": 62, "chest_width": 56, "sleeve_length": 48}, "Five 20 mm corozo buttons", "atelier", 320, ev),
            _piece("C1", 2, "Hedgerow pullover", "core", "Sweater", "Regular fit, crew neck, hip length",
                   "machine knit with hand embroidery", "Fully fashioned panels, linked seams, 5 cm 1x1 rib",
                   "100% wool, DK weight", "Rosehip sprig, 4 cm, right hem edge",
                   [{"name": "Undyed ecru", "hex": "#E8E2D4", "where": "body"}, {"name": "Rosehip red", "hex": "#B3262E", "where": "embroidery"}],
                   {"body_length": 60, "chest_width": 52, "sleeve_length": 60}, "None", "specialist", 190, ev,
                   "Needs a knitter who accepts finished panels back for hand embroidery."),
            _piece("C1", 3, "Hedgerow vest", "core", "Vest", "Cropped, V neck, sleeveless",
                   "machine knit with hand embroidery", "Two panels, linked side seams, 4 cm rib at armholes and hem",
                   "100% wool, DK weight", "Rosehip sprig, 4 cm, left chest",
                   [{"name": "Bramble green", "hex": "#3F5B45", "where": "body"}, {"name": "Rosehip red", "hex": "#B3262E", "where": "embroidery"}],
                   {"body_length": 48, "chest_width": 48}, "None", "specialist", 140, ev),
            _piece("C1", 4, "Hedgerow beanie", "accessory", "Hat", "Close fit with 8 cm fold-up brim",
                   "hand knit with hand embroidery", "Knitted in the round, 2x2 rib brim, crown decreases in four sections",
                   "100% wool, aran weight", "Rosehip sprig, 3 cm, on the brim",
                   [{"name": "Undyed ecru", "hex": "#E8E2D4", "where": "body"}, {"name": "Rosehip red", "hex": "#B3262E", "where": "embroidery"}],
                   {"circumference": 54, "height": 22}, "None", "atelier", 75, ev),
            _piece("C1", 5, "Hedgerow gloves", "accessory", "Gloves", "Fingerless, wrist length",
                   "hand knit with hand embroidery", "Knitted in the round with a thumb gusset, 5 cm rib cuff",
                   "100% wool, DK weight", "Rosehip sprig, 3 cm, back of left hand",
                   [{"name": "Bramble green", "hex": "#3F5B45", "where": "body"}, {"name": "Rosehip red", "hex": "#B3262E", "where": "embroidery"}],
                   {"length": 19, "palm_circumference": 19}, "None", "atelier", 60, ev),
        ],
    }
    c2 = {
        "concept_id": "C2",
        "name": "Sample: Lattice",
        "theme": "Sample concept. Open crochet in cotton, built from one repeating diamond motif at "
                 "three scales, for layering over plain clothes.",
        "target_customer": "Followers who asked about crochet tops and custom pieces for warm weather.",
        "signature_detail": "A diamond filet motif repeated at 3, 6 and 12 cm across the collection.",
        "palette": [{"name": "Chalk white", "hex": "#F5F3EE"}, {"name": "Ink blue", "hex": "#1F2A44"}],
        "why_now": "Sample evidence: interest signals mentioning crochet.",
        "basis": "Sample basis: web research on crochet layering; evidence weak, single source.",
        "pieces": [
            _piece("C2", 1, "Lattice overdress", "hero", "Dress", "Straight, mid-calf, boat neck, sleeveless",
                   "crochet", "Worked in joined motifs from the hem up, crab-stitch edging",
                   "100% mercerised cotton, 4 ply, slight sheen", "12 cm diamond filet motifs across the whole surface",
                   [{"name": "Chalk white", "hex": "#F5F3EE", "where": "all over"}],
                   {"body_length": 112, "chest_width": 48}, "None", "atelier", 420, ev),
            _piece("C2", 2, "Lattice top", "core", "Top", "Boxy, cropped, short sleeve",
                   "crochet", "Two rectangles joined at shoulder and side, picot edge",
                   "100% mercerised cotton, 4 ply", "6 cm diamond filet motifs",
                   [{"name": "Ink blue", "hex": "#1F2A44", "where": "all over"}],
                   {"body_length": 46, "chest_width": 50}, "None", "atelier", 180, ev),
            _piece("C2", 3, "Lattice collar", "core", "Collar", "Detachable, rounded, ties at front",
                   "crochet", "Worked flat in one piece with twisted-cord ties",
                   "100% mercerised cotton, 2 ply", "3 cm diamond motifs with scalloped edge",
                   [{"name": "Chalk white", "hex": "#F5F3EE", "where": "all over"}],
                   {"neck_circumference": 40, "depth": 9}, "Twisted cotton cord ties", "atelier", 65, ev),
            _piece("C2", 4, "Lattice bag", "accessory", "Bag", "Small shoulder bag with flap",
                   "crochet", "Dense single crochet body, cotton twill lining, crocheted strap",
                   "100% cotton cord, 3 mm", "One 12 cm diamond motif on the flap",
                   [{"name": "Ink blue", "hex": "#1F2A44", "where": "body"}, {"name": "Chalk white", "hex": "#F5F3EE", "where": "flap motif"}],
                   {"width": 22, "height": 16, "depth": 6, "strap_drop": 55}, "Magnetic snap, 14 mm", "atelier", 95, ev),
        ],
    }
    c3 = {
        "concept_id": "C3",
        "name": "Sample: Signal",
        "theme": "Sample concept. Machine jacquard knits in two strong colours with a graphic "
                 "stitch-chart pattern, made on demand at an accessible price.",
        "target_customer": "New buyers who want a Malina piece at a lower entry price.",
        "signature_detail": "A knitting-chart grid pattern, 2 cm squares, across every piece.",
        "palette": [{"name": "Signal red", "hex": "#C8102E"}, {"name": "Cream", "hex": "#F2EBDD"}],
        "why_now": "Sample evidence: purchase-intent messages asking for hats and scarves.",
        "basis": "Sample basis: web research on jacquard knits; evidence weak, single source.",
        "pieces": [
            _piece("C3", 1, "Signal sweater", "hero", "Sweater", "Oversized, crew neck, hip length",
                   "machine jacquard knit", "Four jacquard panels, linked seams, 6 cm rib",
                   "Acrylic-wool blend, fibre content to confirm with supplier", "2 cm chart grid, all over",
                   [{"name": "Signal red", "hex": "#C8102E", "where": "pattern"}, {"name": "Cream", "hex": "#F2EBDD", "where": "ground"}],
                   {"body_length": 66, "chest_width": 60, "sleeve_length": 54}, "None", "on_demand", 120, ev,
                   "Fibre content is not stated by on-demand suppliers checked so far."),
            _piece("C3", 2, "Signal beanie", "accessory", "Hat", "Close fit, single layer with rolled brim",
                   "machine jacquard knit", "Knitted tube, gathered crown",
                   "Acrylic-wool blend, to confirm", "2 cm chart grid",
                   [{"name": "Signal red", "hex": "#C8102E", "where": "pattern"}, {"name": "Cream", "hex": "#F2EBDD", "where": "ground"}],
                   {"circumference": 54, "height": 21}, "None", "on_demand", 39, ev),
            _piece("C3", 3, "Signal scarf", "accessory", "Scarf", "Long rectangle",
                   "machine jacquard knit", "Double-layer jacquard tube, closed ends",
                   "Acrylic-wool blend, to confirm", "2 cm chart grid, reversed colours on the back",
                   [{"name": "Signal red", "hex": "#C8102E", "where": "pattern"}, {"name": "Cream", "hex": "#F2EBDD", "where": "ground"}],
                   {"length": 180, "width": 24}, "None", "on_demand", 55, ev),
            _piece("C3", 4, "Signal cardigan", "core", "Cardigan", "Boxy, cropped, V neck",
                   "machine jacquard knit", "Four panels, linked seams, knitted button band",
                   "Acrylic-wool blend, to confirm", "2 cm chart grid",
                   [{"name": "Cream", "hex": "#F2EBDD", "where": "ground"}, {"name": "Signal red", "hex": "#C8102E", "where": "pattern"}],
                   {"body_length": 50, "chest_width": 54, "sleeve_length": 52}, "Four 18 mm resin buttons", "on_demand", 135, ev),
        ],
    }
    return {"concepts": [c1, c2, c3]}


def _sample_plans() -> dict:
    plans = []
    for concept in _sample_concepts()["concepts"]:
        plan = {k: concept[k] for k in ("concept_id", "name", "theme", "target_customer",
                                         "signature_detail", "palette", "why_now", "basis")}
        plan["pieces"] = [
            {"piece_id": p["piece_id"], "name": p["name"], "role": p["role"], "category": p["category"],
             "technique": p["technique"], "production_route": p["production_route"],
             "target_price_eur": p["target_price_eur"], "idea": p["surface_detail"]}
            for p in concept["pieces"]
        ]
        plans.append(plan)
    return {"concepts": plans}


def _detail(prompt: str) -> str:
    match = re.search(r'"concept_id":\s*"([^"]+)"', _between(prompt, "concept_plan"))
    concept_id = match.group(1) if match else "C1"
    for concept in _sample_concepts()["concepts"]:
        if concept["concept_id"] == concept_id:
            return json.dumps(concept)
    return "{}"


SAMPLE_SCORES = {
    "C1": ({"demand_evidence": 4, "coherence": 5, "uniqueness": 4, "makeability": 3, "price_fit": 4, "spec_completeness": 4}, "recommend"),
    "C2": ({"demand_evidence": 3, "coherence": 5, "uniqueness": 4, "makeability": 4, "price_fit": 3, "spec_completeness": 4}, "recommend"),
    "C3": ({"demand_evidence": 4, "coherence": 4, "uniqueness": 2, "makeability": 4, "price_fit": 4, "spec_completeness": 3}, "revise"),
}


def _review(prompt: str) -> str:
    match = re.search(r'"concept_id":\s*"([^"]+)"', _between(prompt, "concept"))
    concept_id = match.group(1) if match else "C1"
    scores, overall = SAMPLE_SCORES.get(concept_id, SAMPLE_SCORES["C1"])
    reasons = {key: "Sample reasoning from the dry run; a real review cites specific pieces." for key in scores}
    risks = ["Sample risk: delivered pieces may differ from concept renders."]
    if concept_id == "C3":
        risks.append("Sample risk: fibre content is unknown, which weakens any quality claim.")
    return json.dumps({
        "concept_id": concept_id, "scores": scores, "reasons": reasons,
        "blocking_issues": [], "risks": risks, "overall": overall,
    })


def _revise(prompt: str) -> str:
    try:
        concept = json.loads(_between(prompt, "concept"))
    except json.JSONDecodeError:
        return "{}"
    for piece in concept.get("pieces", []):
        technique = str(piece.get("technique", "")).lower()
        if piece.get("production_route") == "on_demand" and ("hand" in technique or "crochet" in technique):
            piece["production_route"] = "atelier"
    return json.dumps(concept)


def _first_number(text: str):
    match = re.search(r"\d+(?:\.\d+)?", str(text))
    return float(match.group()) if match else None


def _sourcing(prompt: str) -> str:
    """A rule-based sourcing sheet: atelier pieces need no supplier; others take the first
    supplier in the table on the same route, priced at the first number in its prices."""
    try:
        pieces = json.loads(_between(prompt, "piece_specifications"))
    except json.JSONDecodeError:
        return "{}"
    try:
        suppliers = json.loads(_between(prompt, "supplier_table"))
    except json.JSONDecodeError:
        suppliers = []
    entries = []
    for piece in pieces:
        route = piece.get("production_route")
        technique = str(piece.get("technique", "")).lower()
        if route == "on_demand" and ("hand" in technique or "crochet" in technique):
            route = "atelier"
        entry = {
            "piece_id": piece["piece_id"], "route": route, "status": "makeable_as_specified",
            "changes": [], "candidate_suppliers": [], "unit_cost": None, "currency": "EUR",
            "cost_basis": "Sample: Malina's own cost is not in the table." if route == "atelier" else "",
            "margin_calc": "",
            "questions_for_supplier": [] if route == "atelier" else ["Sample: what is the fibre content?"],
        }
        if route != "atelier":
            match = next((sup for sup in suppliers if sup.get("route") == route), None)
            if match is None:
                entry["status"] = "needs_sourcing"
                entry["cost_basis"] = f"Sample: no {route} supplier in the table."
            else:
                entry["candidate_suppliers"] = [{"name": match["name"], "changes_needed": "Sample: none checked."}]
                cost = _first_number(match.get("prices", ""))
                if cost:
                    entry["unit_cost"] = cost
                    entry["currency"] = match.get("price_currency", "EUR")
                    entry["cost_basis"] = f"Sample, from the table: {match['prices']}"
        entries.append(entry)
    return json.dumps({
        "pieces": entries,
        "collection_summary": "Sample sourcing sheet written by the dry run from simple rules. It says nothing about real suppliers.",
    })


def _art_direction(prompt: str) -> str:
    try:
        spec = json.loads(_between(prompt, "piece_specification"))
    except (ValueError, TypeError):
        spec = {}
    name = spec.get("name") or "the piece"
    colours = ", ".join(
        f"{c.get('name')} ({c.get('hex')})" for c in spec.get("colours", []) if isinstance(c, dict)
    ) or "the specified colours"
    body = (
        f"{name}, {spec.get('category', 'garment')}, {spec.get('silhouette', '')}. "
        f"Made in {spec.get('technique', 'the specified technique')}: {spec.get('construction', '')}. "
        f"Materials: {spec.get('materials', '')}. Surface detail: {spec.get('surface_detail', '')}. "
        f"Colours: {colours}. Sample prompt written by the dry run; no model was called."
    )
    setups = {
        "A": "Front packshot on a seamless off-white background, soft overhead light, 85 mm, f/8.",
        "B": "Back packshot, the same piece as in the reference image, same light, 85 mm, f/8.",
        "C": "Detail macro of the signature detail, the same piece as in the reference image, 100 mm macro, f/5.6, raking light.",
        "D": "On-model studio shot, the same piece as in the reference images, warm-grey background, 85 mm, f/4.",
        "E": "On-model lifestyle shot, the same model and piece as in the reference images, daylight, 50 mm, f/2.8.",
    }
    return json.dumps({
        "piece_id": spec.get("piece_id", ""),
        "prompts": {shot: f"{body} {setup} No text, logos or watermarks." for shot, setup in setups.items()},
        "reviewer_checklist": [
            f"Sample fact: the piece is a {spec.get('category', 'garment')}",
            f"Sample fact: colours are {colours}",
            "Sample fact: the signature detail is placed as specified",
            "Sample fact: stitch scale matches the yarn weight",
            "Sample fact: hems and closures are where the specification puts them",
        ],
        "missing_information": ["Sample: the dry run did not check the specification for gaps."],
    })


def _image_review(prompt: str) -> str:
    """Pass every image, except the first try at a detail macro, so the retry path can be seen."""
    shot = _between(prompt, "shot_type").split(":")[0].strip() or "A"
    first_try = "Earlier attempts at this shot failed" not in prompt
    failing = shot == "C" and first_try
    return json.dumps({
        "piece_id": "",
        "shot": shot,
        "checklist_results": [
            {"fact": "Sample fact: overall shape", "observed": "Sample observation", "result": "match"},
            {"fact": "Sample fact: stitch scale", "observed": "Sample observation",
             "result": "mismatch" if failing else "match"},
        ],
        "realism_issues": [],
        "verdict": "fail" if failing else "pass",
        "correction_note": "Sample correction: the stitches must be readable one by one, about 6 mm wide."
        if failing else "",
    })


def respond(step: str, prompt: str) -> str:
    base = step.removesuffix("_retry")
    if base == "extract_signals":
        return _extract_signals(prompt)
    if base == "bakeoff_describe":
        name = _between(prompt, "piece_name").strip() or "the piece"
        return json.dumps({
            "description": f"Sample description of {name}, written by the dry run without looking at the photos.",
            "checklist": ["Sample fact: overall shape", "Sample fact: main colour", "Sample fact: signature detail"],
            "prompts": {
                "A": f"Sample prompt for a front packshot of {name}.",
                "C": f"Sample prompt for a detail close-up of {name}, the same piece as in the reference image.",
                "D": f"Sample prompt for {name} on a model, the same piece as in the reference image.",
            },
            "unclear_from_photos": ["Sample: fibre content"],
        })
    if base == "research_trends":
        return _research(prompt)
    if base == "reformat_findings":
        return _between(prompt, "text")
    if base == "demand_brief":
        return _demand_brief(prompt)
    if base == "generate_concepts":
        return json.dumps(_sample_concepts())
    if base == "plan_concepts":
        return json.dumps(_sample_plans())
    if base == "detail_concept":
        return _detail(prompt)
    if base == "review_concept":
        return _review(prompt)
    if base == "revise_concept":
        return _revise(prompt)
    if base == "sourcing_check":
        return _sourcing(prompt)
    if base == "visual_art_direction":
        return _art_direction(prompt)
    if base == "visual_review":
        return _image_review(prompt)
    raise ValueError(f"No dry-run response is defined for step '{step}'.")
