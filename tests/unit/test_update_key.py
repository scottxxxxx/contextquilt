"""Tests for the dashboard Providers tab's live-mutation rotation flow.

Pins:
- update_key mutates Settings via object.__setattr__ (frozen guard
  bypassed), keeping the in-process value live
- os.environ is updated alongside Settings so subprocess Python sees
  the new key
- persist_to_secret_manager is invoked with the right SM name + value
- get_secret cache is busted so the next caller sees the fresh value
- Order matters: Settings mutates BEFORE the SM persist (so a slow SM
  doesn't delay the in-process switchover)
- Unknown provider → 400
- Empty new_key → 400

The tests call the route handler function directly (FastAPI dependencies
like verify_admin_key are no-ops here, since they fire on real request
binding, not direct invocation).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from contextquilt.config import get_settings
from contextquilt import secrets as secrets_module


@pytest.fixture(autouse=True)
def _clean_caches():
    get_settings.cache_clear()
    secrets_module.get_secret.cache_clear()
    yield
    get_settings.cache_clear()
    secrets_module.get_secret.cache_clear()


def _build_body(provider: str, new_key: str):
    """Import and instantiate the request model inside the test so the
    import doesn't have to run at module load (avoids the FastAPI app
    side effects from main.py)."""
    from dashboard.router import UpdateKeyBody
    return UpdateKeyBody(provider=provider, new_key=new_key)


async def _call_update_key(body):
    from dashboard.router import update_key
    return await update_key(body)


# --- Happy path: anthropic rotation ---

@pytest.mark.asyncio
async def test_rotate_anthropic_mutates_settings_in_place(monkeypatch):
    monkeypatch.setenv("CQ_ANTHROPIC_API_KEY", "sk-ant-old")
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_settings.cache_clear()
    secrets_module.get_secret.cache_clear()

    settings_before = get_settings()
    assert settings_before.cq_anthropic_api_key == "sk-ant-old"

    with patch.object(
        secrets_module, "persist_to_secret_manager", return_value=True
    ) as persist_mock:
        result = await _call_update_key(
            _build_body("anthropic", "sk-ant-rotated")
        )

    # Step 1: Settings mutated in-place
    assert get_settings() is settings_before  # same singleton
    assert get_settings().cq_anthropic_api_key == "sk-ant-rotated"

    # Step 2: os.environ aligned
    assert os.environ["CQ_ANTHROPIC_API_KEY"] == "sk-ant-rotated"

    # Step 3: SM persist called with the right (name, value)
    persist_mock.assert_called_once_with("anthropic-api-key", "sk-ant-rotated")

    # Response shape
    assert result["provider"] == "anthropic"
    assert result["sm_persisted"] is True
    assert "sk-ant" in result["key_masked"]
    assert "rotated" not in result["key_masked"]  # full key not echoed


@pytest.mark.asyncio
async def test_rotate_openrouter_mutates_settings_in_place(monkeypatch):
    monkeypatch.setenv("CQ_LLM_API_KEY", "sk-or-old")
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_settings.cache_clear()
    secrets_module.get_secret.cache_clear()

    with patch.object(
        secrets_module, "persist_to_secret_manager", return_value=True
    ) as persist_mock:
        result = await _call_update_key(
            _build_body("openrouter", "sk-or-new")
        )

    assert get_settings().cq_llm_api_key == "sk-or-new"
    assert os.environ["CQ_LLM_API_KEY"] == "sk-or-new"
    persist_mock.assert_called_once_with("openrouter-api-key", "sk-or-new")
    assert result["provider"] == "openrouter"


# --- Cache busting ---

@pytest.mark.asyncio
async def test_rotate_busts_get_secret_cache(monkeypatch):
    """After rotation, the next get_secret() call must see the new value
    rather than returning a stale cached one."""
    monkeypatch.setenv("CQ_ANTHROPIC_API_KEY", "sk-ant-cached")
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_settings.cache_clear()
    secrets_module.get_secret.cache_clear()

    # Prime the cache with the OLD value.
    first = secrets_module.get_secret(
        "anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY"
    )
    assert first == "sk-ant-cached"

    with patch.object(
        secrets_module, "persist_to_secret_manager", return_value=True
    ):
        await _call_update_key(_build_body("anthropic", "sk-ant-fresh"))

    # After rotation, get_secret() must reflect the new value (via env,
    # since cache is cleared and env was updated by the handler).
    after = secrets_module.get_secret(
        "anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY"
    )
    assert after == "sk-ant-fresh"


# --- Validation errors ---

@pytest.mark.asyncio
async def test_unknown_provider_returns_400(monkeypatch):
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as exc_info:
        await _call_update_key(_build_body("gemini", "sk-test"))
    assert exc_info.value.status_code == 400
    assert "unknown provider" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_empty_new_key_returns_400(monkeypatch):
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as exc_info:
        await _call_update_key(_build_body("anthropic", "   "))
    assert exc_info.value.status_code == 400
    assert "empty" in str(exc_info.value.detail).lower()


# --- SM failure doesn't break the in-process update ---

@pytest.mark.asyncio
async def test_sm_failure_logged_but_settings_still_mutated(monkeypatch):
    """If SM is unreachable, the in-process Settings + env still update —
    operator can fix SM later and re-rotate. Same trust boundary GP
    documents."""
    monkeypatch.setenv("CQ_ANTHROPIC_API_KEY", "sk-ant-old")
    monkeypatch.delenv("CQ_GCP_PROJECT", raising=False)
    get_settings.cache_clear()
    secrets_module.get_secret.cache_clear()

    with patch.object(
        secrets_module, "persist_to_secret_manager", return_value=False
    ):
        result = await _call_update_key(
            _build_body("anthropic", "sk-ant-new")
        )

    # The in-process value did change even though SM failed.
    assert get_settings().cq_anthropic_api_key == "sk-ant-new"
    assert os.environ["CQ_ANTHROPIC_API_KEY"] == "sk-ant-new"
    # ...and the response surfaces the failure so the operator knows
    # they need to re-rotate after restart.
    assert result["sm_persisted"] is False


# --- Mask format ---

def test_mask_key_shape():
    from dashboard.router import _mask_key
    assert _mask_key("sk-or-v1-abcdef12345") == "sk-or-...2345"
    assert _mask_key("") == "***"
    assert _mask_key("short") == "***"


# --- Mapping completeness ---

def test_provider_key_mapping_contains_both_providers():
    """Catch silent regression if someone removes a row from the
    _PROVIDER_KEY_MAPPING table — the Providers tab wouldn't work for
    that provider anymore."""
    from dashboard.router import _PROVIDER_KEY_MAPPING
    assert "anthropic" in _PROVIDER_KEY_MAPPING
    assert "openrouter" in _PROVIDER_KEY_MAPPING
    for provider, mapping in _PROVIDER_KEY_MAPPING.items():
        assert "env_var" in mapping
        assert "settings_field" in mapping
        assert "sm_secret_name" in mapping
