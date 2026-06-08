"""Unit tests for src/contextquilt/config.py.

Covers field defaults, env-var aliases, the composite Redis URL
validator, the frozen-Settings invariant, and the live-mutation trick
the future dashboard Providers tab depends on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contextquilt.config import (
    Settings,
    get_settings,
    reload_settings,
)


@pytest.fixture(autouse=True)
def _clean_settings_singleton():
    """Ensure each test starts from a fresh Settings instance."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _build(monkeypatch, **env_overrides) -> Settings:
    """Build Settings with a clean env populated by the overrides only."""
    # Wipe every CQ_/REDIS_/DATABASE_/JWT_/etc env var so test isolation
    # doesn't pick up the developer's local .env.
    for var in (
        "DATABASE_URL", "REDIS_URL", "REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD",
        "CQ_LLM_API_KEY", "CQ_LLM_BASE_URL", "CQ_LLM_MODEL",
        "CQ_LLM_PRIMARY_PROVIDER", "CQ_LLM_CONTEXT_WINDOW",
        "CQ_ANTHROPIC_API_KEY", "CQ_ANTHROPIC_MODEL",
        "CQ_ADMIN_KEY", "CQ_KEY_ENCRYPTION_KEY", "JWT_SECRET_KEY",
        "CQ_MAX_ENTITIES", "CQ_MAX_PATCHES", "CQ_MAX_RELATIONSHIPS",
        "CQ_QUEUE_BUDGET_THRESHOLD", "CQ_QUEUE_MAX_WAIT_MINUTES",
        "CQ_ALERT_EMAIL_FROM", "RESEND_API_KEY",
        "MCP_API_KEY", "MCP_PORT", "OLLAMA_URL", "CQ_GCP_PROJECT",
    ):
        monkeypatch.delenv(var, raising=False)
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)
    # Disable .env file loading so the developer's local .env doesn't
    # contaminate tests. pydantic-settings normally merges .env into
    # os.environ; passing _env_file=None on the constructor skips it.
    return Settings(_env_file=None)  # type: ignore[call-arg]


# --- Field defaults ---

def test_defaults_match_pre_refactor_values(monkeypatch):
    s = _build(monkeypatch)
    # Exact values that were hard-coded at each os.getenv() call site
    # before the comprehensive refactor. Any divergence is a regression.
    assert s.database_url == "postgresql://postgres:postgres@localhost:5432/context_quilt"
    assert s.cq_llm_base_url == "https://openrouter.ai/api/v1"
    assert s.cq_llm_model == "anthropic/claude-haiku-4.5"
    assert s.cq_llm_primary_provider == "anthropic"
    assert s.cq_anthropic_model == "claude-haiku-4-5-20251001"
    assert s.cq_max_entities == 10
    assert s.cq_max_patches == 12
    assert s.cq_max_relationships == 10
    assert s.cq_queue_budget_threshold == 0.8
    assert s.cq_queue_max_wait_minutes == 60
    assert s.mcp_port == 8001
    assert s.ollama_url == "http://localhost:11434/api/generate"
    assert s.jwt_secret_key == "dev_secret_key_change_in_production"
    assert s.cq_llm_context_window is None


def test_env_vars_drive_settings(monkeypatch):
    s = _build(
        monkeypatch,
        DATABASE_URL="postgresql://prod/db",
        CQ_LLM_MODEL="anthropic/claude-sonnet-4.6",
        CQ_MAX_PATCHES="20",
        CQ_LLM_CONTEXT_WINDOW="500000",
    )
    assert s.database_url == "postgresql://prod/db"
    assert s.cq_llm_model == "anthropic/claude-sonnet-4.6"
    assert s.cq_max_patches == 20
    assert s.cq_llm_context_window == 500000


# --- Redis URL composition ---

def test_redis_url_override_wins(monkeypatch):
    s = _build(
        monkeypatch,
        REDIS_URL="redis://explicit:6379/0",
        REDIS_HOST="ignored",
        REDIS_PASSWORD="ignored",
    )
    assert s.redis_url == "redis://explicit:6379/0"


def test_redis_url_composed_with_password(monkeypatch):
    s = _build(
        monkeypatch,
        REDIS_HOST="cache",
        REDIS_PORT="6380",
        REDIS_PASSWORD="hunter2",
    )
    assert s.redis_url == "redis://:hunter2@cache:6380"


def test_redis_url_composed_without_password(monkeypatch):
    s = _build(monkeypatch, REDIS_HOST="cache", REDIS_PORT="6380")
    assert s.redis_url == "redis://cache:6380"


def test_redis_url_default_when_unset(monkeypatch):
    s = _build(monkeypatch)
    assert s.redis_url == "redis://localhost:6379"


# --- Frozen invariant + live-mutation trick ---

def test_settings_frozen_blocks_direct_setattr(monkeypatch):
    s = _build(monkeypatch)
    with pytest.raises(ValidationError):
        s.cq_admin_key = "pwned"  # type: ignore[misc]


def test_object_setattr_live_mutation_works(monkeypatch):
    """Future Gap 3 (dashboard Providers tab) depends on this exact escape
    hatch. If pydantic's frozen guard ever changes shape this test fails."""
    s = _build(monkeypatch, CQ_ANTHROPIC_API_KEY="sk-ant-old")
    assert s.cq_anthropic_api_key == "sk-ant-old"
    object.__setattr__(s, "cq_anthropic_api_key", "sk-ant-rotated")
    assert s.cq_anthropic_api_key == "sk-ant-rotated"


# --- Singleton + reload ---

def test_get_settings_returns_singleton(monkeypatch):
    monkeypatch.setenv("CQ_LLM_MODEL", "test-model")
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_reload_settings_picks_up_env_change(monkeypatch):
    monkeypatch.setenv("CQ_LLM_MODEL", "model-a")
    get_settings.cache_clear()
    s1 = get_settings()
    assert s1.cq_llm_model == "model-a"

    monkeypatch.setenv("CQ_LLM_MODEL", "model-b")
    s2 = reload_settings()
    assert s2.cq_llm_model == "model-b"
    assert s1 is not s2
