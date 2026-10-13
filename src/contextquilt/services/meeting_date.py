"""The day a meeting happened, as the ingest path should read it.

One derivation, two consumers in the meeting lane: the `Meeting date:`
prompt line (what every relative deadline resolves against) and
`meeting_origins.meeting_date`.

Until 2026-10-13 this read only `payload.timestamp`, which `/v1/memory`
stamps with utcnow() when the caller sends none. ShoulderSurf never
sends one, deliberately: the date rides as `metadata.recording_started_at`
because GP's typed proxy drops unknown top-level fields, and metadata
travels as a dict. CQ never read that key, so an imported recording
from last month resolved "by Friday" against the day it was imported,
and a replay against the replay day. Live captures were only harmless
because ingest lands minutes after the meeting.

`recording_started_at` wins over `timestamp` because it is the
meeting's own clock, while `timestamp` is usually the enqueue clock.
It is NOT copied into `payload.timestamp`: that field is also every
patch's created_at, and backdating it would move recall windows and
decay, which is a different decision from anchoring a deadline.

The date is taken in the offset the string carries, so a 10:00
Central meeting is that day in Central, never the UTC day.

Pure on purpose (no fastapi, no asyncpg) so the precedence is testable
outside CI.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional


def _parse_day(raw: Any) -> Optional[date]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def resolve_meeting_date(payload: dict) -> Optional[date]:
    """metadata.recording_started_at, else payload.timestamp, else None.

    An unparseable recording_started_at falls through to the timestamp
    rather than yielding None: a bad client field must not cost the
    meeting the anchor it had before this key was read at all.
    """
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        day = _parse_day(metadata.get("recording_started_at"))
        if day is not None:
            return day
    return _parse_day(payload.get("timestamp"))
