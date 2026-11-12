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


SAMPLE_FINDINGS = [
    ("Chunky hand-knit accessories", "Sample finding. Oversized hand-knit hats, balaclavas and mittens in undyed wool.",
     "Sample evidence: a marketplace trend report names hand-knit winter accessories among its fastest-growing searches.",
     "Sample marketplace trend report", "https://example.com/sample/marketplace-trends", "2026-10"),
    ("Embroidered everyday knitwear", "Sample finding. Plain knit cardigans and vests with one small embroidered motif.",
     "Sample evidence: a retailer reports embroidered knitwear selling out ahead of plain styles.",
     "Sample retailer season report", "https://example.com/sample/retailer-report", "2026-09"),
    ("Open crochet layering pieces", "Sample finding. Crochet tops, collars and overdresses worn over plain clothes.",
     "Sample evidence: a trade article reports rising search interest in crochet layering for spring.",
     "Sample trade press article", "https://example.com/sample/trade-article", "2026-10"),
    ("Graphic two-colour jacquard", "Sample finding. Bold two-colour patterned knits at accessible prices.",
     "Sample evidence: a resale platform lists patterned jacquard knits among its most-saved items.",
     "Sample resale platform data", "https://example.com/sample/resale-data", "2026-08"),
]


def sample_source_urls() -> list:
    return [f[4] for f in SAMPLE_FINDINGS]


def _research(prompt: str) -> str:
    findings = [
        {"theme": t, "what_people_want": w, "evidence": e, "source_title": title,
         "source_url": url, "source_date": date,
         "relevance": "Sample relevance: fits craft techniques named in the brand brief."}
        for t, w, e, title, url, date in SAMPLE_FINDINGS
    ]
    return json.dumps({"findings": findings, "searches_summary": "Sample searches; no web search was run."})


def _demand_brief(prompt: str) -> str:
    try:
        signals = json.loads(_between(prompt, "audience_signals") or "[]")
    except json.JSONDecodeError:
        signals = []
    try:
        findings = json.loads(_between(prompt, "trend_findings") or "[]")
    except json.JSONDecodeError:
        findings = []
    direction = _between(prompt, "founder_direction").strip()
    purchase = [s for s in signals if s.get("intent") == "purchase"]
    interest = [s for s in signals if s.get("intent") == "interest"]
    trend = [s for s in signals if s.get("intent") == "trend"]
    counts = {}
    for s in signals:
        key = s.get("product_type") or "unspecified pieces"
        counts.setdefault(key, {"purchase": 0, "interest": 0, "trend": 0, "quotes": []})
        counts[key][s.get("intent", "interest")] += 1
        if s.get("quote") and len(counts[key]["quotes"]) < 2:
            counts[key]["quotes"].append(s["quote"])
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1]["purchase"], -kv[1]["interest"]))[:5]

    def strength(c):
        if c["purchase"] >= 5:
            return "strong"
        if c["purchase"] >= 2:
            return "moderate"
        return "weak"

    has_direction = bool(direction) and not direction.lower().startswith("none given")
    lines = [
        "SAMPLE RESEARCH BRIEF (dry run: written by simple rules, not by a model)",
        "",
        f"Read: {len(findings)} web research findings and {len(signals)} audience signals "
        f"({len(purchase)} purchase intent, {len(interest)} interest, {len(trend)} external).",
        "",
        "1. Top demand themes",
    ]
    for f in findings[:5]:
        lines.append(
            f"- {f.get('theme')}: {f.get('what_people_want')} Rests on web research "
            f"({f.get('source_title')}). Evidence: weak, single source."
        )
    for name, c in ranked:
        quotes = "; ".join(f'"{q}"' for q in c["quotes"]) or "no quotes"
        lines.append(
            f"- {name.capitalize()}: {c['purchase']} purchase-intent and {c['interest']} interest "
            f"signals, {c['trend']} external. Evidence: {strength(c)}. Quotes: {quotes}."
        )
    lines += [
        "",
        "Founder's direction",
        (f"Given: {direction} Evidence: untested until the price test." if has_direction
         else "No direction was given."),
    ]
    lines += [
        "",
        "2. Earlier price tests",
        "None yet. This is the first cycle.",
        "",
        "3. Gaps",
        "- No prices have been shown to the audience, so willingness to pay is unknown.",
        "- External trend data is thin or missing until Pinterest and Google access is granted.",
        "",
        "4. Conflicts with the brand brief",
        "None found by the dry-run rules.",
    ]
    return "\n".join(lines)


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


def respond(step: str, prompt: str) -> str:
    base = step.removesuffix("_retry")
    if base == "extract_signals":
        return _extract_signals(prompt)
    if base == "research_trends":
        return _research(prompt)
    if base == "reformat_findings":
        return _between(prompt, "text")
    if base == "demand_brief":
        return _demand_brief(prompt)
    if base == "generate_concepts":
        return json.dumps(_sample_concepts())
    if base == "review_concept":
        return _review(prompt)
    if base == "revise_concept":
        return _revise(prompt)
    raise ValueError(f"No dry-run response is defined for step '{step}'.")
