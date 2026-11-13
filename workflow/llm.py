"""Calls to the language model, with cost logging and a hard spend limit.

Every call is recorded as a ModelCall row whether it succeeds or fails, so
any output can be traced to the prompt that produced it, and the spend limit
is checked against real recorded cost before each call.
"""
import json
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings

from . import dryrun
from .models import ModelCall
from .schemas import SchemaError

MAX_PAUSE_CONTINUATIONS = 4


class StageError(Exception):
    """A stage could not finish. The message is safe to show to the user."""


class SpendLimitError(StageError):
    """The cycle has reached its spend limit; no further calls are made."""


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    web_searches: int = 0
    # URLs the search tool actually returned. Findings are checked against these.
    source_urls: list = field(default_factory=list)


class AnthropicClient:
    """Thin wrapper over the Anthropic Messages API."""

    dry_run = False

    def __init__(self):
        if not settings.ANTHROPIC_API_KEY:
            raise StageError(
                "No ANTHROPIC_API_KEY is set. Add it to your .env file, "
                "or set FDW_DRY_RUN=1 to try the flow with sample responses."
            )
        import anthropic  # imported here so dry runs work without the package configured

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY, max_retries=3, timeout=600.0
        )

    def complete(self, step: str, prompt: str, model: str, max_tokens: int,
                 web_search_max: int = 0, images=None, effort: str = "") -> LLMResult:
        """Run one request. With ``web_search_max`` above zero the model may search the web.

        ``effort`` (low, medium, high) sets how much the model reasons before it
        answers. Empty means the model's own default.

        Web search is the only tool ever offered, and only to the research
        step, so text found on a web page has nothing else it could trigger.
        ``images`` is an optional list of (media_type, base64 data) the model can look at.
        """
        kwargs = {"model": model, "max_tokens": max_tokens}
        if effort:
            # Sent as a raw field so it works whichever SDK version is installed.
            kwargs["extra_body"] = {"output_config": {"effort": effort}}
        if web_search_max > 0:
            kwargs["tools"] = [{
                "type": settings.FDW_WEB_SEARCH_TOOL,
                "name": "web_search",
                "max_uses": web_search_max,
            }]
        if images:
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
                for media_type, data in images
            ] + [{"type": "text", "text": prompt}]
        else:
            content = prompt
        messages = [{"role": "user", "content": content}]
        result = LLMResult("", 0, 0)
        for _ in range(MAX_PAUSE_CONTINUATIONS + 1):
            try:
                # Streaming avoids HTTP timeouts on long responses.
                with self._client.messages.stream(messages=messages, **kwargs) as stream:
                    message = stream.get_final_message()
            except self._anthropic.APIError as exc:
                raise StageError(f"The model call for '{step}' failed: {exc}") from exc
            self._collect(message, result)
            if message.stop_reason == "pause_turn":
                # A long search turn was paused. Send the assistant turn back
                # unchanged so the model continues where it stopped.
                messages = messages + [{"role": "assistant", "content": message.content}]
                continue
            if message.stop_reason == "max_tokens":
                raise StageError(
                    f"The model's answer for '{step}' was cut off at {max_tokens} tokens, "
                    "which includes its reasoning. Nothing was saved. Raise FDW_MAX_OUTPUT_TOKENS "
                    "in .env (the model allows up to 128000) and run the step again."
                )
            return result
        raise StageError(f"The model call for '{step}' paused too many times. Nothing was saved.")

    @staticmethod
    def _collect(message, result: LLMResult) -> None:
        for block in message.content:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                result.text += block.text
            elif block_type == "web_search_tool_result":
                content = getattr(block, "content", None)
                if isinstance(content, list):
                    for item in content:
                        url = getattr(item, "url", None)
                        if url:
                            result.source_urls.append(url)
        usage = message.usage
        result.input_tokens += getattr(usage, "input_tokens", 0) or 0
        result.output_tokens += getattr(usage, "output_tokens", 0) or 0
        server_tools = getattr(usage, "server_tool_use", None)
        result.web_searches += getattr(server_tools, "web_search_requests", 0) or 0


class DryRunClient:
    """Returns sample responses so the flow can be tried without an API key."""

    dry_run = True

    def complete(self, step: str, prompt: str, model: str, max_tokens: int,
                 web_search_max: int = 0, images=None, effort: str = "") -> LLMResult:
        text = dryrun.respond(step, prompt)
        urls = dryrun.sample_source_urls() if web_search_max > 0 else []
        return LLMResult(text, len(prompt) // 4, len(text) // 4,
                         web_searches=3 if web_search_max > 0 else 0, source_urls=urls)


def get_client():
    return DryRunClient() if settings.FDW_DRY_RUN else AnthropicClient()


def cost_of(model: str, input_tokens: int, output_tokens: int, web_searches: int = 0) -> Decimal:
    prices = settings.FDW_PRICES.get(model)
    if prices is None:
        raise StageError(
            f"No price is configured for model '{model}'. Add it to FDW_PRICES in settings "
            "so spend can be tracked."
        )
    price_in, price_out = prices
    million = Decimal("1000000")
    cost = (
        Decimal(input_tokens) / million * price_in
        + Decimal(output_tokens) / million * price_out
        + Decimal(web_searches) * settings.FDW_SEARCH_PRICE_USD
    )
    return cost.quantize(Decimal("0.0001"))


def check_spend(cycle) -> None:
    limit = settings.FDW_CYCLE_LIMIT_USD
    spent = cycle.spend_usd
    if spent >= limit:
        raise SpendLimitError(
            f"This cycle has spent ${spent} of its ${limit} limit, so no further model calls "
            "were made. Raise FDW_CYCLE_LIMIT_USD if you want to continue."
        )


def run_result(cycle, step: str, prompt: str, model: str, max_tokens: int, client,
               web_search_max: int = 0, effort: str = "") -> LLMResult:
    """Make one model call for a cycle, record it, and return the full result."""
    check_spend(cycle)
    cost_of(model, 0, 0)  # raises a clear error for an unpriced model before any money is spent
    is_dry = getattr(client, "dry_run", False)
    try:
        options = {}
        if web_search_max > 0:
            options["web_search_max"] = web_search_max
        if effort:
            options["effort"] = effort
        result = client.complete(step, prompt, model, max_tokens, **options)
    except StageError as exc:
        ModelCall.objects.create(
            cycle=cycle, step=step, model=model, prompt=prompt, ok=False, error=str(exc), dry_run=is_dry,
        )
        raise
    ModelCall.objects.create(
        cycle=cycle,
        step=step,
        model=model,
        prompt=prompt,
        response=result.text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        web_searches=result.web_searches,
        cost_usd=Decimal("0") if is_dry else cost_of(
            model, result.input_tokens, result.output_tokens, result.web_searches
        ),
        dry_run=is_dry,
    )
    return result


def run_call(cycle, step: str, prompt: str, model: str, max_tokens: int, client, effort: str = "") -> str:
    """Make one model call for a cycle and return its text."""
    return run_result(cycle, step, prompt, model, max_tokens, client, effort=effort).text


def parse_json(text: str):
    """Extract the JSON value from a model response.

    Models sometimes wrap JSON in a code fence or add a sentence around it,
    so this finds the outermost object or array and parses that.
    """
    stripped = text.strip()
    starts = [i for i in (stripped.find("{"), stripped.find("[")) if i != -1]
    if not starts:
        raise SchemaError(["The response contained no JSON."])
    start = min(starts)
    closer = "}" if stripped[start] == "{" else "]"
    end = stripped.rfind(closer)
    if end <= start:
        raise SchemaError(["The response contained incomplete JSON."])
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SchemaError([f"The response was not valid JSON: {exc}"]) from exc


def run_json(cycle, step: str, prompt: str, model: str, max_tokens: int, validator, client, effort: str = ""):
    """Call the model and return validated JSON.

    If the first answer is malformed or fails validation, the model gets one
    more attempt with the problems listed. After that the stage stops, because
    saving unvalidated output would pass errors on to every later stage.
    """
    text = run_call(cycle, step, prompt, model, max_tokens, client, effort)
    try:
        return validator(parse_json(text))
    except SchemaError as first:
        retry_prompt = (
            prompt
            + "\n\nYour previous answer could not be used. Problems found:\n- "
            + "\n- ".join(first.problems[:20])
            + "\n\nReturn the complete corrected JSON and nothing else."
        )
        text = run_call(cycle, f"{step}_retry", retry_prompt, model, max_tokens, client, effort)
        try:
            return validator(parse_json(text))
        except SchemaError as second:
            raise StageError(
                f"The model's output for '{step}' was invalid twice, so nothing was saved. "
                f"First problems: {'; '.join(second.problems[:5])}"
            ) from second
