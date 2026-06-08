"""
Anthropic native API client for Context Quilt's cold path extraction.

Direct to api.anthropic.com so we don't pay the OpenRouter markup on
extraction traffic. Mirrors the `LLMClient.extract()` interface in
`llm_client.py` so the worker can swap clients via env var without any
code change on the call site.

Differences vs the OpenAI-compat path:
- Auth: `x-api-key` header (not `Authorization: Bearer`).
- Request shape: `system` is a top-level field, `messages` carries
  user/assistant turns. No `response_format` parameter; we lean on
  the system prompt to ask for JSON and parse defensively.
- Response shape: `content[0].text` (Anthropic's content blocks
  array) instead of `choices[0].message.content`.
- Token accounting: `usage.input_tokens` / `usage.output_tokens`
  rather than prompt/completion tokens.

Pricing is fetched from a small table here. As of 2026-06-07, direct
Anthropic Haiku 4.5 list is $1.00 / $5.00 per million tokens
(input/output). OpenRouter charges a ~5% markup over list, so
moving extraction here saves that margin per call.
"""

import json
import time
from typing import Any

import httpx
import structlog

from contextquilt.config import get_settings
from contextquilt.secrets import get_secret
from contextquilt.services.llm_client import (
    LLMResponse,
    estimate_cost,
)

logger = structlog.get_logger()


# Default model used when CQ_ANTHROPIC_MODEL isn't set. The native id
# (with the date suffix) lets Anthropic pin to the exact snapshot.
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

# Anthropic API version pin. Update when Anthropic ships a new wire
# revision we want to opt into.
_ANTHROPIC_API_VERSION = "2023-06-01"

# Native Anthropic id → pricing (in USD per million tokens). Mirrors
# the entries used through OpenRouter so the cost estimate matches
# what we'd have paid via OR (minus the markup).
_NATIVE_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-7": (15.00, 75.00),
}


def _estimate_native_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Native-model cost estimate. Falls back to the OpenAI-compat
    `estimate_cost` for unknown models (which has its own default)."""
    pricing = _NATIVE_PRICING.get(model)
    if pricing is None:
        return estimate_cost(model, input_tokens, output_tokens)
    return (input_tokens / 1_000_000) * pricing[0] + (output_tokens / 1_000_000) * pricing[1]


class AnthropicLLMClient:
    """Drop-in replacement for `LLMClient` that talks to Anthropic's
    native API.

    Same `extract()` shape as the OpenAI-compat client so the worker
    or the fallback wrapper can call either without branching on the
    underlying provider.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        max_tokens: int = 4096,
    ):
        # Source the key via the secrets helper so a Secret Manager
        # value (when CQ_GCP_PROJECT is configured and the env var is
        # empty) is picked up automatically. Env var still wins for
        # local dev and operator override.
        self.api_key = api_key or get_secret(
            "anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY"
        )
        self.model = model or get_settings().cq_anthropic_model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.base_url = "https://api.anthropic.com"

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": _ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            timeout=httpx.Timeout(timeout),
        )
        logger.info(
            "llm_client_anthropic_init",
            base_url=self.base_url,
            model=self.model,
        )

    async def extract(
        self,
        system_prompt: str,
        user_content: str,
        model: str | None = None,
        json_schema: dict | None = None,  # noqa: ARG002 — kept for interface parity
    ) -> LLMResponse:
        """Run an extraction call. Returns parsed JSON in the same
        LLMResponse shape as the OpenAI-compat client.

        Anthropic doesn't take a `response_format` parameter, so the
        json_schema kwarg is accepted for interface parity but not
        sent on the wire. The system prompt is expected to ask for
        JSON output (existing extraction prompts already do).
        """
        use_model = model or self.model
        start = time.monotonic()

        body: dict[str, Any] = {
            "model": use_model,
            "max_tokens": self.max_tokens,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
        }

        resp = await self._client.post("/v1/messages", json=body)
        resp.raise_for_status()
        data = resp.json()

        latency_ms = (time.monotonic() - start) * 1000

        usage = data.get("usage", {})
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))

        # Pull text from content blocks. Anthropic returns
        # `content: [{"type": "text", "text": "..."}, ...]`. We take
        # the concatenation of all text blocks, in order.
        content_blocks = data.get("content") or []
        raw_text = "".join(
            block.get("text", "") for block in content_blocks
            if block.get("type") == "text"
        )

        json_valid = True
        try:
            content = json.loads(raw_text)
        except json.JSONDecodeError:
            json_valid = False
            start_idx = raw_text.find("{")
            end_idx = raw_text.rfind("}")
            if start_idx != -1 and end_idx != -1:
                try:
                    content = json.loads(raw_text[start_idx : end_idx + 1])
                except json.JSONDecodeError:
                    content = {"facts": [], "action_items": [], "_parse_error": True}
            else:
                content = {"facts": [], "action_items": [], "_parse_error": True}

        cost = _estimate_native_cost(use_model, input_tokens, output_tokens)

        return LLMResponse(
            content=content,
            model=use_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            json_valid=json_valid,
        )

    async def close(self) -> None:
        await self._client.aclose()
