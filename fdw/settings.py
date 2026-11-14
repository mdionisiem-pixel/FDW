"""Settings for the Malina design-to-launch workflow (FDW).

Configuration comes from environment variables so that secrets never live in
the repository. See .env.example for the full list.
"""
import os
import sys
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines from a local .env file, without overriding real env vars."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


DEBUG = _flag("DJANGO_DEBUG", "1")
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("Set DJANGO_SECRET_KEY before running with DJANGO_DEBUG=0.")
    SECRET_KEY = "dev-only-key-not-for-production"

ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

# GitHub Codespaces serves the app through a forwarded HTTPS address. Trust
# that one address, so forms work when the app is tried in a codespace.
if os.environ.get("CODESPACE_NAME"):
    _codespace_host = "{}-8000.{}".format(
        os.environ["CODESPACE_NAME"],
        os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev"),
    )
    ALLOWED_HOSTS.append(_codespace_host)
    CSRF_TRUSTED_ORIGINS.append(f"https://{_codespace_host}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "workflow",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "fdw.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "fdw.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        # Parallel model calls each record their result; wait for a write lock
        # instead of failing at once. IMMEDIATE makes every transaction take the
        # write lock when it starts. Without it, a transaction that reads first
        # and writes later fails at once with "database is locked" when another
        # request is writing, because SQLite cannot make it wait safely.
        "OPTIONS": {"timeout": 30, "transaction_mode": "IMMEDIATE"},
        # Tests use a file too, like the real app, so parallel calls are tested
        # against the same locking behaviour. Django deletes it after the run.
        "TEST": {"NAME": BASE_DIR / "test_db.sqlite3"},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Europe/Dublin")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

# Uploaded photos and generated images. Served only to signed-in users,
# through the app's own views, never as public files.
MEDIA_ROOT = Path(os.environ.get("FDW_MEDIA_ROOT", BASE_DIR / "media"))
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/admin/login/"

# --- Workflow configuration -------------------------------------------------

# Dry run uses canned model responses, so the whole flow can be clicked
# through without an API key and without spending anything.
FDW_DRY_RUN = _flag("FDW_DRY_RUN", "1")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Model per job. The writing and reviewing steps use the stronger model;
# signal extraction is high-volume and simple, so it uses the small one.
FDW_MODEL_WRITER = os.environ.get("FDW_MODEL_WRITER", "claude-sonnet-5-5")
FDW_MODEL_EXTRACTOR = os.environ.get("FDW_MODEL_EXTRACTOR", "claude-haiku-5-5")

# US dollars per million tokens (input, output). Source: Claude pricing page,
# read 12 November 2026. Update here when prices change.
FDW_PRICES = {
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
}

# Ceiling on output tokens for one model call. On current Claude models the
# model's own reasoning counts toward this limit as well as the answer, so a
# low ceiling cuts answers off. Anthropic's guidance (models docs, read
# 13 November 2026) is 64,000 for agentic work; Sonnet 5.5 allows 128,000.
# Only tokens actually produced are billed, and the cycle spend limit below
# still stops the money.
FDW_MAX_OUTPUT_TOKENS = int(os.environ.get("FDW_MAX_OUTPUT_TOKENS", "64000"))

# How much the model reasons before answering a review step (low, medium,
# high). Reviews follow a fixed checklist, so they use less than the design
# steps, which keep the model's default. Empty means the model's default.
FDW_REVIEW_EFFORT = os.environ.get("FDW_REVIEW_EFFORT", "medium").strip().lower()

# How many independent model calls may run at the same time, for example the
# three collections in stage 2. 1 runs them one after another.
# The test suite runs them one at a time, because its in-memory database is
# not shared safely between threads; one test switches parallel calls on.
_RUNNING_TESTS = len(sys.argv) > 1 and sys.argv[1] == "test"
FDW_PARALLEL_CALLS = int(os.environ.get("FDW_PARALLEL_CALLS", "1" if _RUNNING_TESTS else "3"))

# Hard stop: no model call is made for a cycle once its recorded spend
# reaches this amount.
FDW_CYCLE_LIMIT_USD = Decimal(os.environ.get("FDW_CYCLE_LIMIT_USD", "5"))

# How many raw messages are sent to the extractor in one call.
FDW_EXTRACT_BATCH_SIZE = int(os.environ.get("FDW_EXTRACT_BATCH_SIZE", "40"))

# Stage 1 market research uses the model's web search tool.
# Price per search in US dollars. Source: Claude web search tool docs
# ($10 per 1,000 searches), read 12 November 2026.
FDW_SEARCH_PRICE_USD = Decimal(os.environ.get("FDW_SEARCH_PRICE_USD", "0.01"))
# Hard cap on searches in one research run.
FDW_RESEARCH_MAX_SEARCHES = int(os.environ.get("FDW_RESEARCH_MAX_SEARCHES", "12"))
FDW_WEB_SEARCH_TOOL = os.environ.get("FDW_WEB_SEARCH_TOOL", "web_search_20250305")

# --- Stage 3: sourcing check ------------------------------------------------

# A piece whose gross margin falls below this share of its price is flagged
# for the founder. A working assumption, not a rule from a source: change it
# in .env to match Malina's own pricing.
FDW_MIN_GROSS_MARGIN = Decimal(os.environ.get("FDW_MIN_GROSS_MARGIN", "0.5"))


def _eur_rates(text: str) -> dict:
    """Parse 'USD=0.86,GBP=1.17' into {'USD': Decimal('0.86'), ...}. EUR is always 1."""
    rates = {"EUR": Decimal("1")}
    for part in text.split(","):
        code, _, value = part.partition("=")
        code, value = code.strip().upper(), value.strip()
        if code and value:
            rates[code] = Decimal(value)
    return rates


# Euros per unit of each other currency, used only to compare a supplier's
# price with a retail price in euros. No rate is built in, because a stale
# rate would give a wrong margin; without one, the margin is left blank.
FDW_EUR_RATES = _eur_rates(os.environ.get("FDW_EUR_RATES", ""))

# --- Image generation (OpenRouter) and the image model bake-off ------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Image models to compare. Model names come from OpenRouter's comparison of
# September 2026; use "Check available models" on the bake-off page to see
# what your key can reach today, and change this list in .env.
FDW_BAKEOFF_MODELS = [
    m.strip() for m in os.environ.get(
        "FDW_BAKEOFF_MODELS",
        "openai/gpt-image-2,black-forest-labs/flux.2-pro,google/gemini-3.1-flash-image,google/gemini-3-pro-image",
    ).split(",") if m.strip()
]
FDW_IMAGE_RESOLUTION = os.environ.get("FDW_IMAGE_RESOLUTION", "2K")
# Hard stop for the whole bake-off, in US dollars. Image costs are as
# reported by OpenRouter; the Claude call that writes the prompts counts too.
FDW_BAKEOFF_LIMIT_USD = Decimal(os.environ.get("FDW_BAKEOFF_LIMIT_USD", "10"))

# --- Stage 4: visual prototypes -------------------------------------------------

# The image model chosen by the bake-off of 14 November 2026: Gemini 3 Pro
# scored highest on matching the real piece (4.67 of 5 on average).
FDW_IMAGE_MODEL = os.environ.get("FDW_IMAGE_MODEL", "google/gemini-3-pro-image")
# Hard stop for stage 4 in one cycle, in US dollars: images plus the Claude
# calls that write and review them. Set by the founder on 14 November 2026.
FDW_VISUALS_LIMIT_USD = Decimal(os.environ.get("FDW_VISUALS_LIMIT_USD", "15"))
# What one image is assumed to cost when OpenRouter does not report a cost,
# and what is set aside before each image so parallel work cannot overshoot
# the limit. The bake-off measured $0.1358 per Gemini 3 Pro image.
FDW_IMAGE_COST_ESTIMATE_USD = Decimal(os.environ.get("FDW_IMAGE_COST_ESTIMATE_USD", "0.14"))
# Set aside before each Claude call in stage 4 (prompt writing or one image review).
FDW_VISUAL_CALL_ESTIMATE_USD = Decimal(os.environ.get("FDW_VISUAL_CALL_ESTIMATE_USD", "0.08"))
# Attempts per shot before it goes to the founder marked "could not meet standard".
FDW_MAX_SHOT_ATTEMPTS = int(os.environ.get("FDW_MAX_SHOT_ATTEMPTS", "3"))
# How many images the page makes at the same time. Pieces do not depend on
# each other, so several can be worked on at once.
FDW_VISUAL_WORKERS = int(os.environ.get("FDW_VISUAL_WORKERS", "2"))
# Longest side, in pixels, of the reference images sent with shots B to E (and
# the image test's C and D). They are sent inside the request, and OpenRouter
# counts them toward its cost estimate, so smaller references keep requests
# within a small balance. If OpenRouter still refuses one as too large, it is
# retried once with references at the fallback size.
FDW_REFERENCE_EDGE = int(os.environ.get("FDW_REFERENCE_EDGE", "1024"))
FDW_REFERENCE_FALLBACK_EDGE = int(os.environ.get("FDW_REFERENCE_FALLBACK_EDGE", "640"))
# Longest side, in pixels, of the images the reviewer looks at. Larger shows
# more stitch detail and costs more per review.
FDW_REVIEW_IMAGE_EDGE = int(os.environ.get("FDW_REVIEW_IMAGE_EDGE", "1568"))

# --- Reviewer check (Tools page) ---------------------------------------------------

# An image the founder scored at least this on all four points counts as one
# they would accept; anything lower, as one they would reject.
FDW_REVIEWER_ACCEPT_SCORE = int(os.environ.get("FDW_REVIEWER_ACCEPT_SCORE", "4"))
# The reviewer is trusted once it has been tested on at least this many images
# the founder rejected, and failed at least this share of them. From the
# workflow specification: about 30 scored images, half of them rejected, and
# at least 9 in 10 rejected images caught.
FDW_REVIEWER_MIN_REJECTED = int(os.environ.get("FDW_REVIEWER_MIN_REJECTED", "15"))
FDW_REVIEWER_TARGET_CATCH = Decimal(os.environ.get("FDW_REVIEWER_TARGET_CATCH", "0.9"))
