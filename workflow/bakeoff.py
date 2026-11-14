"""The image model bake-off.

The founder photographs finished pieces. Claude looks at the photos and
writes a precise description and three shot prompts. Each candidate image
model then draws the same shots through OpenRouter, the founder scores the
results without seeing which model made them, and the scores pick the model
for stage 4.

Generation runs one image per request, so a slow model never blocks the
page for long and a closed browser loses nothing: every image is a stored
job that is either waiting, made or failed.
"""
import base64
import io
import json
import random
import ssl
import string
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models import Sum
from django.utils import timezone

from . import prompts, schemas
from .llm import StageError, cost_of, get_client, parse_json
from .models import BakeoffImage, BakeoffPiece, ReviewerResult

# Photos straight from a phone or camera are accepted and made smaller here.
MAX_PHOTO_BYTES = 50 * 1024 * 1024
# The longest side Claude looks at in detail (Claude Sonnet 5.5 migration
# guide, read 13 November 2026). Anything larger is scaled down by the API
# anyway, so it would only cost upload time.
PHOTO_LONG_EDGE = 2576
# Claude rejects images above 5 MB, so a stored photo is kept under this.
PHOTO_TARGET_BYTES = int(4.5 * 1024 * 1024)
MAX_IMAGE_BYTES = 25 * 1024 * 1024
SHOT_ORDER = ("A", "C", "D")
# Which real photo each generated shot is compared with.
SHOT_PHOTO = {"A": "photo_front", "C": "photo_detail", "D": "photo_worn"}
IMAGE_TIMEOUT_SECONDS = 240

_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
)


class BakeoffError(StageError):
    """A bake-off step could not finish. The message is safe to show."""


class OutOfCredit(BakeoffError):
    """OpenRouter refused the request for lack of credit. Retrying will not help until credit is added."""


class TooLargeForBudget(OutOfCredit):
    """One request's estimated cost is bigger than OpenRouter's whole in-flight budget.

    OpenRouter estimates a request from its input, and reference images sent
    inline count as input, so smaller references can bring it back under.
    """


class BudgetBusy(BakeoffError):
    """OpenRouter's in-flight budget is full: requests already running hold the credit. Retry shortly.

    OpenRouter lets a prepaid balance hold only part of itself for requests that
    are still running. Several images made at once can fill it even when the
    balance is enough for each of them on its own.
    """

    def __init__(self, message, retry_after=10):
        super().__init__(message)
        self.retry_after = retry_after


# Waiting for the in-flight budget inside one request, before handing the wait back to the page.
BUDGET_RETRIES = 2
_sleep = time.sleep


# --- Photos -------------------------------------------------------------------


def sniff(data: bytes):
    """Return (media_type, extension) from the file's first bytes, or None."""
    for signature, media_type, ext in _SIGNATURES:
        if data.startswith(signature):
            return media_type, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def check_photo(upload):
    """Open an uploaded photo by its content, not its name, and store a web-sized JPEG.

    The photo is turned upright using its rotation tag, scaled down so its
    longest side is at most PHOTO_LONG_EDGE pixels, and saved without its
    metadata, so a phone's GPS location never leaves the app. Returns a
    ContentFile or raises BakeoffError with a message the founder can act on.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:  # HEIC support is optional; JPEG, PNG and WebP still work
        pass

    if upload.size > MAX_PHOTO_BYTES:
        raise BakeoffError(f"{upload.name} is larger than 50 MB. Export it smaller and try again.")
    try:
        with Image.open(upload) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened)
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                image = Image.new("RGB", rgba.size, (255, 255, 255))
                image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image = image.convert("RGB")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise BakeoffError(f"{upload.name} could not be read as a photo. Use JPEG, PNG, WebP or HEIC.") from None
    image.thumbnail((PHOTO_LONG_EDGE, PHOTO_LONG_EDGE), Image.Resampling.LANCZOS)
    for quality in (90, 82, 74, 66):
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
        if buffer.tell() <= PHOTO_TARGET_BYTES:
            break
    return ContentFile(buffer.getvalue(), name=f"{uuid.uuid4().hex}.jpg")


def _photo_payload(field_file):
    field_file.open("rb")
    try:
        data = field_file.read()
    finally:
        field_file.close()
    kind = sniff(data)
    if kind is None:
        raise BakeoffError("A stored photo is not a readable image. Upload it again.")
    return kind[0], base64.b64encode(data).decode("ascii")


# --- Spend ------------------------------------------------------------------------


def total_spend() -> Decimal:
    images = BakeoffImage.objects.aggregate(total=Sum("cost_usd"))["total"] or Decimal("0")
    describing = BakeoffPiece.objects.aggregate(total=Sum("prompt_cost_usd"))["total"] or Decimal("0")
    # The reviewer check on the Tools page shares this budget.
    reviewing = ReviewerResult.objects.aggregate(total=Sum("cost_usd"))["total"] or Decimal("0")
    return images + describing + reviewing


def check_spend():
    limit = settings.FDW_BAKEOFF_LIMIT_USD
    spent = total_spend()
    if spent >= limit:
        raise BakeoffError(
            f"The bake-off has spent ${spent:.2f} of its ${limit:.2f} limit, so nothing more was "
            "generated. Raise FDW_BAKEOFF_LIMIT_USD if you want to continue."
        )


# --- Step 1: Claude describes the piece and writes the prompts ------------------


def validate_spec(data):
    problems = []
    if not isinstance(data, dict):
        raise schemas.SchemaError(["Expected a JSON object."])
    if not schemas._is_text(data.get("description")):
        problems.append("description is missing")
    checklist = data.get("checklist")
    if not isinstance(checklist, list) or not checklist or not all(schemas._is_text(c) for c in checklist):
        problems.append("checklist must be a non-empty list of facts")
    shot_prompts = data.get("prompts")
    if not isinstance(shot_prompts, dict) or not all(schemas._is_text(shot_prompts.get(s)) for s in SHOT_ORDER):
        problems.append("prompts must contain non-empty A, C and D")
    if not isinstance(data.get("unclear_from_photos", []), list):
        problems.append("unclear_from_photos must be a list")
    if problems:
        raise schemas.SchemaError(problems)
    data.setdefault("unclear_from_photos", [])
    return data


def describe_piece(piece: BakeoffPiece, client=None) -> dict:
    """Have Claude look at the photos and write the description, checklist and prompts."""
    check_spend()
    client = client or get_client()
    photos = piece.photos()
    images = [_photo_payload(f) for _, _, f in photos]
    prompt = prompts.render(
        prompts.BAKEOFF_ART_DIRECTOR,
        realism_standard=prompts.REALISM_STANDARD,
        piece_name=piece.name,
        founder_notes=piece.notes.strip() or "None.",
        photo_list=", ".join(label for _, label, _ in photos) + " (attached, in this order)",
    )
    model = settings.FDW_MODEL_WRITER
    cost_of(model, 0, 0)  # refuses an unpriced model before anything is spent
    total_cost = Decimal("0")
    text = ""
    for attempt in range(2):
        attempt_prompt = prompt if attempt == 0 else (
            prompt + "\n\nYour previous answer could not be used. Return the complete JSON and nothing else."
        )
        result = client.complete("bakeoff_describe", attempt_prompt, model, settings.FDW_MAX_OUTPUT_TOKENS, images=images)
        if not getattr(client, "dry_run", False):
            total_cost += cost_of(model, result.input_tokens, result.output_tokens)
        text = result.text
        try:
            spec = validate_spec(parse_json(text))
            break
        except schemas.SchemaError:
            spec = None
    piece.prompt_cost_usd += total_cost
    if spec is None:
        piece.save(update_fields=["prompt_cost_usd"])
        raise BakeoffError("Claude's description of the piece could not be used twice. Nothing was queued.")
    piece.spec = spec
    piece.save(update_fields=["spec", "prompt_cost_usd"])
    return spec


# --- Step 2: queue one job per model and shot -------------------------------------


def _codes(count: int) -> list:
    """Neutral, shuffled labels such as K, Q, B, so position and name give nothing away."""
    letters = list(string.ascii_uppercase)
    random.SystemRandom().shuffle(letters)
    if count <= len(letters):
        return letters[:count]
    return [f"{a}{b}" for a in letters for b in letters][:count]


def queue_images(piece: BakeoffPiece, models_wanted) -> int:
    """Create the waiting jobs for the chosen models. Returns how many were added."""
    if not piece.spec:
        raise BakeoffError("Write the prompts before queueing images.")
    models_wanted = [m.strip() for m in models_wanted if m and m.strip()]
    if not models_wanted:
        raise BakeoffError("Choose at least one image model.")
    if len(models_wanted) > 8:
        raise BakeoffError("Compare at most eight models at a time.")
    shots = [s for s in SHOT_ORDER if s == "A" or getattr(piece, SHOT_PHOTO[s])]
    existing = set(piece.images.values_list("model", "shot"))
    wanted = [(m, s) for m in dict.fromkeys(models_wanted) for s in shots if (m, s) not in existing]
    if not wanted:
        return 0
    # Codes are drawn per shot, so within a shot the order of images says nothing about the model.
    used = {shot: set(piece.images.filter(shot=shot).values_list("code", flat=True)) for shot in shots}
    created = 0
    for shot in shots:
        jobs = [m for m, s in wanted if s == shot]
        codes = [c for c in _codes(len(jobs) + len(used[shot]) + 26) if c not in used[shot]][: len(jobs)]
        for model, code in zip(jobs, codes):
            BakeoffImage.objects.create(
                piece=piece, model=model, shot=shot, code=code, prompt=piece.spec["prompts"][shot],
            )
            created += 1
    return created


# --- Step 3: make the images, one per request ------------------------------------


def _tls_context() -> ssl.SSLContext:
    """A verified TLS context that works on every platform.

    Python from python.org on macOS does not read the system's certificates,
    so plain urllib fails with CERTIFICATE_VERIFY_FAILED there. The certifi
    bundle, which the Anthropic library already uses, fixes that. Verification
    stays on in every case.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class OpenRouterImages:
    """Image generation through OpenRouter's images endpoint."""

    dry_run = False

    def __init__(self):
        if not settings.OPENROUTER_API_KEY:
            raise BakeoffError(
                "No OPENROUTER_API_KEY is set. Add it to your .env file, or set FDW_DRY_RUN=1 "
                "to try the bake-off with sample images."
            )
        self.base = settings.OPENROUTER_BASE_URL.rstrip("/")
        self.tls = _tls_context()

    def _payment_error(self, exc, detail: str) -> BakeoffError:
        """Turn OpenRouter's 402 into an error that says what happened and what to do.

        OpenRouter rejects these before any provider is called, so nothing is charged.
        """
        try:
            error = json.loads(detail).get("error") or {}
        except (ValueError, AttributeError):
            error = {}
        meta = error.get("metadata") or {}
        reason = str(meta.get("reason") or "")
        source = str(meta.get("limit_source") or "")
        if reason == "in_flight_budget_exhausted" or source == "openrouter_in_flight_budget":
            try:
                wait = int(float(exc.headers.get("Retry-After") or 10))
            except (TypeError, ValueError):
                wait = 10
            return BudgetBusy(
                "OpenRouter returned 402: the images already being made are holding all the credit "
                "OpenRouter lets your balance hold at once. Nothing was charged.",
                retry_after=max(1, min(wait, 60)),
            )
        if source == "openrouter_key_limit":
            text = ("This OpenRouter key has reached its own credit limit. Raise the key's limit, or wait for it to "
                    "reset, at https://openrouter.ai/settings/keys.")
        elif reason == "weight_exceeds_budget":
            return TooLargeForBudget(
                "OpenRouter returned 402. OpenRouter estimates this image, with its reference images, at more than "
                "it will hold against your current balance at once. Adding credit at "
                "https://openrouter.ai/settings/credits removes the limit. Nothing was charged for this image; "
                "press the button again once that is done."
            )
        else:
            text = "Your OpenRouter balance cannot cover the next image. Add credits at https://openrouter.ai/settings/credits."
        return OutOfCredit(f"OpenRouter returned 402. {text} Nothing was charged for this image; "
                           "press the button again once that is done.")

    def _request(self, method, path, payload=None, timeout=IMAGE_TIMEOUT_SECONDS):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}", data=body, method=method,
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "X-Title": "Malina studio",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.tls) as response:
                raw = response.read(MAX_IMAGE_BYTES * 2)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            if exc.code == 402:
                raise self._payment_error(exc, detail) from exc
            raise BakeoffError(f"OpenRouter returned {exc.code}: {detail[:400]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise BakeoffError(
                    "Could not verify OpenRouter's security certificate. Run "
                    "'pip install --upgrade certifi' in the app's environment and try again."
                ) from exc
            raise BakeoffError(f"Could not reach OpenRouter: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BakeoffError("OpenRouter's answer was not readable.") from exc

    def _send_image_request(self, payload):
        """POST one image request, waiting out OpenRouter's in-flight budget a few times before giving up."""
        for attempt in range(BUDGET_RETRIES + 1):
            try:
                return self._request("POST", "/images", payload)
            except BudgetBusy as exc:
                if attempt == BUDGET_RETRIES:
                    raise
                _sleep(exc.retry_after)

    def generate(self, model, prompt, references):
        payload = {"model": model, "prompt": prompt, "n": 1,
                   "resolution": settings.FDW_IMAGE_RESOLUTION, "aspect_ratio": "4:5"}
        if references:
            payload["input_references"] = [{"type": "image_url", "image_url": {"url": r}} for r in references]
        try:
            data = self._send_image_request(payload)
        except BakeoffError as exc:
            if "returned 400" not in str(exc):
                raise
            # Some providers reject a size or aspect they do not support; try their defaults.
            payload.pop("resolution", None)
            payload.pop("aspect_ratio", None)
            data = self._send_image_request(payload)
        items = data.get("data") or []
        if not items:
            raise BakeoffError(f"{model} returned no image.")
        b64 = items[0].get("b64_json")
        if not b64:
            raise BakeoffError(f"{model} did not return the image data.")
        image = base64.b64decode(b64)
        if len(image) > MAX_IMAGE_BYTES:
            raise BakeoffError(f"{model} returned an image larger than 25 MB.")
        kind = sniff(image)
        if kind is None:
            raise BakeoffError(f"{model} returned a file that is not a JPEG, PNG or WebP image.")
        cost = None
        reported = (data.get("usage") or {}).get("cost")
        if reported is not None:
            try:
                cost = Decimal(str(reported)).quantize(Decimal("0.0001"))
            except InvalidOperation:
                cost = None
        return image, kind, cost

    def list_models(self):
        data = self._request("GET", "/images/models", timeout=30)
        items = data.get("data") if isinstance(data, dict) else data
        models = []
        for item in items or []:
            if isinstance(item, dict) and item.get("id"):
                models.append({"id": str(item["id"]), "name": str(item.get("name") or "")})
        return sorted(models, key=lambda m: m["id"])


class SampleImages:
    """Dry-run stand-in: a labelled placeholder instead of a real image, at no cost."""

    dry_run = True
    COLOURS = ("#C9B8A6", "#8FA39A", "#B4919A", "#9AA6C2", "#C2B48E", "#A99DBF")

    def generate(self, model, prompt, references):
        colour = self.COLOURS[sum(map(ord, model)) % len(self.COLOURS)]
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="1000" viewBox="0 0 800 1000">'
            f'<rect width="800" height="1000" fill="{colour}"/>'
            '<text x="400" y="480" font-family="sans-serif" font-size="44" text-anchor="middle" fill="#1b2140">Sample image</text>'
            '<text x="400" y="540" font-family="sans-serif" font-size="26" text-anchor="middle" fill="#1b2140">Dry run: no model was called</text>'
            "</svg>"
        ).encode()
        return svg, ("image/svg+xml", "svg"), None

    def list_models(self):
        return [{"id": m, "name": "Sample list (dry run)"} for m in settings.FDW_BAKEOFF_MODELS]


def get_image_client():
    return SampleImages() if settings.FDW_DRY_RUN else OpenRouterImages()


def reference_url(field_file, edge=None):
    """A reference image as a small JPEG data URL.

    References are sent inside the request, and OpenRouter counts them toward
    the request's estimated cost, so a full-size PNG can make one image look
    too expensive to a small balance. The image model only needs enough
    detail to keep the piece the same.
    """
    from PIL import Image

    field_file.open("rb")
    try:
        data = field_file.read()
    finally:
        field_file.close()
    if sniff(data) is None:
        return None
    edge = edge or settings.FDW_REFERENCE_EDGE
    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        image = opened.convert("RGB")
    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def next_job(piece: BakeoffPiece):
    """The next image that can be made: front shots first, then shots that use them as reference."""
    for image in piece.images.filter(status=BakeoffImage.Status.PENDING).order_by("shot", "id"):
        if image.shot == "A":
            return image
        front = piece.images.filter(model=image.model, shot="A").first()
        if front is None or front.status == BakeoffImage.Status.FAILED:
            image.status = BakeoffImage.Status.FAILED
            image.error = "Needs this model's front packshot, which failed."
            image.save(update_fields=["status", "error"])
            continue
        if front.status == BakeoffImage.Status.DONE:
            return image
    return None


def make_next_image(piece: BakeoffPiece, client=None):
    """Make one waiting image. Returns the image, or None when nothing is left to make."""
    image = next_job(piece)
    if image is None:
        return None
    check_spend()
    client = client or get_image_client()
    references = []
    if image.shot != "A":
        front = piece.images.get(model=image.model, shot="A")
        reference = reference_url(front.file) if not getattr(client, "dry_run", False) else None
        if reference:
            references.append(reference)
    started = time.monotonic()
    try:
        data, (media_type, ext), cost = client.generate(image.model, image.prompt, references)
    except BakeoffError as exc:
        image.status = BakeoffImage.Status.FAILED
        image.error = str(exc)[:2000]
        image.seconds = round(time.monotonic() - started, 1)
        image.save(update_fields=["status", "error", "seconds"])
        return image
    image.file.save(f"{uuid.uuid4().hex}.{ext}", ContentFile(data), save=False)
    image.status = BakeoffImage.Status.DONE
    image.cost_usd = cost
    image.error = ""
    image.seconds = round(time.monotonic() - started, 1)
    image.save()
    return image


def retry_failed(piece: BakeoffPiece) -> int:
    return piece.images.filter(status=BakeoffImage.Status.FAILED).update(status=BakeoffImage.Status.PENDING, error="")


# --- Step 4: scores and results -------------------------------------------------------


def save_scores(image: BakeoffImage, values: dict):
    for field in BakeoffImage.SCORE_FIELDS:
        raw = values.get(field)
        if raw in (None, ""):
            raise BakeoffError("Give all four scores before saving.")
        try:
            score = int(raw)
        except (TypeError, ValueError):
            raise BakeoffError("Scores must be whole numbers from 1 to 5.") from None
        if not 1 <= score <= 5:
            raise BakeoffError("Scores must be whole numbers from 1 to 5.")
        setattr(image, field, score)
    image.scored_at = timezone.now()
    image.save(update_fields=list(BakeoffImage.SCORE_FIELDS) + ["scored_at"])


def results():
    """Scores by model across every piece, best match with the real piece first."""
    rows = {}
    for image in BakeoffImage.objects.all():
        row = rows.setdefault(image.model, {
            "model": image.model, "made": 0, "failed": 0, "scored": 0,
            "sums": {f: 0 for f in BakeoffImage.SCORE_FIELDS}, "cost": Decimal("0"),
            "costed": 0, "seconds": 0.0,
        })
        if image.status == BakeoffImage.Status.DONE:
            row["made"] += 1
            row["seconds"] += image.seconds or 0
            if image.cost_usd is not None:
                row["cost"] += image.cost_usd
                row["costed"] += 1
        elif image.status == BakeoffImage.Status.FAILED:
            row["failed"] += 1
        if image.is_scored:
            row["scored"] += 1
            for f in BakeoffImage.SCORE_FIELDS:
                row["sums"][f] += getattr(image, f)
    out = []
    for row in rows.values():
        n = row["scored"]
        averages = {f: round(row["sums"][f] / n, 2) if n else None for f in BakeoffImage.SCORE_FIELDS}
        scored_values = [v for v in averages.values() if v is not None]
        out.append({
            **row,
            "averages": averages,
            "overall": round(sum(scored_values) / len(scored_values), 2) if scored_values else None,
            "cost_per_image": (row["cost"] / row["costed"]).quantize(Decimal("0.0001")) if row["costed"] else None,
            "seconds_per_image": round(row["seconds"] / row["made"], 1) if row["made"] else None,
        })
    out.sort(key=lambda r: (
        r["averages"]["matches_piece"] is None,
        -(r["averages"]["matches_piece"] or 0),
        -(r["overall"] or 0),
    ))
    if out and out[0]["averages"]["matches_piece"] is not None:
        out[0]["leader"] = True
    return out
