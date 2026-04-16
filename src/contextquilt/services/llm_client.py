"""
Provider-agnostic LLM client for Context Quilt's cold path extraction.

Supports any OpenAI-compatible API (OpenAI, Gemini, DeepSeek, Mistral, etc.)
via configurable base_url. Uses JSON mode for structured extraction output.

Configuration via environment variables:
    CQ_LLM_API_KEY    - API key for the provider
    CQ_LLM_BASE_URL   - OpenAI-compatible endpoint (default: OpenAI)
    CQ_LLM_MODEL      - Model name (default: gpt-4.1-nano)
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# Pricing per million tokens: (input, output)
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-4o-mini": (0.15, 0.60),
    # Gemini (via OpenAI-compatible endpoint or OpenRouter)
    "google/gemini-2.5-flash": (0.30, 2.50),
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "google/gemini-2.0-flash-001": (0.10, 0.40),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    # Qwen (via OpenRouter)
    "qwen/qwen3-4b:free": (0.0, 0.0),
    "qwen/qwen-turbo": (0.033, 0.13),
    "qwen/qwen3.5-flash-02-23": (0.065, 0.26),
    "qwen/qwen3-14b": (0.06, 0.24),
    "qwen/qwen3-32b": (0.08, 0.24),
    # DeepSeek (via OpenRouter)
    "deepseek/deepseek-chat-v3-0324": (0.20, 0.77),
    # Mistral (via OpenRouter)
    "mistralai/mistral-small-3.1-24b-instruct:free": (0.0, 0.0),
    "mistralai/mistral-small-3.1-24b-instruct": (0.03, 0.11),
    # Cohere (via OpenRouter)
    "cohere/command-r7b-12-2024": (0.037, 0.15),
    # Direct
    "qwen-turbo": (0.05, 0.20),
    "deepseek-chat": (0.28, 0.42),
    "mistral-small-latest": (0.15, 0.60),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD based on token counts."""
    pricing = MODEL_PRICING.get(model, (0.15, 0.60))
    return (input_tokens / 1_000_000) * pricing[0] + (output_tokens / 1_000_000) * pricing[1]


@dataclass
class LLMResponse:
    """Response from an LLM extraction call."""
    content: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    json_valid: bool = True


class LLMClient:
    """
    OpenAI-compatible LLM client for structured extraction.

    Works with any provider implementing the chat completions API:
    - OpenAI (GPT-4.1-nano, GPT-4o-mini)
    - Google Gemini (via generativelanguage.googleapis.com/v1beta/openai/)
    - DeepSeek, Mistral, etc.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ):
        self.api_key = api_key or os.getenv("CQ_LLM_API_KEY", "")
        self.base_url = (
            base_url or os.getenv("CQ_LLM_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.model = model or os.getenv("CQ_LLM_MODEL", "gpt-4.1-nano")
        self.timeout = timeout

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout),
        )
        logger.info("llm_client_init", base_url=self.base_url, model=self.model)

    async def extract(
        self,
        system_prompt: str,
        user_content: str,
        model: str | None = None,
        json_schema: dict | None = None,
    ) -> LLMResponse:
        """
        Run a structured extraction call. Returns parsed JSON.

        Response-format fallback chain (strictest → loosest):
          1. json_schema  (constrained decoding, if json_schema passed)
          2. json_object  (loose JSON mode)
          3. no response_format (pure prompt-described output)

        Each tier is tried on HTTP 400 responses that mention "response_format",
        so a provider that doesn't support json_schema will automatically
        degrade to json_object without aborting the call.
        """
        use_model = model or self.model
        start = time.monotonic()

        base_body: dict[str, Any] = {
            "model": use_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
        }

        # Build fallback chain based on whether a schema was provided.
        if json_schema is not None:
            attempts = [
                ("json_schema", {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "cq_extraction",
                        "schema": json_schema,
                        "strict": True,
                    },
                }),
                ("json_object", {"type": "json_object"}),
                ("none", None),
            ]
        else:
            attempts = [
                ("json_object", {"type": "json_object"}),
                ("none", None),
            ]

        data = None
        last_error: Exception | None = None
        for mode, response_format in attempts:
            body = dict(base_body)
            if response_format is not None:
                body["response_format"] = response_format
            try:
                resp = await self._client.post("/chat/completions", json=body)
                resp.raise_for_status()
                candidate = resp.json()
                # Some providers (e.g. Mistral via OpenRouter) return 200 with
                # null content when they can't satisfy a strict json_schema.
                # Treat null content as a fallback signal, not a success.
                message_content = (
                    candidate.get("choices", [{}])[0].get("message", {}).get("content")
                )
                if message_content is None and mode != attempts[-1][0]:
                    logger.warning("response_format_null_content", model=use_model, mode=mode)
                    continue
                data = candidate
                if mode != attempts[0][0]:
                    logger.warning("response_format_downgraded", model=use_model, mode=mode)
                break
            except httpx.HTTPStatusError as e:
                last_error = e
                # Any 400 on a non-terminal attempt triggers fallback. Providers
                # vary in whether they mention "response_format" in error text,
                # and Anthropic via OpenRouter returns generic 400s.
                if e.response.status_code == 400 and mode != attempts[-1][0]:
                    logger.warning(
                        "response_format_unsupported",
                        model=use_model,
                        mode=mode,
                        status=e.response.status_code,
                    )
                    continue
                raise
        if data is None:
            # All attempts failed or returned null content.
            if last_error is not None:
                raise last_error
            raise RuntimeError(f"LLM returned null content for all response_format modes (model={use_model})")

        latency_ms = (time.monotonic() - start) * 1000

        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        # Parse content
        raw_text = data["choices"][0]["message"]["content"]
        json_valid = True
        try:
            content = json.loads(raw_text)
        except json.JSONDecodeError:
            json_valid = False
            # Try to find JSON object in the response
            start_idx = raw_text.find("{")
            end_idx = raw_text.rfind("}")
            if start_idx != -1 and end_idx != -1:
                try:
                    content = json.loads(raw_text[start_idx : end_idx + 1])
                except json.JSONDecodeError:
                    content = {"facts": [], "action_items": [], "_parse_error": True}
            else:
                content = {"facts": [], "action_items": [], "_parse_error": True}

        cost = estimate_cost(use_model, input_tokens, output_tokens)

        logger.info(
            "extraction_complete",
            model=use_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=round(latency_ms, 1),
            cost_usd=round(cost, 6),
            facts_count=len(content.get("facts", [])),
            json_valid=json_valid,
        )

        return LLMResponse(
            content=content,
            model=use_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            json_valid=json_valid,
        )

    async def close(self):
        await self._client.aclose()
