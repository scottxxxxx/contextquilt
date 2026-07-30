"""Pydantic Settings module for ContextQuilt.

Single source of truth for environment-driven configuration. Mirrors the
GhostPour `app/config.py:Settings` pattern: frozen pydantic model, env-var
sourced, accessed via a lazy singleton.

Why frozen:
    The dashboard "Providers" tab (future Gap 3) edits secrets at runtime
    and live-mutates the Settings via `object.__setattr__`. That trick
    only works on a frozen model — pydantic's standard setattr is disabled
    so the update doesn't trigger re-validation on every mutation. Until
    Gap 3 lands, frozen is just hygiene; once Gap 3 ships, it's load-bearing.

Why a singleton, not FastAPI app.state:
    Three entry points use the same Settings:
      - FastAPI app (`src/main.py`)
      - Cold-path worker (`src/worker.py`) — async loop, no app.state
      - MCP server (`src/mcp_server.py`) — separate process, no app.state
    A module-level lazy accessor (`get_settings()`) works uniformly across
    all three. FastAPI startup can stash the same instance on `app.state`
    for the request-time access pattern GP uses, but the source of truth
    lives here.

Secrets resolution:
    Settings reads strictly from environment variables. The
    `_ensure_secrets_in_env()` helper in `secrets.py` is what pulls
    GCP Secret Manager values INTO `os.environ` before this module
    builds, so env vars stay the only thing pydantic sees. Keeps the
    two concerns testable in isolation.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env-driven configuration for ContextQuilt.

    Field defaults match the values previously hard-coded at each
    `os.getenv()` call site. Any divergence is a regression.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        frozen=True,
    )

    # --- Database / cache ---
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/context_quilt",
        alias="DATABASE_URL",
    )
    # REDIS_URL wins if set; otherwise compose from host/port/password.
    # `redis_url_override` holds the raw env var; the `redis_url` computed
    # property does the composition and is the value callers actually use.
    redis_url_override: str = Field(default="", alias="REDIS_URL")
    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_password: str = Field(default="", alias="REDIS_PASSWORD")

    # --- LLM: OpenAI-compatible (OpenRouter fallback path) ---
    cq_llm_api_key: str = Field(default="", alias="CQ_LLM_API_KEY")
    cq_llm_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="CQ_LLM_BASE_URL"
    )
    cq_llm_model: str = Field(
        default="anthropic/claude-haiku-4.5", alias="CQ_LLM_MODEL"
    )
    cq_llm_primary_provider: str = Field(
        default="anthropic", alias="CQ_LLM_PRIMARY_PROVIDER"
    )
    # Optional override; None means "look up from the model registry at
    # call time" — see worker.py around the context_window derivation.
    cq_llm_context_window: int | None = Field(
        default=None, alias="CQ_LLM_CONTEXT_WINDOW"
    )

    # --- LLM: Anthropic native (primary path) ---
    cq_anthropic_api_key: str = Field(default="", alias="CQ_ANTHROPIC_API_KEY")
    cq_anthropic_model: str = Field(
        default="claude-haiku-4-5-20251001", alias="CQ_ANTHROPIC_MODEL"
    )

    # --- Auth / admin ---
    cq_admin_key: str = Field(default="", alias="CQ_ADMIN_KEY")
    cq_key_encryption_key: str = Field(default="", alias="CQ_KEY_ENCRYPTION_KEY")
    jwt_secret_key: str = Field(
        default="dev_secret_key_change_in_production", alias="JWT_SECRET_KEY"
    )

    # --- Extraction budgets ---
    # cq_max_patches is the FLOOR of the length-scaled patch backstop
    # (extraction_patch_backstop) — a minimum guarantee, not a target.
    # Sized 36 by the 2026-07-30 density probe + fixtures: a 1.7K-char
    # ultra-dense standup legitimately emitted 32 memories. Dedup
    # downstream is the precision stage; the backstop only bounds
    # degenerate output.
    cq_max_entities: int = Field(default=15, alias="CQ_MAX_ENTITIES")
    cq_max_patches: int = Field(default=36, alias="CQ_MAX_PATCHES")
    cq_max_relationships: int = Field(default=15, alias="CQ_MAX_RELATIONSHIPS")
    cq_queue_budget_threshold: float = Field(
        default=0.8, alias="CQ_QUEUE_BUDGET_THRESHOLD"
    )
    cq_queue_max_wait_minutes: int = Field(
        default=60, alias="CQ_QUEUE_MAX_WAIT_MINUTES"
    )

    # Semantic dedup: LLM judge for trigram gray-zone patch pairs at
    # write time. Kill switch — off reverts to trigram-only dedup.
    cq_semantic_dedup_enabled: bool = Field(
        default=True, alias="CQ_SEMANTIC_DEDUP_ENABLED"
    )

    # Consolidation ("sleep" pass): periodic synthesis of higher-order
    # patches from cue-clustered sources. Kill switch — inert anyway
    # unless a registered manifest declares consolidation_rules.
    cq_consolidation_enabled: bool = Field(
        default=True, alias="CQ_CONSOLIDATION_ENABLED"
    )

    # --- Alerting / email ---
    cq_alert_email_from: str = Field(default="", alias="CQ_ALERT_EMAIL_FROM")
    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")

    # --- MCP server ---
    mcp_api_key: str = Field(default="", alias="MCP_API_KEY")
    mcp_port: int = Field(default=8001, alias="MCP_PORT")

    # --- Misc external ---
    ollama_url: str = Field(
        default="http://localhost:11434/api/generate", alias="OLLAMA_URL"
    )

    # --- GCP / Secret Manager ---
    # Empty string means "no SM available" — get_secret() falls back to
    # the env-only path. Set to the contextquilt GCP project id in prod.
    cq_gcp_project: str = Field(default="", alias="CQ_GCP_PROJECT")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        """Compose the Redis URL the worker and dashboard actually use.

        REDIS_URL wins if set. Otherwise:
          - With a password: `redis://:<pw>@<host>:<port>`
          - Without:         `redis://<host>:<port>`

        Mirrors `src/worker.py:66` / `src/dashboard/router.py:1119`.
        """
        if self.redis_url_override:
            return self.redis_url_override
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}"
        return f"redis://{self.redis_host}:{self.redis_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton.

    `@lru_cache(maxsize=1)` is the singleton mechanism — first call builds
    from env, subsequent calls return the cached instance. Call
    `secrets._ensure_secrets_in_env()` BEFORE the first `get_settings()`
    in any process that should pull values from GCP Secret Manager. That
    helper writes SM values into `os.environ` so this module sees them.

    To re-source after a Secret Manager rotation or test reset, call
    `get_settings.cache_clear()`.
    """
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache and rebuild. Exposed for tests + future rotation flows."""
    get_settings.cache_clear()
    return get_settings()


# Convenience for code that pre-existed bare `os.environ` reads. Lets
# callers ask for "the current settings, mid-run" without importing the
# accessor at every site. Mirrors GP's `request.app.state.settings`
# pattern conceptually, just without needing FastAPI in scope.
def current_settings() -> Settings:
    return get_settings()


__all__ = ["Settings", "get_settings", "reload_settings", "current_settings"]
