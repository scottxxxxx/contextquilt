"""Is a submitted client_id even shaped like one.

WHY THIS EXISTS (2026-09-18). `applications.app_id` is a `uuid` column,
so a client_id that is not UUID-shaped makes asyncpg raise `DataError`
INSIDE the fetch. The token endpoint's outer arm then turns that into the
same 401 a wrong secret gets, which is fine for the caller and wrong for
everything else: `verify_password` is never reached, so
`auth_rate_limit.record_failure` never runs and the attempt is not
counted. A caller can therefore fail forever without ever entering the
failure counter.

It is NOT an amplification hole. That path pays a DB round trip and an
exception but no pbkdf2 hash, which is the cost the limiter exists to
prevent. What it is instead is a SILENT one: the attempt looks like a
credential failure to the caller and like a backend error to CQ's logs,
and it lands in neither counter.

Found the hard way. A smoke test of the limiter used the scratch id
`cq-smoke-20260916`, which is not UUID-shaped, so it took this path: the
401 came back exactly as predicted, and the run proved nothing about the
limiter because it never reached the code under test. A clean-firing
instrument answering a different question.

GhostPour's version of the same hazard, from their own config: their code
default for `cq_app_id` is the literal string "cloudzap". It is overridden
in every environment today, but were that env var ever absent, EVERY GP
auth attempt would take this path, CQ's counter would never increment,
and GP's own cooldown (keyed on 400/401/403) would still fire on the 401,
so the symptom would be traffic quietly degrading to `X-App-ID` forever.
They are making that default empty so it fails loudly at startup. This is
the same defence from CQ's side: a malformed id becomes an ordinary,
COUNTED credential rejection, so their failure would be loud here too.

Pure, no fastapi and no asyncpg, so the decision executes in the local
unit venv instead of only in CI.
"""
from __future__ import annotations

import uuid


def is_uuid_shaped(value) -> bool:
    """True when `value` can address the `uuid`-typed app_id column.

    Deliberately accepts what `uuid.UUID()` accepts, which is what the
    database itself accepts: the check must not be STRICTER than the
    column, or a credential that would have worked starts being refused.
    That includes the braced and urn forms, and the 32-character
    unhyphenated form.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True
