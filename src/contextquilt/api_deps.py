"""FastAPI dependencies shared across CQ's routers.

Separate from `services/admin_auth` on purpose: the DECISION there is pure
and unit-testable without fastapi installed, while the dependency here
needs a real `Header(...)` default at import time. FastAPI reads the
signature when a route is registered, and a parameter whose default is a
plain string is treated as a QUERY PARAMETER rather than a header, which
would silently ignore the `X-Admin-Key` every legitimate admin caller
sends. So the Header default is written normally, in a module that
imports fastapi like any other router module.

THE ADMIN KEY IS GUESSABLE AT LINE RATE WITHOUT THIS (2026-09-16).
Every admin-gated route answers 403 or 200, so every one of them is an
oracle; `GET /api/dashboard/verify-key` is simply the politest one, being
unauthenticated by design so the dashboard login can check a typed key.
GhostPour's edge IP-gates exactly two literal prefixes, `cz/admin` and
`cq/dashboard`, and CQ mounts the dashboard API at `/api/dashboard/`,
which that prefix never matches, on a host CQ does not distinguish from
any other. So the oracle was reachable from the internet with no limit,
against a SINGLE long-lived operator key that gates every admin surface
CQ has. Rate limiting one endpoint would have moved the guessing to
another, so the counter lives HERE, in the check every admin route shares.

TWO BUCKETS, and the reason is that one of them can be weaponised.
A purely global counter lets an attacker lock the operator out of the
dashboard by guessing, which would be introducing a denial of service
while fixing an oracle. So: a per-source counter (cheap to evade by
rotating addresses, but it makes single-source guessing pointless) with a
higher global counter behind it (catches the rotation, at a threshold an
operator's own typos will never reach). The forwarded address is taken as
the LAST entry of X-Forwarded-For, which is the one the proxy observed;
earlier entries are client-supplied and are not trusted. With no
forwarded header at all every caller shares the global bucket, which is
the conservative direction.

FAILS OPEN, like the token-endpoint limiter: Redis unreachable, or no
client bound, means allowed. A closed limiter here locks every admin
surface on a cache hiccup.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from contextquilt.config import get_settings
from contextquilt.services import auth_rate_limit
from contextquilt.services.admin_auth import (
    GLOBAL_BUCKET,
    is_authorized,
    resolve_source,
    resolve_trusted,
    trusted_proxies,
)

logger = logging.getLogger(__name__)

ADMIN_KEY_PREFIX = "admin_fail:"
TRUSTED_CACHE_SECONDS = 60

_redis = None
_trusted_cache: tuple = ("", frozenset(), 0.0)


def _dns(name: str):
    """Addresses for a name, via docker's embedded DNS in prod."""
    return [info[4][0] for info in socket.getaddrinfo(name, None)]


def current_trusted(resolver=_dns) -> frozenset:
    """The trusted-peer set, re-resolved at most once per minute.

    Names are resolved rather than assumed because the proxy's address on
    the docker network is NOT pinned (IPAM nil): it comes from the subnet
    pool at container start and moves if things restart in a different
    order. A stale literal fails SILENTLY, degrading every edge request
    into one shared bucket with nothing saying why, so the resolved set is
    logged whenever it changes and a mismatch is visible rather than
    inferred.
    """
    global _trusted_cache
    raw = os.getenv("CQ_TRUSTED_PROXY_IPS", "") or ""
    cached_raw, cached_set, cached_at = _trusted_cache
    now = time.monotonic()
    if raw == cached_raw and (now - cached_at) < TRUSTED_CACHE_SECONDS and cached_at:
        return cached_set
    resolved = resolve_trusted(trusted_proxies(raw), resolver)
    if resolved != cached_set or raw != cached_raw:
        logger.info(
            "admin_trusted_proxies_resolved configured=%r resolved=%s",
            raw, sorted(resolved) or "NONE (no forwarding header will be believed)")
    _trusted_cache = (raw, resolved, now)
    return resolved


def bind_redis(client) -> None:
    """Hand the app's Redis client to this module at startup.

    A setter rather than a second `from_url` so admin checks share the
    one connection pool the app already has, and rather than importing
    `main` (circular). Unbound means the limiter is inert, which is the
    same posture as Redis being down.
    """
    global _redis
    _redis = client


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


async def verify_admin_key(
    request: Request,
    x_admin_key: str = Header(default=""),
    x_forwarded_for: Optional[str] = Header(default=None),
    x_real_ip: Optional[str] = Header(default=None),
) -> None:
    """403 unless `X-Admin-Key` matches `CQ_ADMIN_KEY`; 429 once a source
    (or everyone together) has failed too often.

    403 rather than 401 because the caller is not being invited to
    authenticate: there is one key, and either you hold it or you do not.
    """
    configured = get_settings().cq_admin_key
    if not configured:
        # Dev mode: no key configured, nothing to guess, nothing to count.
        return

    peer = request.client.host if request.client else None
    source = resolve_source(peer, x_real_ip, x_forwarded_for, current_trusted())
    per_source_limit = _int_env("CQ_ADMIN_MAX_FAILURES", 10)
    per_source_window = _int_env("CQ_ADMIN_FAILURE_WINDOW_SECONDS", 900)
    global_limit = _int_env("CQ_ADMIN_GLOBAL_MAX_FAILURES", 50)
    global_window = _int_env("CQ_ADMIN_GLOBAL_WINDOW_SECONDS", 3600)

    if _redis is not None:
        for ident, limit, window in (
            (source, per_source_limit, per_source_window),
            (GLOBAL_BUCKET, global_limit, global_window),
        ):
            verdict = await auth_rate_limit.check(
                _redis, ident, prefix=ADMIN_KEY_PREFIX, limit=limit, window=window)
            if verdict["refused"]:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many failed admin key attempts. Try again later.",
                    headers={"Retry-After": str(verdict["retry_after"])},
                )

    if not is_authorized(configured, x_admin_key):
        if _redis is not None:
            await auth_rate_limit.record_failure(
                _redis, source, prefix=ADMIN_KEY_PREFIX, window=per_source_window)
            await auth_rate_limit.record_failure(
                _redis, GLOBAL_BUCKET, prefix=ADMIN_KEY_PREFIX, window=global_window)
        raise HTTPException(status_code=403, detail="Invalid admin key")

    if _redis is not None:
        # A correct key forgets this source's failures. The GLOBAL bucket is
        # deliberately NOT cleared: one operator typing the right key must
        # not reset a distributed guessing campaign's budget.
        await auth_rate_limit.clear(_redis, source, prefix=ADMIN_KEY_PREFIX)
