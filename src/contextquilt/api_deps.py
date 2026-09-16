"""FastAPI dependencies shared across CQ's routers.

Separate from `services/admin_auth` on purpose: the DECISION there is pure
and unit-testable without fastapi installed, while the dependency here
needs a real `Header(...)` default at import time. FastAPI reads the
signature when a route is registered, and a parameter whose default is a
plain string is treated as a QUERY PARAMETER rather than a header, which
would silently ignore the `X-Admin-Key` every legitimate admin caller
sends. So the Header default is written normally, in a module that
imports fastapi like any other router module.
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from contextquilt.config import get_settings
from contextquilt.services.admin_auth import is_authorized


async def verify_admin_key(x_admin_key: str = Header(default="")) -> None:
    """403 unless `X-Admin-Key` matches `CQ_ADMIN_KEY`.

    403 rather than 401 because the caller is not being invited to
    authenticate: there is one key, and either you hold it or you do not.
    """
    if not is_authorized(get_settings().cq_admin_key, x_admin_key):
        raise HTTPException(status_code=403, detail="Invalid admin key")
