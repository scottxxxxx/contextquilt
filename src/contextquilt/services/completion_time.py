"""Parse and validate a caller-supplied completion timestamp.

The complete endpoint historically stamped completed_at with the server
clock and nothing else. Scott's ruling (2026-08-19, from using the close
surface): the completion date defaults to today, and the user must be
able to say when a thing was actually finished. This module is the pure
half of that: it turns the request's optional `completed_at` string into
a naive-UTC datetime the endpoint can store, or raises with enough words
for a device to explain the rejection (GP passes status and body through
unchanged, so whatever we write here is what the user's screen gets).

Pure logic, no fastapi and no DB, so the local unit suite can run it.
"""

from datetime import datetime, timedelta, timezone

# Devices and servers disagree by seconds, not hours. Five minutes of
# skew keeps an honest "just now" from bouncing while still rejecting
# any moment a user could have picked on purpose.
FUTURE_SKEW = timedelta(minutes=5)

# A date-only value from a device east of UTC can name a calendar day
# UTC has not reached yet (their "today" is our "tomorrow" for up to
# ~14 hours). One day of headroom keeps every legitimate "today" pick
# accepted in every timezone; anything further out is a real future
# date and is rejected.
DATE_ONLY_HEADROOM_DAYS = 1


class CompletedAtError(ValueError):
    """Rejected completed_at, carrying the words the wire needs."""

    def __init__(self, code: str, message: str, received: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.received = str(received)[:80]

    def detail(self) -> dict:
        """The 422 body: code, field, reason, and the received value,
        so a device can say what to fix instead of "something went
        wrong" (GP's ask, 2026-08-19)."""
        return {
            "code": self.code,
            "field": "completed_at",
            "message": self.message,
            "received": self.received,
        }


def parse_completed_at(raw, now: datetime | None = None):
    """Resolve an optional caller-supplied completed_at to aware UTC.

    Returns None when raw is None (the server clock applies, which is
    the "today" default). Accepts ISO 8601:

    - A date-only value (YYYY-MM-DD) names a calendar day. A past day
      is stored at 12:00 UTC so the date survives rendering in any
      nearby timezone; the caller's "today" (including a device day up
      to one ahead of UTC) resolves to now, because for today the
      server clock IS the honest answer.
    - A datetime is converted to UTC; a naive datetime is taken as
      already UTC. More than FUTURE_SKEW ahead of now is rejected.

    The return value is timezone-aware UTC (the column is TIMESTAMPTZ
    and asyncpg should be handed an unambiguous instant).

    Raises CompletedAtError with code INVALID_COMPLETED_AT or
    FUTURE_COMPLETED_AT.
    """
    if raw is None:
        return None
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    if not isinstance(raw, str) or not raw.strip():
        raise CompletedAtError(
            "INVALID_COMPLETED_AT",
            "completed_at must be an ISO 8601 date (YYYY-MM-DD) or datetime string.",
            raw,
        )
    text = raw.strip()

    if len(text) == 10:
        try:
            day = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            raise CompletedAtError(
                "INVALID_COMPLETED_AT",
                "completed_at must be an ISO 8601 date (YYYY-MM-DD) or datetime string.",
                text,
            )
        today_utc = now_utc.date()
        if (day - today_utc).days > DATE_ONLY_HEADROOM_DAYS:
            raise CompletedAtError(
                "FUTURE_COMPLETED_AT",
                "completed_at cannot be a future date. Pick today or an earlier day.",
                text,
            )
        if day >= today_utc:
            # The caller's today (or a device day one ahead of UTC).
            return now_utc
        return datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=timezone.utc)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise CompletedAtError(
            "INVALID_COMPLETED_AT",
            "completed_at must be an ISO 8601 date (YYYY-MM-DD) or datetime string.",
            text,
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed_utc = parsed.astimezone(timezone.utc)
    if parsed_utc > now_utc + FUTURE_SKEW:
        raise CompletedAtError(
            "FUTURE_COMPLETED_AT",
            "completed_at cannot be in the future. Use the current time or earlier.",
            text,
        )
    return parsed_utc
