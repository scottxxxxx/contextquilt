"""Minimal GCP Secret Manager client.

Reads a secret from any GCP project the running service account has
`roles/secretmanager.secretAccessor` on. Designed for cross-project
secret sharing (e.g. the Resend API key lives in the cloudzap project
and CQ's VM SA has been granted access to that one specific secret).

No `google-cloud-secret-manager` dependency: we use the metadata
server for the access token and the Secret Manager REST API directly
via httpx (already a transitive dep). Keeps the install footprint
small and matches the existing pattern of talking to Google APIs via
plain HTTP (see the backup sidecar's gcloud-based path).

In-memory cache with a short TTL so a flood of alerts doesn't hit SM
on every call. Cache TTL is short enough that key rotation propagates
within minutes without a restart.

Fallback semantics: if the metadata server isn't reachable (running
locally for tests, no GCP metadata available) we fall back to an env
var by the same name as the secret. That keeps unit tests and local
dev working without requiring SM access.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)
SECRET_MANAGER_BASE = "https://secretmanager.googleapis.com/v1"
DEFAULT_CACHE_TTL_S = 300  # 5 min


@dataclass
class _CachedSecret:
    value: str
    fetched_at: float


_cache: dict[str, _CachedSecret] = {}


async def _fetch_metadata_token(timeout_s: float = 2.0) -> Optional[str]:
    """Ask the GCE metadata server for an OAuth token. Returns None
    if not running on GCE (metadata server unreachable)."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(
                METADATA_TOKEN_URL,
                headers={"Metadata-Flavor": "Google"},
            )
        if resp.status_code != 200:
            return None
        return resp.json().get("access_token")
    except httpx.HTTPError:
        # Local dev / non-GCE environment — metadata server isn't there.
        return None


async def _fetch_secret_from_sm(
    project: str, secret_name: str, version: str, token: str,
    timeout_s: float = 5.0,
) -> Optional[str]:
    """Read one secret version from Secret Manager via REST."""
    url = (
        f"{SECRET_MANAGER_BASE}/projects/{project}"
        f"/secrets/{secret_name}/versions/{version}:access"
    )
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            logger.warning(
                "gcp_secret.sm_rejected project=%s secret=%s status=%d body=%s",
                project, secret_name, resp.status_code, resp.text[:200],
            )
            return None
        # payload.data is base64 (URL-safe by default in REST responses).
        import base64
        b64 = resp.json().get("payload", {}).get("data", "")
        if not b64:
            return None
        return base64.b64decode(b64).decode("utf-8").strip()
    except httpx.HTTPError as exc:
        logger.warning("gcp_secret.transport_failed reason=%s", exc)
        return None


async def get_secret(
    *,
    project: str,
    secret_name: str,
    version: str = "latest",
    env_fallback: str | None = None,
    cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
) -> Optional[str]:
    """Read a secret value. Tries Secret Manager first; falls back to
    `os.getenv(env_fallback)` if metadata server is unreachable or
    Secret Manager refuses access.

    Cached in-process by (project, secret_name, version) tuple for
    `cache_ttl_s` seconds. Short enough that rotation propagates
    quickly, long enough to avoid SM round-trips on every call."""
    cache_key = f"{project}/{secret_name}/{version}"
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached.fetched_at) < cache_ttl_s:
        return cached.value

    token = await _fetch_metadata_token()
    value: Optional[str] = None
    if token:
        value = await _fetch_secret_from_sm(project, secret_name, version, token)

    if value is None and env_fallback:
        value = os.getenv(env_fallback)

    if value:
        _cache[cache_key] = _CachedSecret(value=value, fetched_at=time.time())
    return value


def clear_cache() -> None:
    """For tests / forced reload."""
    _cache.clear()
