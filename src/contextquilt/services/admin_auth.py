"""The admin-key DECISION, defined once, with no web framework in sight.

WHY THIS EXISTS (2026-09-16). CQ had its admin gate written twice, in
`dashboard/router.py` and `routers/app_schemas.py`, identical both times,
and applied to every route in both. `main.py` imported neither, so three
routes that administer the application registry were reachable with no
credential at all:

    POST  /v1/auth/register        mints an app and returns its client_secret
    GET   /v1/auth/apps            lists every app id, name and enforce_auth
    PATCH /v1/auth/apps/{app_id}   sets enforce_auth on ANY app, rotates its
                                   LLM key fields

`enforce_auth=False` is what makes CQ accept a bare `X-App-ID` header, so
an unauthenticated PATCH could switch ShoulderSurf or ghostpour into
header-only auth and then speak as them. The guard was never missing; it
was never APPLIED, which is the shape of a capability whose carrier is
"every route remembers to add it" (doc 19.2).

The decision lives here, pure, so it is testable in the local unit venv
(no fastapi, no asyncpg). The FastAPI dependency that turns a False into a
403 lives in `contextquilt/api_deps.py`, because a `Header(...)` default
has to be a real fastapi object at import time: an earlier version of this
module attached it after definition so the file would import without
fastapi, and the failure mode of that trick is silent and bad. If FastAPI
does not see a Header default it reads the value as a QUERY PARAMETER,
which would ignore the header every legitimate admin caller sends.
"""
from __future__ import annotations


GLOBAL_BUCKET = "__all__"


def trusted_proxies(raw: str | None) -> frozenset:
    """Parse `CQ_TRUSTED_PROXY_IPS` (comma separated). Empty by default.

    Empty means NO forwarding header is ever believed, which is the safe
    default: every caller is then counted by the address CQ actually sees.
    """
    return frozenset(p.strip() for p in (raw or "").split(",") if p.strip())


def resolve_source(peer: str | None, x_real_ip: str | None,
                   x_forwarded_for: str | None, trusted: frozenset) -> str:
    """The identity a per-source failure counter keys on.

    A FORWARDING HEADER IS ONLY EVIDENCE IF THE PEER IS THE PROXY
    (GhostPour, 2026-09-16). Their nginx sets `X-Real-IP` from
    `$remote_addr`, replacing whatever arrived, so it is trustworthy from
    that peer. But GP itself reaches CQ container to container at
    `http://contextquilt:8000` and never passes through the proxy, so
    plenty of legitimate traffic carries no such header, and ANYTHING on
    that docker network can send one saying whatever it likes.

    Believing the header unconditionally would be worse than not counting
    at all: an attacker could rotate it to evade their own counter, and
    could POISON somebody else's by claiming the operator's address, which
    is the dashboard lockout this design exists to avoid. So the header is
    read only from a peer in `trusted`; everyone else is counted as the
    address CQ actually sees.

    `X-Forwarded-For` is the fallback and only its LAST entry is used,
    because nginx APPENDS to a client-supplied value there: everything
    before the last element is attacker-controlled even from the proxy.

    Pure, and here rather than in api_deps, so it executes in the local
    unit venv instead of only in CI.
    """
    peer = (peer or "").strip()
    if peer and peer in trusted:
        if x_real_ip and x_real_ip.strip():
            return x_real_ip.strip()
        if x_forwarded_for:
            parts = [p.strip() for p in x_forwarded_for.split(",") if p.strip()]
            if parts:
                return parts[-1]
        # The proxy forwarded nothing usable: count it as the proxy rather
        # than inventing an identity.
        return peer
    return peer or GLOBAL_BUCKET


def is_authorized(configured_key: str | None, presented_key: str | None) -> bool:
    """True when the caller may use an admin route.

    No configured key means dev mode: open. That is deliberate, matches
    both copies this replaces, and is a trapdoor: prod sets CQ_ADMIN_KEY
    (a required env var), so the gate bites there. Tested explicitly
    rather than left as an accident of the `if`.

    Compared with `!=` rather than a constant-time compare, matching the
    copies this replaces: the key is a long random operator secret and CQ
    is not where timing analysis would start.
    """
    if not configured_key:
        return True
    return presented_key == configured_key
