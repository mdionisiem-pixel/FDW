# FDW: Malina design-to-launch workflow

A Django application that researches what people are buying now and designs new
collections from it. You approve every step.

This is **slice 1** of the specification. It covers the first two of seven stages:

| Stage | Status | What it does |
| --- | --- | --- |
| 1. Market research | Built | Searches the web for current demand, checks every source, and writes a research brief. It compares the target season across the last two to three years with what is rising now. Nothing needs to be typed; a target season, a direction and audience messages are optional |
| 2. Collection concepts | Built | Plans three new collections in one call, then specifies each in its own call so no answer is cut off; reviews each one separately; you pick one or two |
| 3. Sourcing check | Not built | Slice 2 |
| 4. Visual prototypes | Not built | Slice 2 |
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

## Guardrails

These are enforced in code, not by prompts.

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
