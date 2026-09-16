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
