"""Provider health probe tests.

Pins:
- probe_anthropic short-circuits to 'failed' / 'no_api_key_configured'
  when the secrets helper returns ""
- probe_openrouter short-circuits to 'failed' / 'no_api_key_configured'
  when Settings has no CQ_LLM_API_KEY
- probe_anthropic constructs the right wire shape (URL, headers, body)
- probe_openrouter parses balance / limit / is_free_tier from a happy
  response and tolerates missing fields without flipping status
- _classify_http_status maps codes per the documented allowlist
- timeout / network error → 'degraded' (matches _should_fallback)
- 401/403 → 'failed' (matches request-path semantics)
- 429 / 5xx → 'degraded' (provider's up, just unhappy)

No real network. All httpx calls are patched at the AsyncClient level.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from contextquilt.config import get_settings
from contextquilt.secrets import get_secret
from contextquilt.services.provider_health import (
    ProbeResult,
    _classify_http_status,
    probe_anthropic,
    probe_openrouter,
)


# --- Status classifier ---

@pytest.mark.parametrize(
    "code,expected",
    [
        (200, "active"),
        (201, "active"),
        (204, "active"),
        (401, "failed"),
        (403, "failed"),
        (429, "degraded"),
        (500, "degraded"),
        (502, "degraded"),
        (503, "degraded"),
        (599, "degraded"),
        # Surprises (400/404/302) → failed, since they signal something
        # other than "provider unreachable but recoverable".
        (400, "failed"),
        (404, "failed"),
    ],
)
def test_classify_http_status_matches_allowlist(code, expected):
    assert _classify_http_status(code) == expected


# --- Short-circuit when no key ---

def test_probe_anthropic_no_key(monkeypatch):
    monkeypatch.delenv("CQ_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_secret.cache_clear()
    get_settings.cache_clear()

    import asyncio
    result = asyncio.run(probe_anthropic())
    assert result.status == "failed"
    assert result.error == "no_api_key_configured"
    assert result.provider == "anthropic"
    # Should never have attempted to talk to anthropic.com
    assert result.latency_ms is None


def test_probe_openrouter_no_key(monkeypatch):
    monkeypatch.delenv("CQ_LLM_API_KEY", raising=False)
    get_settings.cache_clear()

    import asyncio
    result = asyncio.run(probe_openrouter())
    assert result.status == "failed"
    assert result.error == "no_api_key_configured"
    assert result.provider == "openrouter"
    assert result.balance_usd is None


# --- Anthropic wire shape ---

def _mock_response(status_code: int, text: str = "{}") -> MagicMock:
    """Build a MagicMock that quacks like httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json = MagicMock(return_value=({} if not text else _safe_json(text)))
    return resp


def _safe_json(text: str):
    import json
    try:
        return json.loads(text)
    except Exception:
        return {}


def test_probe_anthropic_constructs_correct_wire_shape(monkeypatch):
    monkeypatch.setenv("CQ_ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CQ_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_secret.cache_clear()
    get_settings.cache_clear()

    captured: dict = {}

    async def fake_post(self, url, json=None, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return _mock_response(200, '{"input_tokens": 5}')

    with patch.object(httpx.AsyncClient, "post", new=fake_post):
        import asyncio
        result = asyncio.run(probe_anthropic())

    assert result.status == "active"
    assert result.latency_ms is not None
    assert captured["url"].endswith("/v1/messages/count_tokens")
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["body"]["model"] == "claude-haiku-4-5-20251001"
    assert captured["body"]["messages"] == [{"role": "user", "content": "ping"}]


def test_probe_anthropic_auth_failure_maps_to_failed(monkeypatch):
    monkeypatch.setenv("CQ_ANTHROPIC_API_KEY", "sk-ant-revoked")
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_secret.cache_clear()
    get_settings.cache_clear()

    async def fake_post(self, url, json=None, headers=None):
        return _mock_response(401, '{"error": "invalid api key"}')

    with patch.object(httpx.AsyncClient, "post", new=fake_post):
        import asyncio
        result = asyncio.run(probe_anthropic())

    assert result.status == "failed"
    assert result.latency_ms is not None
    assert result.error is not None and "http_401" in result.error


def test_probe_anthropic_5xx_is_degraded_not_failed(monkeypatch):
    monkeypatch.setenv("CQ_ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_secret.cache_clear()
    get_settings.cache_clear()

    async def fake_post(self, url, json=None, headers=None):
        return _mock_response(503, "service unavailable")

    with patch.object(httpx.AsyncClient, "post", new=fake_post):
        import asyncio
        result = asyncio.run(probe_anthropic())

    assert result.status == "degraded"
    assert result.error is not None and "http_503" in result.error


def test_probe_anthropic_timeout_is_degraded(monkeypatch):
    monkeypatch.setenv("CQ_ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_secret.cache_clear()
    get_settings.cache_clear()

    async def fake_post(self, url, json=None, headers=None):
        raise httpx.TimeoutException("timed out")

    with patch.object(httpx.AsyncClient, "post", new=fake_post):
        import asyncio
        result = asyncio.run(probe_anthropic())

    assert result.status == "degraded"
    assert result.error == "timeout"


# --- OpenRouter parsing ---

def test_probe_openrouter_parses_full_response(monkeypatch):
    monkeypatch.setenv("CQ_LLM_API_KEY", "sk-or-test")
    monkeypatch.setenv("CQ_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    get_settings.cache_clear()

    async def fake_get(self, url, headers=None):
        return _mock_response(
            200,
            '{"data": {"usage": 4.21, "limit": 50.00, "is_free_tier": false}}',
        )

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        import asyncio
        result = asyncio.run(probe_openrouter())

    assert result.status == "active"
    assert result.balance_usd == 4.21
    assert result.limit_usd == 50.00
    assert result.is_free_tier is False


def test_probe_openrouter_handles_null_limit(monkeypatch):
    """pay-as-you-go accounts have `limit: null` — must not flip status."""
    monkeypatch.setenv("CQ_LLM_API_KEY", "sk-or-test")
    monkeypatch.setenv("CQ_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    get_settings.cache_clear()

    async def fake_get(self, url, headers=None):
        return _mock_response(
            200,
            '{"data": {"usage": 4.21, "limit": null, "is_free_tier": false}}',
        )

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        import asyncio
        result = asyncio.run(probe_openrouter())

    assert result.status == "active"
    assert result.balance_usd == 4.21
    assert result.limit_usd is None  # pay-as-you-go


def test_probe_openrouter_tolerates_missing_data_block(monkeypatch):
    """If OR ever flips the body shape, status must stay 'active' since
    the auth check (200) passed — we just can't extract balance."""
    monkeypatch.setenv("CQ_LLM_API_KEY", "sk-or-test")
    monkeypatch.setenv("CQ_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    get_settings.cache_clear()

    async def fake_get(self, url, headers=None):
        return _mock_response(200, '{"unexpected": "shape"}')

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        import asyncio
        result = asyncio.run(probe_openrouter())

    assert result.status == "active"
    assert result.balance_usd is None
    assert result.limit_usd is None
    assert result.is_free_tier is None


def test_probe_openrouter_auth_failure(monkeypatch):
    monkeypatch.setenv("CQ_LLM_API_KEY", "sk-or-revoked")
    monkeypatch.setenv("CQ_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    get_settings.cache_clear()

    async def fake_get(self, url, headers=None):
        return _mock_response(401, "invalid key")

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        import asyncio
        result = asyncio.run(probe_openrouter())

    assert result.status == "failed"
    assert result.balance_usd is None
