"""The meeting-date anchor reads the meeting's own clock first.

ShoulderSurf carries the date as metadata.recording_started_at (GP's
typed proxy drops unknown top-level keys), and /v1/memory stamps
payload.timestamp with utcnow() when nothing is sent. Before
2026-10-13 only the timestamp was read, so imported recordings and
replays resolved relative deadlines against the ingest day.
"""

import inspect
from datetime import date

from contextquilt.services.meeting_date import resolve_meeting_date

INGEST_NOW = "2026-10-13T18:04:11.123456"  # what /v1/memory stamps


def test_recording_started_at_beats_the_enqueue_clock():
    payload = {"timestamp": INGEST_NOW,
               "metadata": {"recording_started_at": "2026-09-16T10:00:00-05:00"}}
    assert resolve_meeting_date(payload) == date(2026, 9, 16)


def test_date_is_taken_in_the_strings_own_offset():
    # 20:30 Central on Sep 16 is already Sep 17 in UTC; the meeting
    # happened on the 16th where it was recorded.
    payload = {"metadata": {"recording_started_at": "2026-09-16T20:30:00-05:00"}}
    assert resolve_meeting_date(payload) == date(2026, 9, 16)


def test_z_suffix_parses():
    payload = {"metadata": {"recording_started_at": "2026-09-23T15:00:00Z"}}
    assert resolve_meeting_date(payload) == date(2026, 9, 23)


def test_absent_key_keeps_the_old_timestamp_behaviour():
    assert resolve_meeting_date({"timestamp": INGEST_NOW}) == date(2026, 10, 13)
    assert resolve_meeting_date({"timestamp": INGEST_NOW, "metadata": {}}) == date(2026, 10, 13)


def test_unparseable_recording_started_at_falls_through_to_timestamp():
    for bad in ("", "yesterday", 1726480800, None, ["2026-09-16"]):
        payload = {"timestamp": INGEST_NOW,
                   "metadata": {"recording_started_at": bad}}
        assert resolve_meeting_date(payload) == date(2026, 10, 13), bad


def test_nothing_parseable_is_none_not_today():
    assert resolve_meeting_date({}) is None
    assert resolve_meeting_date({"timestamp": "garbage", "metadata": "notadict"}) is None


def test_payload_timestamp_is_never_rewritten():
    # timestamp is also every patch's created_at; anchoring a deadline
    # must not backdate storage.
    payload = {"timestamp": INGEST_NOW,
               "metadata": {"recording_started_at": "2026-09-16T10:00:00-05:00"}}
    resolve_meeting_date(payload)
    assert payload["timestamp"] == INGEST_NOW


def test_worker_meeting_lane_uses_the_resolver():
    # Executing tests above pin the rule; this pins that the worker's
    # meeting lane is wired to it rather than parsing timestamp itself.
    import worker
    src = inspect.getsource(worker)
    assert "meeting_date = resolve_meeting_date(payload)" in src
    assert 'raw_ts = payload.get("timestamp")' not in src
