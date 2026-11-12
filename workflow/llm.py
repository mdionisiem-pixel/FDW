"""Calls to the language model, with cost logging and a hard spend limit.

Every call is recorded as a ModelCall row whether it succeeds or fails, so
any output can be traced to the prompt that produced it, and the spend limit
is checked against real recorded cost before each call.
"""
import json
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

from . import dryrun
from .models import ModelCall
from .schemas import SchemaError


class StageError(Exception):
    """A stage could not finish. The message is safe to show to the user."""


class SpendLimitError(StageError):
    """The cycle has reached its spend limit; no further calls are made."""


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int


class AnthropicClient:
    """Thin wrapper over the Anthropic Messages API."""

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
        self.dry_run = False

    def complete(self, step: str, prompt: str, model: str, max_tokens: int) -> LLMResult:
        try:
            # Streaming avoids HTTP timeouts on long responses such as three
            # full collection concepts.
            with self._client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
        except self._anthropic.APIError as exc:
            raise StageError(f"The model call for '{step}' failed: {exc}") from exc
        text = "".join(block.text for block in message.content if block.type == "text")
        if message.stop_reason == "max_tokens":
            raise StageError(
                f"The model's answer for '{step}' was cut off at {max_tokens} tokens. "
                "Nothing was saved."
            )
        return LLMResult(text, message.usage.input_tokens, message.usage.output_tokens)


class DryRunClient:
    """Returns sample responses so the flow can be tried without an API key."""

    dry_run = True

    def complete(self, step: str, prompt: str, model: str, max_tokens: int) -> LLMResult:
        text = dryrun.respond(step, prompt)
        return LLMResult(text, len(prompt) // 4, len(text) // 4)


def get_client():
    return DryRunClient() if settings.FDW_DRY_RUN else AnthropicClient()


def cost_of(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    prices = settings.FDW_PRICES.get(model)
    if prices is None:
        raise StageError(
            f"No price is configured for model '{model}'. Add it to FDW_PRICES in settings "
            "so spend can be tracked."
        )
    price_in, price_out = prices
    million = Decimal("1000000")
    cost = Decimal(input_tokens) / million * price_in + Decimal(output_tokens) / million * price_out
    return cost.quantize(Decimal("0.0001"))


def check_spend(cycle) -> None:
    limit = settings.FDW_CYCLE_LIMIT_USD
    spent = cycle.spend_usd
    if spent >= limit:
        raise SpendLimitError(
            f"This cycle has spent ${spent} of its ${limit} limit, so no further model calls "
            "were made. Raise FDW_CYCLE_LIMIT_USD if you want to continue."
        )


def run_call(cycle, step: str, prompt: str, model: str, max_tokens: int, client) -> str:
    """Make one model call for a cycle and record it."""
    check_spend(cycle)
    if model not in settings.FDW_PRICES:
        cost_of(model, 0, 0)  # raises a clear error before any money is spent
    try:
        result = client.complete(step, prompt, model, max_tokens)
    except StageError as exc:
        ModelCall.objects.create(
            cycle=cycle, step=step, model=model, prompt=prompt, ok=False, error=str(exc),
            dry_run=getattr(client, "dry_run", False),
        )
        raise
    is_dry = getattr(client, "dry_run", False)
    ModelCall.objects.create(
        cycle=cycle,
        step=step,
        model=model,
        prompt=prompt,
        response=result.text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=Decimal("0") if is_dry else cost_of(model, result.input_tokens, result.output_tokens),
        dry_run=is_dry,
    )
    return result.text


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


def run_json(cycle, step: str, prompt: str, model: str, max_tokens: int, validator, client):
    """Call the model and return validated JSON.

    If the first answer is malformed or fails validation, the model gets one
    more attempt with the problems listed. After that the stage stops, because
    saving unvalidated output would pass errors on to every later stage.
    """
    text = run_call(cycle, step, prompt, model, max_tokens, client)
    try:
        return validator(parse_json(text))
    except SchemaError as first:
        retry_prompt = (
            prompt
            + "\n\nYour previous answer could not be used. Problems found:\n- "
            + "\n- ".join(first.problems[:20])
            + "\n\nReturn the complete corrected JSON and nothing else."
        )
        text = run_call(cycle, f"{step}_retry", retry_prompt, model, max_tokens, client)
        try:
            return validator(parse_json(text))
        except SchemaError as second:
            raise StageError(
                f"The model's output for '{step}' was invalid twice, so nothing was saved. "
                f"First problems: {'; '.join(second.problems[:5])}"
            ) from second
