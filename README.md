# FDW: Malina design-to-launch workflow

A Django application that turns what Malina's audience asks for into collection
concepts you can choose between. You approve every step.

This is **slice 1** of the specification. It covers the first two of seven stages:

| Stage | Status | What it does |
| --- | --- | --- |
| 1. Demand signals | Built | Extracts signals from pasted messages and writes a demand brief |
| 2. Collection concepts | Built | Designs three collections, reviews each one separately, you pick one or two |
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

The live model path has automated tests around it (validation, retries, spend limit)
but has not yet been run against the real API. Run one small cycle first and check
the "Model calls" record on the cycle page.

## Guardrails

These are enforced in code, not by prompts.

- **Gates.** Stage 2 refuses to run without an approved demand brief. A brief can be
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
- **Untrusted input.** Pasted messages are treated as data. The extraction step has
  no tools, and prompt slots are filled in one pass so text inside a message cannot
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
- Signals are pasted by hand. Automated intake comes later.
- Models and prices are set in `fdw/settings.py` (`FDW_PRICES`). Update them when
  prices change; a model without a price is refused so spend is never untracked.

## Before using this beyond your own machine

Set `DJANGO_DEBUG=0`, a long random `DJANGO_SECRET_KEY`, and `DJANGO_ALLOWED_HOSTS`,
and serve it behind HTTPS.
