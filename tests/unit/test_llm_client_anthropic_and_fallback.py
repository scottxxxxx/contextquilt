"""Anthropic native client + fallback wrapper tests.

Pins:
- AnthropicLLMClient parses native response shape correctly
- AnthropicLLMClient handles malformed JSON in content gracefully
- AnthropicLLMClient cost math uses native pricing
- _should_fallback: 401, 403, 429, 5xx, timeout, network → yes
- _should_fallback: 400, 422, ValueError → no
- _translate_to_or_model_id: known models map, unknown pass through
- LLMClientWithFallback: success on primary, no fallback fired
- LLMClientWithFallback: fallback on auth failure, alert dispatched
- LLMClientWithFallback: fallback on 5xx
- LLMClientWithFallback: NOT fallback on 400
- LLMClientWithFallback: alert dispatch failure doesn't break the call
- LLMClientWithFallback with no alert_db is silent
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from contextquilt.services.llm_client import LLMResponse
from contextquilt.services.llm_client_anthropic import (
    AnthropicLLMClient,
    _NATIVE_PRICING,
    _estimate_native_cost,
)
from contextquilt.services.llm_client_fallback import (
    LLMClientWithFallback,
    _OR_MODEL_TRANSLATION,
    _should_fallback,
    _translate_to_or_model_id,
    primary_provider_from_env,
)


# --- AnthropicLLMClient -------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_client_parses_native_response():
    """Anthropic returns `content: [{type:text, text:"..."}]` and a
    usage block with input_tokens/output_tokens. Validate end to end."""
    client = AnthropicLLMClient(api_key="test-key", model="claude-haiku-4-5-20251001")
    fake_response = httpx.Response(
        status_code=200,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json={
            "content": [{"type": "text", "text": '{"facts": [{"id": "f1"}]}'}],
            "usage": {"input_tokens": 100, "output_tokens": 30},
        },
    )
    client._client.post = AsyncMock(return_value=fake_response)

    out = await client.extract("system prompt", "user content")

    assert out.input_tokens == 100
    assert out.output_tokens == 30
    assert out.json_valid is True
    assert out.content == {"facts": [{"id": "f1"}]}
    assert out.model == "claude-haiku-4-5-20251001"
    await client.close()


@pytest.mark.asyncio
async def test_anthropic_client_handles_malformed_json():
    """When the model emits prose around JSON, the tolerant parser
    should still extract the object — same fallback path as the
    OpenAI-compat client."""
    client = AnthropicLLMClient(api_key="test-key")
    fake_response = httpx.Response(
        status_code=200,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json={
            "content": [{
                "type": "text",
                "text": 'Here is the result: {"facts": []} and that\'s it.',
            }],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )
    client._client.post = AsyncMock(return_value=fake_response)

    out = await client.extract("s", "u")
    assert out.json_valid is False  # initial json.loads failed
    assert out.content == {"facts": []}
    await client.close()


@pytest.mark.asyncio
async def test_anthropic_client_handles_no_json_at_all():
    """No `{` or `}` in the output — bottom-out parse_error shape."""
    client = AnthropicLLMClient(api_key="test-key")
    fake_response = httpx.Response(
        status_code=200,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json={
            "content": [{"type": "text", "text": "I refuse to comply."}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        },
    )
    client._client.post = AsyncMock(return_value=fake_response)

    out = await client.extract("s", "u")
    assert out.json_valid is False
    assert out.content.get("_parse_error") is True
    await client.close()


@pytest.mark.asyncio
async def test_anthropic_client_concatenates_multiple_text_blocks():
    """Anthropic may emit multiple content blocks. Concatenate them."""
    client = AnthropicLLMClient(api_key="test-key")
    fake_response = httpx.Response(
        status_code=200,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json={
            "content": [
                {"type": "text", "text": '{"facts":'},
                {"type": "text", "text": ' [{"id": "f1"}]}'},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 8},
        },
    )
    client._client.post = AsyncMock(return_value=fake_response)

    out = await client.extract("s", "u")
    assert out.json_valid is True
    assert out.content == {"facts": [{"id": "f1"}]}
    await client.close()


def test_native_pricing_matches_haiku_listing():
    assert _NATIVE_PRICING["claude-haiku-4-5-20251001"] == (1.00, 5.00)


def test_native_cost_estimate_haiku():
    """1M input + 1M output at $1 + $5 = $6 total."""
    cost = _estimate_native_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    assert cost == pytest.approx(6.0, abs=1e-6)


def test_native_cost_unknown_model_falls_through():
    """Unknown native model uses estimate_cost default (which falls
    back to the LLMClient default pricing)."""
    cost = _estimate_native_cost("claude-unicorn-99", 1000, 1000)
    assert cost >= 0


# --- _should_fallback ---------------------------------------------------


def _http_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "err",
        request=httpx.Request("POST", "https://x"),
        response=httpx.Response(status),
    )


def test_should_fallback_on_5xx():
    assert _should_fallback(_http_error(503)) is True
    assert _should_fallback(_http_error(500)) is True


def test_should_fallback_on_auth():
    assert _should_fallback(_http_error(401)) is True
    assert _should_fallback(_http_error(403)) is True


def test_should_fallback_on_rate_limit():
    assert _should_fallback(_http_error(429)) is True


def test_should_fallback_on_timeout():
    assert _should_fallback(httpx.TimeoutException("slow")) is True


def test_should_fallback_on_network_error():
    assert _should_fallback(httpx.NetworkError("dns")) is True


def test_should_not_fallback_on_bad_request():
    assert _should_fallback(_http_error(400)) is False
    assert _should_fallback(_http_error(422)) is False


def test_should_not_fallback_on_value_error():
    assert _should_fallback(ValueError("oops")) is False


# --- translation --------------------------------------------------------


def test_translation_table_known_models():
    assert _OR_MODEL_TRANSLATION["claude-haiku-4-5-20251001"] == "anthropic/claude-haiku-4.5"
    assert _OR_MODEL_TRANSLATION["claude-sonnet-4-6"] == "anthropic/claude-sonnet-4.6"
    assert _OR_MODEL_TRANSLATION["claude-opus-4-7"] == "anthropic/claude-opus-4.7"


def test_translation_unknown_falls_through():
    assert _translate_to_or_model_id("claude-unicorn-99") == "claude-unicorn-99"


# --- LLMClientWithFallback ---------------------------------------------


def _response(model: str = "claude-haiku-4-5-20251001") -> LLMResponse:
    return LLMResponse(
        content={"ok": True}, model=model,
        input_tokens=10, output_tokens=5,
        latency_ms=100.0, cost_usd=0.0001, json_valid=True,
    )


@pytest.mark.asyncio
async def test_wrapper_returns_primary_on_success():
    primary = MagicMock()
    primary.model = "claude-haiku-4-5-20251001"
    primary.extract = AsyncMock(return_value=_response())
    fallback = MagicMock()
    fallback.extract = AsyncMock()

    w = LLMClientWithFallback(primary, fallback)
    out = await w.extract("s", "u")
    assert out.content == {"ok": True}
    fallback.extract.assert_not_called()


@pytest.mark.asyncio
async def test_wrapper_falls_back_on_auth_failure():
    primary = MagicMock()
    primary.model = "claude-haiku-4-5-20251001"
    primary.extract = AsyncMock(side_effect=_http_error(401))
    fallback = MagicMock()
    fallback.extract = AsyncMock(return_value=_response("anthropic/claude-haiku-4.5"))

    db = MagicMock()
    with patch(
        "contextquilt.services.llm_client_fallback.report_incident",
        new=AsyncMock(),
    ) as alert:
        w = LLMClientWithFallback(primary, fallback, alert_db=db)
        out = await w.extract("s", "u")

    assert out.model == "anthropic/claude-haiku-4.5"
    alert.assert_called_once()
    args, kwargs = alert.call_args
    assert kwargs["category"] == "anthropic_fallback_to_or"
    assert kwargs["details"]["primary_provider"] == "anthropic"
    assert kwargs["details"]["fallback_provider"] == "openrouter"
    # Fallback was invoked with the OR-translated model id.
    fallback.extract.assert_awaited_once()
    assert fallback.extract.await_args.kwargs["model"] == "anthropic/claude-haiku-4.5"


@pytest.mark.asyncio
async def test_wrapper_falls_back_on_5xx():
    primary = MagicMock()
    primary.model = "claude-haiku-4-5-20251001"
    primary.extract = AsyncMock(side_effect=_http_error(503))
    fallback = MagicMock()
    fallback.extract = AsyncMock(return_value=_response())

    w = LLMClientWithFallback(primary, fallback)
    out = await w.extract("s", "u")
    assert out.content == {"ok": True}


@pytest.mark.asyncio
async def test_wrapper_does_not_fall_back_on_400():
    """400 = our request shape is wrong. Fallback won't fix it."""
    primary = MagicMock()
    primary.model = "claude-haiku-4-5-20251001"
    primary.extract = AsyncMock(side_effect=_http_error(400))
    fallback = MagicMock()
    fallback.extract = AsyncMock()

    w = LLMClientWithFallback(primary, fallback)
    with pytest.raises(httpx.HTTPStatusError):
        await w.extract("s", "u")
    fallback.extract.assert_not_called()


@pytest.mark.asyncio
async def test_wrapper_alert_failure_does_not_break_user_call():
    primary = MagicMock()
    primary.model = "claude-haiku-4-5-20251001"
    primary.extract = AsyncMock(side_effect=_http_error(503))
    fallback = MagicMock()
    fallback.extract = AsyncMock(return_value=_response())

    db = MagicMock()
    with patch(
        "contextquilt.services.llm_client_fallback.report_incident",
        new=AsyncMock(side_effect=RuntimeError("alert broken")),
    ):
        w = LLMClientWithFallback(primary, fallback, alert_db=db)
        out = await w.extract("s", "u")
    assert out.content == {"ok": True}


@pytest.mark.asyncio
async def test_wrapper_skips_alert_when_no_db_configured():
    primary = MagicMock()
    primary.model = "claude-haiku-4-5-20251001"
    primary.extract = AsyncMock(side_effect=_http_error(503))
    fallback = MagicMock()
    fallback.extract = AsyncMock(return_value=_response())

    with patch(
        "contextquilt.services.llm_client_fallback.report_incident",
        new=AsyncMock(),
    ) as alert:
        # No alert_db argument.
        w = LLMClientWithFallback(primary, fallback)
        out = await w.extract("s", "u")
    assert out.content == {"ok": True}
    alert.assert_not_called()


# --- primary_provider_from_env -----------------------------------------
#
# primary_provider_from_env reads the lru_cached Settings singleton, not
# os.environ directly. Any earlier test that touches get_settings() (the
# AnthropicLLMClient constructor does, for cq_anthropic_model) freezes the
# singleton with the env as it was THEN — so monkeypatch.setenv alone is
# invisible and the override test failed whenever the whole file ran.
# Clear the cache around each test so Settings rebuilds from the patched
# env, and again afterwards so the frozen-env instance doesn't leak into
# later tests.


@pytest.fixture
def fresh_settings():
    from contextquilt.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_primary_provider_defaults_to_anthropic(monkeypatch, fresh_settings):
    monkeypatch.delenv("CQ_LLM_PRIMARY_PROVIDER", raising=False)
    assert primary_provider_from_env() == "anthropic"


def test_primary_provider_respects_env_override(monkeypatch, fresh_settings):
    monkeypatch.setenv("CQ_LLM_PRIMARY_PROVIDER", "openrouter")
    assert primary_provider_from_env() == "openrouter"


def test_primary_provider_lowercases(monkeypatch, fresh_settings):
    monkeypatch.setenv("CQ_LLM_PRIMARY_PROVIDER", "ANTHROPIC")
    assert primary_provider_from_env() == "anthropic"
