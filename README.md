# FDW: Malina design-to-launch workflow

A Django application that researches what people are buying now and designs new
collections from it. You approve every step.

This covers the first three of seven stages:

| Stage | Status | What it does |
| --- | --- | --- |
| 1. Market research | Built | Researches the whole fashion market, compares the target season across earlier years with what is rising now, and builds a strategic report: headline and takeaways, an opportunity matrix, a timeline of each theme, reported growth figures, published forecasts, a category view, and recommendations. Nothing needs to be typed |
| 2. Collection concepts | Built | Plans three new collections in one call, then specifies each in its own call so no answer is cut off; reviews each one separately; you pick one or two |
| 3. Sourcing check | Built | Checks every approved piece against your supplier table: route, candidate suppliers, unit cost traced to a price you entered, gross margin worked out in code, and questions for the supplier. You decide per piece: continue, continue without a supplier (waitlist only), or drop |
| 4. Visual prototypes | Built | Five shots per piece (front, back, detail, on-model studio, on-model lifestyle) drawn by the image model the image test picked. Each image is checked by a separate reviewer against the specification, colour strictly, and redrawn with its correction up to three times. You approve, request changes or reject each piece. Stops at `FDW_VISUALS_LIMIT_USD` (default $15) per cycle |
| 5. 3D generation | Not built | Slice 3 |
| 6. Price test | Not built | Slice 3 |
| 7. Launch kit and learning | Not built | Slice 3 |

## Run it

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign in with the user you created.

### Or try it in the browser with GitHub Codespaces

On the repository page, switch to the branch you want to try, then choose
Code, Codespaces, Create codespace. Setup runs automatically. In the terminal
that opens:

```bash
python manage.py createsuperuser
python manage.py runserver
```

Open the forwarded address for port 8000 when it is offered, and sign in.

## Dry run and live mode

The app starts in **dry run** (`FDW_DRY_RUN=1`). No model is called and nothing is
spent. Stages return sample responses written by simple rules, so you can try every
screen and gate. Sample output is labelled as a sample and says nothing about real
demand.

To use real models, edit `.env`:

```
FDW_DRY_RUN=0
ANTHROPIC_API_KEY=your-key
```

The live model path has automated tests around it (validation, retries, spend limit,
search handling) but has not yet been run against the real API. Web search must be
enabled for your organisation in the Claude Console, which it is by default, and it
is not confirmed here that the configured model supports it; if it does not, stage 1
shows the API's error and saves nothing. Run one small cycle first and check
the "Model calls" record on the cycle page.

## Image model test

The Image test page compares image-generation models on pieces you have already made.

1. Add a piece with a front photo, and ideally detail and worn photos.
2. Claude studies the photos and writes a precise description, a scoring checklist and
   three shot prompts. The image models never see your photos, only the prompts, as they
   will for new designs.
3. Each chosen model draws a front packshot from the prompt alone, then a detail close-up
   and an on-model shot using its own packshot as reference.
4. You score each image against your real photo, 1 to 5 on four points, without seeing
   which model made it.
5. Results rank the models by how closely they matched your pieces, with cost and speed.

Image generation goes through OpenRouter. Set `OPENROUTER_API_KEY` in `.env`, and choose
the models in `FDW_BAKEOFF_MODELS`; "Check available models" lists what your key can
reach. Spending stops at `FDW_BAKEOFF_LIMIT_USD` (default $10). Photos and generated
images are stored in `media/`, which is not committed, and are shown only to signed-in
users.

## Reviewer check

Tools → Reviewer check runs the stage 4 image reviewer, with the same instructions and
model, on images you have scored in the image model test, and compares its verdicts with
yours. An image counts as one you would accept when all four of your scores are at least
`FDW_REVIEWER_ACCEPT_SCORE` (default 4). The reviewer can be relied on once it has seen at
least `FDW_REVIEWER_MIN_REJECTED` (default 15) images you would reject and failed at least
`FDW_REVIEWER_TARGET_CATCH` (default 90%) of them. Run it again whenever the reviewer's
prompt or model changes; the page says when a check is out of date. It shares the image
test's spending limit.

## Visual prototypes (stage 4)

After gate 3, press **Set up visual prototypes**, then **Make images**. The page makes
several images at once (`FDW_VISUAL_WORKERS`, default 3); keep the tab open while it
runs. Closing it loses nothing; press the button again to carry on.

For each piece kept at gate 3, Claude writes five shot prompts and a checklist from the
specification and the changes you accepted at sourcing. Shot A is drawn first; B and C
are drawn from A, D from A and B, E from A and D. Only images that passed review, or that
you kept, are used as references. A separate Claude call reviews every image against the
checklist; a failed image is redrawn with the reviewer's correction note, up to
`FDW_MAX_SHOT_ATTEMPTS` (default 3) times, and then reaches you marked "could not meet
standard" with its best attempt. If the front packshot cannot meet the standard, the
piece stops there and you choose whether to continue from its best attempt.

Stage 4 has its own budget, `FDW_VISUALS_LIMIT_USD` (default $15 per cycle): images as
OpenRouter reports them, plus the Claude calls that write and review them. Before each
image, the expected cost is set aside, so parallel work cannot pass the limit. The
research budget, `FDW_CYCLE_LIMIT_USD`, now covers stages 1 to 3 only.

The app checks at startup that its libraries are installed, and refuses to start with
the fix to run if one is missing.

## Guardrails

These are enforced in code, not by prompts.

- **Figures.** A number reaches a chart only if the same number appears in the source's
  evidence text. Opportunity scores, timeline levels and the outlook are labelled on screen
  as the researcher's assessment.
- **Sources.** A research finding reaches the brief only if its link is one the web
  search actually returned in that run. This is checked in code. Findings that fail
  are shown as left out.
- **Search cap.** One research run makes at most `FDW_RESEARCH_MAX_SEARCHES` searches
  (default 12). Searches are priced at `FDW_SEARCH_PRICE_USD` each and counted in the
  cycle's spend.
- **Gates.** Stage 2 refuses to run without an approved research brief. A brief can be
  decided once. Concept selection accepts one or two concepts from the same cycle.
- **Spend limit.** Every model call is recorded with its cost. Before each call the
  cycle's spend is checked against `FDW_CYCLE_LIMIT_USD` (default $5); at the limit,
  calls stop.
- **Validation.** Model output is checked against the expected structure. Invalid
  output gets one retry with the problems listed, then the stage stops and saves
  nothing.
- **No loops.** A concept with blocking issues is revised once. If issues remain it
  reaches you marked "unresolved".
- **Production tiers.** A hand technique cannot be assigned to an on-demand machine
  supplier, and only atelier pieces may be described as handmade. Both are checked in
  code as well as by the reviewer.
- **Untrusted input.** Web pages and pasted messages are treated as data. The research
  step can search the web and do nothing else; the extraction step has no tools, and prompt slots are filled in one pass so text inside a message cannot
  act as a slot.
- **Sign-in.** Every screen requires a signed-in user.

## Where things are

| File | What it holds |
| --- | --- |
| `workflow/prompts.py` | The prompts for stages 1 and 2 |
| `workflow/stages.py` | Stage logic and gates |
| `workflow/llm.py` | Model calls, cost logging, spend limit, JSON retry |
| `workflow/schemas.py` | Validation of model output and the code-level rules |
| `workflow/dryrun.py` | Sample responses for dry run |
| `workflow/models.py` | Database tables |
| `workflow/views.py`, `workflow/templates/` | Screens |
| `workflow/tests.py` | Tests |

## Updating an existing copy

If you ran the app before 12 November 2026 20:30, the database layout has changed.
The data in it was sample data, so start fresh:

```bash
rm db.sqlite3
python manage.py migrate
python manage.py createsuperuser
```

## Tests

```bash
python manage.py test
```

## Shortcuts in this slice

Each is deliberate and replaced in a later slice without changing the stage logic.

- SQLite database.
- Model calls run inside the web request, so a stage can take a minute or two while
  the page waits. A background job queue comes with slice 4.
- Audience messages, when used, are pasted by hand. Automated intake comes later.
- Models and prices are set in `fdw/settings.py` (`FDW_PRICES`). Update them when
  prices change; a model without a price is refused so spend is never untracked.

## Before using this beyond your own machine

Set `DJANGO_DEBUG=0`, a long random `DJANGO_SECRET_KEY`, and `DJANGO_ALLOWED_HOSTS`,
and serve it behind HTTPS.
