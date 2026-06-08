"""GCP Secret Manager helper for ContextQuilt.

Mirrors GhostPour's `app/secrets.py` pattern. Two entry points:

  1. `get_secret(name, env_var="X")` — resolve a single secret. Env var
     wins (so local dev `.env` and CI overrides work without touching
     GCP). On miss, fetch `projects/{project}/secrets/{name}/versions/latest`
     from Secret Manager. Returns `""` on any failure and logs a warning;
     callers decide whether that's fatal.

  2. `ensure_secrets_in_env()` — walk `_SECRET_MANAGER_MAPPINGS` at
     process startup, fetch any secret whose env var is empty, and write
     it into `os.environ` BEFORE `Settings()` builds. Lets pydantic stay
     blissfully env-only.

Both use `@lru_cache` so repeated calls don't re-hit Secret Manager.
After a Secret Manager rotation in long-running processes, call
`get_secret.cache_clear()` and (if Settings was already built)
`config.reload_settings()`.

Provisioning policy:
    Secrets migrate from `.env.prod` to Secret Manager when a new
    consumer ships, not in a big-bang sweep — matches GP's stated stance.
    The Anthropic native LLM path is the first CQ consumer, so
    `CQ_ANTHROPIC_API_KEY` is the first entry in
    `_SECRET_MANAGER_MAPPINGS` below.

GCP project:
    Set `CQ_GCP_PROJECT` in `.env.prod`. ContextQuilt secrets live in
    the `contextquilt` GCP project (separate from GP's `cloudzap` per
    the app-scoped separation principle). The VM's Compute SA needs
    `roles/secretmanager.secretAccessor` granted PER SECRET in that
    project — no project-level IAM.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Final

import structlog

logger = structlog.get_logger()


# Maps an env-var name → Secret Manager secret id. When `ensure_secrets_in_env()`
# runs, anything in this table that doesn't have a value in `os.environ` gets
# pulled from SM (if `CQ_GCP_PROJECT` is set) and written into `os.environ`
# so the Settings module sees it. Add an entry the same day the first
# consumer for that secret ships.
_SECRET_MANAGER_MAPPINGS: Final[dict[str, str]] = {
    "CQ_ANTHROPIC_API_KEY": "anthropic-api-key",
}


def _project() -> str:
    """The GCP project secrets live in. Returns empty string when unset.

    Read straight from `os.environ` rather than via Settings: this helper
    has to run BEFORE Settings builds (it populates env vars Settings
    will read), so it can't depend on Settings itself.
    """
    return os.getenv("CQ_GCP_PROJECT", "")


@lru_cache(maxsize=64)
def get_secret(name: str, env_var: str | None = None) -> str:
    """Resolve a single secret.

    Resolution order:
      1. If `env_var` is set in the environment with a non-empty value,
         return it. Local dev and CI overrides win.
      2. Otherwise, if `CQ_GCP_PROJECT` is set, fetch
         `projects/{project}/secrets/{name}/versions/latest`.
      3. Otherwise return `""` and log a warning. Callers decide fatality.

    Result is cached, so repeated lookups for the same `(name, env_var)`
    pair don't re-hit Secret Manager. Call `get_secret.cache_clear()` after
    a rotation to force a re-read in long-running processes.
    """
    if env_var:
        value = os.getenv(env_var, "")
        if value:
            return value

    project = _project()
    if not project:
        logger.warning(
            "secret_unavailable",
            name=name,
            reason="no_env_value_and_no_gcp_project",
            env_var=env_var,
        )
        return ""

    try:
        # Lazy import so callers who never hit SM don't pay the import
        # cost. Also keeps `google-cloud-secret-manager` optional for
        # local dev environments that only set env vars.
        from google.cloud import secretmanager  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "secret_unavailable",
            name=name,
            reason="google_cloud_secret_manager_not_installed",
        )
        return ""

    try:
        client = secretmanager.SecretManagerServiceClient()
        resource_name = f"projects/{project}/secrets/{name}/versions/latest"
        response = client.access_secret_version(request={"name": resource_name})
        payload = response.payload.data.decode("utf-8")
        logger.info(
            "secret_loaded_from_sm",
            name=name,
            project=project,
            length=len(payload),
        )
        return payload
    except Exception as exc:
        logger.warning(
            "secret_load_failed",
            name=name,
            project=project,
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        return ""


def ensure_secrets_in_env() -> None:
    """Populate `os.environ` from Secret Manager for any mapped secret
    whose env var is currently empty.

    Call this at process startup BEFORE building `Settings`. Pattern:

        from contextquilt.secrets import ensure_secrets_in_env
        from contextquilt.config import get_settings

        ensure_secrets_in_env()
        settings = get_settings()

    The two halves are deliberately split: this helper does the I/O and
    side effect (writing to `os.environ`); Settings stays pure. That
    makes both testable in isolation.

    If `CQ_GCP_PROJECT` is unset, this is a no-op — local dev and
    operator-owned `.env.prod` deployments still work as before.
    """
    project = _project()
    if not project:
        logger.info("secrets_bootstrap_skipped", reason="no_gcp_project")
        return

    populated = []
    for env_var, sm_name in _SECRET_MANAGER_MAPPINGS.items():
        if os.environ.get(env_var):
            # Env wins. Local dev / operator override.
            continue
        value = get_secret(sm_name, env_var=env_var)
        if value:
            os.environ[env_var] = value
            populated.append(env_var)

    logger.info(
        "secrets_bootstrap_complete",
        project=project,
        populated_count=len(populated),
        populated=populated,
    )


__all__ = ["get_secret", "ensure_secrets_in_env"]
