"""
Two-client LLM wrapper: primary first, fallback on failure, alert.

Pattern is intentionally narrow. We try the primary client (typically
Anthropic native), and on failure conditions where the fallback could
plausibly succeed (auth, rate limit, 5xx, network, timeout) we retry
the same logical extraction through the fallback client (typically
OpenRouter). Every fallback fires an operator alert under category
`anthropic_fallback_to_or` so the underlying primary-side issue gets
investigated before our managed key path silently rots.

Failure modes that DON'T trigger fallback:
- 400/422 (request shape problem; fallback won't fix it)
- json parse errors that bubble up to the call site
- Anything else genuinely deterministic

The wrapper exposes the same `extract()` interface as both underlying
clients so the worker can drop it in transparently.
"""

import os
from typing import Any

import httpx
import structlog

from contextquilt.services.alerting import report_incident
from contextquilt.services.llm_client import LLMResponse

logger = structlog.get_logger()


# Heuristic mapping: native Anthropic model → OR-equivalent model id.
# Used to translate the primary's native model name onto OR's slash
# form when we fall back. Add entries as new Anthropic models go into
# production extraction.
_OR_MODEL_TRANSLATION: dict[str, str] = {
    "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-opus-4-7": "anthropic/claude-opus-4.7",
}


def _translate_to_or_model_id(native_model: str) -> str:
    """Return the OR id for a given native Anthropic model. Falls back
    to the native id verbatim when the mapping is missing — OR's
    error response will surface the configuration drift to operators."""
    return _OR_MODEL_TRANSLATION.get(native_model, native_model)


def _should_fallback(exc: Exception) -> bool:
    """Narrow allowlist of failure types where the fallback might
    actually succeed. Keep tight so we don't paper over real bugs."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code in (401, 403, 429) or 500 <= code < 600
    return False


class LLMClientWithFallback:
    """Try `primary.extract()`; on a recoverable failure, retry via
    `fallback.extract()` and fire an operator alert.

    Implementation note: we don't make the alert dispatch a blocking
    failure mode. If alerting is down, the user-facing call still
    completes via the fallback; we just lose the email.
    """

    def __init__(self, primary, fallback, *, alert_db=None):
        """
        Args:
            primary: a client exposing `async extract(...) -> LLMResponse`.
            fallback: same shape, used when primary fails.
            alert_db: optional DB connection or pool object accepted by
                `report_incident`. When None, we skip alerting (testable
                in isolation without spinning up Postgres).
        """
        self.primary = primary
        self.fallback = fallback
        self.alert_db = alert_db

    # Convenience surface so worker code doesn't have to know which
    # client is "real". Most call sites use these.
    @property
    def model(self) -> str:
        return getattr(self.primary, "model", "") or getattr(self.fallback, "model", "")

    @property
    def base_url(self) -> str:
        return getattr(self.primary, "base_url", "") or getattr(self.fallback, "base_url", "")

    async def extract(
        self,
        system_prompt: str,
        user_content: str,
        model: str | None = None,
        json_schema: dict | None = None,
    ) -> LLMResponse:
        primary_model = model or self.primary.model
        try:
            return await self.primary.extract(
                system_prompt=system_prompt,
                user_content=user_content,
                model=primary_model,
                json_schema=json_schema,
            )
        except Exception as exc:
            if not _should_fallback(exc):
                raise

            or_model = _translate_to_or_model_id(primary_model)
            logger.warning(
                "anthropic_extract_failed_falling_back",
                primary_model=primary_model,
                or_model=or_model,
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
            )

            await self._alert(primary_model, or_model, exc)

            return await self.fallback.extract(
                system_prompt=system_prompt,
                user_content=user_content,
                model=or_model,
                json_schema=json_schema,
            )

    async def _alert(self, primary_model: str, or_model: str, exc: Exception) -> None:
        if self.alert_db is None:
            return
        try:
            await report_incident(
                self.alert_db,
                category="anthropic_fallback_to_or",
                subject=f"anthropic_extract_failed_{type(exc).__name__}",
                details={
                    "primary_provider": "anthropic",
                    "primary_model": primary_model,
                    "fallback_provider": "openrouter",
                    "fallback_model": or_model,
                    "failure_type": type(exc).__name__,
                    "failure_message": str(exc)[:500],
                },
            )
        except Exception as alert_exc:
            # Alerting must not break the user-facing fallback.
            logger.warning(
                "anthropic_fallback_alert_dispatch_failed",
                error_type=type(alert_exc).__name__,
                error_message=str(alert_exc)[:200],
            )

    async def close(self) -> None:
        for client in (self.primary, self.fallback):
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass


def primary_provider_from_env() -> str:
    """Return the configured primary provider id ('anthropic' or
    'openrouter'). Default is 'anthropic' once this lands; operators
    can flip back to 'openrouter' via the env var without code change."""
    return os.getenv("CQ_LLM_PRIMARY_PROVIDER", "anthropic").lower()
