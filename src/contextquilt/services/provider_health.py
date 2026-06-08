"""Provider health probes for the LLM backends ContextQuilt depends on.

Mirrors GhostPour's `app/services/provider_health.py` shape adapted for
CQ's surface: a daemon (`provider_health_loop`, wired in `src/worker.py`)
pings both Anthropic and OpenRouter on a 15-minute cadence and persists
each probe result. The latest per-provider row is what the dashboard
"Providers" tab will read (Gap 3, future) and what answers the
`/api/dashboard/provider-health` endpoint today.

Why probe at all:
    Keys can rot silently — Anthropic auth revoked, OpenRouter credits
    drained, network routes broken. Without a heartbeat we only learn
    when the next real extraction fails, which the operator finds out
    via the existing `anthropic_fallback_to_or` alert. That's the wrong
    way around: by then we've already paid the latency cost of a
    failed primary call. A 15-min ping lets us alert before the next
    real call lands on a dead provider.

Why this exact shape:
    GP delivered specific guidance (see project memory + the team's
    Q3 answer):
      * Anthropic and OpenAI don't expose a sane public balance API,
        so for those we just confirm the key is live via the cheapest
        meaningful call. Anthropic: `count_tokens` on a minimal payload.
        Neither tokens nor model rate limits charge for it.
      * OpenRouter exposes a real `/v1/auth/key` returning the current
        usage / limit / is_free_tier — so we get a true balance there.
    Result shape reflects this asymmetry (`balance_usd` only populated
    for OpenRouter).

Why not a fancier circuit breaker:
    The fallback wrapper in `llm_client_fallback.py` is already
    per-request, narrow-allowlist, and emits alerts on every fallback.
    A circuit breaker on top would duplicate that signal. The health
    daemon's role is precisely "background heartbeat" — orthogonal to
    request-time fallback.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
import structlog

from contextquilt.config import get_settings
from contextquilt.secrets import get_secret

logger = structlog.get_logger()


# Cheapest possible Anthropic call: count tokens on one user turn.
# count_tokens is billed at $0 and doesn't consume model rate budget,
# so we can probe every 15 minutes indefinitely.
_ANTHROPIC_COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
_ANTHROPIC_API_VERSION = "2023-06-01"

# OpenRouter's account-balance endpoint. Returns:
#   { "data": { "usage": <USD>, "limit": <USD or null>, "is_free_tier": bool, ... } }
# limit=null = pay-as-you-go (no hard cap). usage is total spent on this key.
_OPENROUTER_AUTH_KEY_PATH = "/auth/key"

_PROBE_TIMEOUT_S = 10.0


@dataclass
class ProbeResult:
    """One probe outcome. Stored verbatim in `provider_health_probes`."""

    provider: str  # "anthropic" | "openrouter"
    status: str    # "active" | "degraded" | "failed"
    latency_ms: Optional[float] = None
    # OpenRouter only — Anthropic doesn't expose a usable public balance API.
    balance_usd: Optional[float] = None
    limit_usd: Optional[float] = None
    is_free_tier: Optional[bool] = None
    error: Optional[str] = None


def _classify_http_status(code: int) -> str:
    """Map an HTTP status code → ProbeResult.status.

    Mirrors the same narrow allowlist as `llm_client_fallback._should_fallback`
    so probe status tracks request-path semantics. 2xx → active; 4xx
    that genuinely means dead key (401/403) → failed; rate limit (429) and
    server errors (5xx) → degraded (provider's up but unhappy); anything
    else surprising → failed.
    """
    if 200 <= code < 300:
        return "active"
    if code in (401, 403):
        return "failed"
    if code == 429 or 500 <= code < 600:
        return "degraded"
    return "failed"


async def probe_anthropic() -> ProbeResult:
    """Verify the configured Anthropic key + reachability.

    Uses `messages/count_tokens` rather than `messages` so the probe is
    cost-free and doesn't burn model rate budget. A 200 response with
    `input_tokens` in the body confirms the wire path, the auth header,
    and the model id all parse server-side.

    Returns a `ProbeResult` whose status reflects the deepest fact we
    can confirm: full round-trip succeeded vs failed, with the
    classifier above mapping the failure mode.
    """
    api_key = get_secret("anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY")
    if not api_key:
        return ProbeResult(
            provider="anthropic",
            status="failed",
            error="no_api_key_configured",
        )

    settings = get_settings()
    model = settings.cq_anthropic_model
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            resp = await client.post(
                f"https://api.anthropic.com{_ANTHROPIC_COUNT_TOKENS_PATH}",
                json=body,
                headers=headers,
            )
    except httpx.TimeoutException:
        return ProbeResult(
            provider="anthropic",
            status="degraded",
            latency_ms=(time.monotonic() - start) * 1000,
            error="timeout",
        )
    except httpx.HTTPError as exc:
        return ProbeResult(
            provider="anthropic",
            status="degraded",
            latency_ms=(time.monotonic() - start) * 1000,
            error=f"transport:{type(exc).__name__}",
        )

    latency_ms = (time.monotonic() - start) * 1000
    status = _classify_http_status(resp.status_code)
    error: Optional[str] = None
    if status != "active":
        body_preview = resp.text[:200] if resp.text else ""
        error = f"http_{resp.status_code}:{body_preview}"

    return ProbeResult(
        provider="anthropic",
        status=status,
        latency_ms=latency_ms,
        error=error,
    )


async def probe_openrouter() -> ProbeResult:
    """Verify OpenRouter key + fetch real usage/limit/is_free_tier.

    OR is the only provider in our stack that gives a useful balance via
    public API, so we capture it on every probe. Returned shape:
        {
          "data": {
            "usage": 12.34,            # USD spent
            "limit": null | 50.00,     # USD cap (null = pay-as-you-go)
            "is_free_tier": false,
            ...
          }
        }
    """
    settings = get_settings()
    api_key = settings.cq_llm_api_key
    if not api_key:
        return ProbeResult(
            provider="openrouter",
            status="failed",
            error="no_api_key_configured",
        )

    base_url = settings.cq_llm_base_url.rstrip("/")
    url = f"{base_url}{_OPENROUTER_AUTH_KEY_PATH}"

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        return ProbeResult(
            provider="openrouter",
            status="degraded",
            latency_ms=(time.monotonic() - start) * 1000,
            error="timeout",
        )
    except httpx.HTTPError as exc:
        return ProbeResult(
            provider="openrouter",
            status="degraded",
            latency_ms=(time.monotonic() - start) * 1000,
            error=f"transport:{type(exc).__name__}",
        )

    latency_ms = (time.monotonic() - start) * 1000
    status = _classify_http_status(resp.status_code)
    if status != "active":
        body_preview = resp.text[:200] if resp.text else ""
        return ProbeResult(
            provider="openrouter",
            status=status,
            latency_ms=latency_ms,
            error=f"http_{resp.status_code}:{body_preview}",
        )

    # Parse the success body. Be defensive — OR has historically
    # tweaked this shape and we don't want a missing field to flip the
    # status from "active" to "failed".
    balance_usd: Optional[float] = None
    limit_usd: Optional[float] = None
    is_free_tier: Optional[bool] = None
    try:
        data = resp.json().get("data") or {}
        # `usage` is documented as USD; coerce to float defensively.
        if "usage" in data and data["usage"] is not None:
            balance_usd = float(data["usage"])
        if "limit" in data and data["limit"] is not None:
            limit_usd = float(data["limit"])
        if "is_free_tier" in data:
            is_free_tier = bool(data["is_free_tier"])
    except (ValueError, TypeError) as exc:
        logger.warning(
            "openrouter_probe_body_parse_failed",
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        # Still report active — the auth check passed, we just couldn't
        # extract the balance figures.

    return ProbeResult(
        provider="openrouter",
        status=status,
        latency_ms=latency_ms,
        balance_usd=balance_usd,
        limit_usd=limit_usd,
        is_free_tier=is_free_tier,
    )


__all__ = ["ProbeResult", "probe_anthropic", "probe_openrouter"]
