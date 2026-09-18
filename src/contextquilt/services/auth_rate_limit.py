"""Refuse a credential that keeps failing, BEFORE the password hash runs.

WHY THIS EXISTS. `POST /v1/auth/token` verifies with pbkdf2_sha256 and
had no limit of any kind, so a WRONG credential cost CQ one password hash
per attempt, and a caller that retries on failure turns its own ordinary
request rate into a hashing load on us. GhostPour found this from the
other side and built a cooldown in their client: they remember a rejected
credential and stop asking, deliberately, because "a wrong secret is
permanent". That cooldown is a caller compensating for a limit the server
never had, and it only protects us for as long as every caller has built
one. The server has to hold this itself (2026-09-16, Scott's call).

WHAT IT COUNTS: failures only. A successful mint clears the counter, and
a legitimate caller is never throttled for minting tokens (GP caches a
bearer per app until expiry, so the honest rate is a handful an hour).
So the limit bites exactly the shape that costs us: the same client_id
failing over and over.

WHERE IT RUNS: before the applications lookup and before
`verify_password`. A refusal that still pays the hash would defeat the
point, and the ORDER is the whole feature, which is why a test asserts
it in main.py's source as well as here.

FAIL OPEN. Redis being unreachable must never stop a correct credential
minting a token: the failure mode of a closed limiter is a total auth
outage, which is far worse than the load this prevents. Every Redis call
here is wrapped and degrades to "allowed".

THE COUNTER IS SHARED. Prod runs four uvicorn workers, so an in-process
counter would be four counters each with the full budget. The window is
fixed, not sliding: the first failure sets the expiry, and the key
vanishes when it lapses. Bounded memory by construction, including
against a caller inventing client_ids, since every key carries a TTL.

Kill switch `CQ_AUTH_RATELIMIT_ENABLED=0`. Knobs
`CQ_AUTH_MAX_FAILURES` (default 10) and
`CQ_AUTH_FAILURE_WINDOW_SECONDS` (default 900).

A CALLER DEPENDS ON THESE NUMBERS. GhostPour's client (their #1002)
backs off on this limiter's 429 and honours `Retry-After`, and it was
built against the contract as written here, NOT measured: a burst test
against prod was planned and cancelled by Scott on 2026-09-18 ("we trust
the contract"). What GP assumes: 10 failures per 900 seconds, keyed on
client_id, a 429 on refusal, and `Retry-After` present (we send
delta-seconds; they parse both forms). So before changing the threshold,
the window, the key, the status code, or dropping the header, INCLUDING
by flipping one of the env knobs above on prod, tell GP first. They
cannot notice by observation, because a wrong credential is the only
thing that reaches this path and theirs are all correct.

Pure where it can be (the key, the decision), so both are testable with
no Redis and no fastapi.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Optional

KEY_PREFIX = "auth_fail:"
DEFAULT_MAX_FAILURES = 10
DEFAULT_WINDOW_SECONDS = 900


def _int_env(name: str, default: int) -> int:
    """Read at CALL time, never at import: a module-level constant would
    pin the value to whenever this module happened to be imported, which
    is the difference between a knob and a decoration."""
    try:
        value = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def enabled() -> bool:
    return (os.getenv("CQ_AUTH_RATELIMIT_ENABLED", "1") or "1").strip().lower() \
        not in ("0", "false", "no")


def max_failures() -> int:
    return _int_env("CQ_AUTH_MAX_FAILURES", DEFAULT_MAX_FAILURES)


def window_seconds() -> int:
    return _int_env("CQ_AUTH_FAILURE_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)


def key_for(client_id: Any, prefix: str = KEY_PREFIX) -> str:
    """One Redis key per client_id, hashed, inside a namespace.

    Hashed rather than interpolated because client_id is attacker-supplied
    and lands in a key name: a raw value could carry a newline, a colon,
    or 8KB of junk. The log line keeps the readable (truncated) id, so
    nothing is lost for debugging.

    `prefix` namespaces the counter. App credentials and admin-key
    attempts must not share a bucket: they have different populations,
    different thresholds, and one locking out the other would be a
    surprise nobody would find quickly.
    """
    raw = "" if client_id is None else str(client_id)
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def is_refused(failures: int, limit: int) -> bool:
    """At the limit, not past it: the Nth failure is the one that closes
    the door, so `limit` is the number of attempts allowed in a window."""
    return failures >= limit


async def check(redis_client, client_id: Any, *, prefix: str = KEY_PREFIX,
                limit: Optional[int] = None,
                window: Optional[int] = None) -> Dict[str, Any]:
    """{"refused": bool, "failures": int, "retry_after": int}. Never raises."""
    if not enabled():
        return {"refused": False, "failures": 0, "retry_after": 0}
    limit = max_failures() if limit is None else limit
    try:
        raw = await redis_client.get(key_for(client_id, prefix))
        failures = int(raw) if raw is not None else 0
    except Exception:
        return {"refused": False, "failures": 0, "retry_after": 0}
    if not is_refused(failures, limit):
        return {"refused": False, "failures": failures, "retry_after": 0}
    retry_after = window_seconds() if window is None else window
    try:
        ttl = await redis_client.ttl(key_for(client_id, prefix))
        if isinstance(ttl, int) and ttl > 0:
            retry_after = ttl
    except Exception:
        pass
    return {"refused": True, "failures": failures, "retry_after": retry_after}


async def record_failure(redis_client, client_id: Any, *, prefix: str = KEY_PREFIX,
                         window: Optional[int] = None) -> int:
    """Count one failed attempt. Returns the running count, 0 if not counted."""
    if not enabled():
        return 0
    key = key_for(client_id, prefix)
    try:
        count = await redis_client.incr(key)
        # Expiry on the FIRST failure only, so the window runs from the
        # first failure rather than being pushed forward by each new one.
        # Refreshing it every time would let a steady trickle hold the
        # key open forever and lock the caller out permanently.
        if count == 1:
            await redis_client.expire(
                key, window_seconds() if window is None else window)
        return int(count)
    except Exception:
        return 0


async def clear(redis_client, client_id: Any, *, prefix: str = KEY_PREFIX) -> None:
    """A correct credential forgets the failures. Never raises."""
    try:
        await redis_client.delete(key_for(client_id, prefix))
    except Exception:
        pass
