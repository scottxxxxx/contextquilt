"""parse_completed_at: the pure half of the user-declared completion date.

Scott's ruling 2026-08-19: closing an item defaults to completed today,
and the user can override when it was actually finished. These tests pin
the contract the device builds against, including the words in the 422
body, because GP forwards status and body unchanged to the screen.
"""

from datetime import datetime, timedelta, timezone

import pytest

from contextquilt.services.completion_time import (
    DATE_ONLY_HEADROOM_DAYS,
    FUTURE_SKEW,
    CompletedAtError,
    parse_completed_at,
)

NOW = datetime(2026, 8, 19, 21, 30, 0, tzinfo=timezone.utc)


def test_absent_means_server_clock():
    assert parse_completed_at(None, now=NOW) is None


def test_past_date_only_lands_at_noon_utc():
    got = parse_completed_at("2026-08-14", now=NOW)
    assert got == datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def test_today_date_only_resolves_to_now():
    # For "today" the server clock is the honest answer; noon UTC could
    # be in the future or the past of the actual tap.
    assert parse_completed_at("2026-08-19", now=NOW) == NOW


def test_device_day_one_ahead_of_utc_still_counts_as_today():
    # A device east of UTC picks its own "today" while UTC is a day
    # behind. That is a legitimate today pick, not a future date.
    assert parse_completed_at("2026-08-20", now=NOW) == NOW


def test_date_only_beyond_headroom_is_future():
    day = (NOW + timedelta(days=DATE_ONLY_HEADROOM_DAYS + 1)).date().isoformat()
    with pytest.raises(CompletedAtError) as exc:
        parse_completed_at(day, now=NOW)
    assert exc.value.code == "FUTURE_COMPLETED_AT"


def test_datetime_with_offset_converts_to_utc():
    got = parse_completed_at("2026-08-19T14:00:00-07:00", now=NOW)
    assert got == datetime(2026, 8, 19, 21, 0, 0, tzinfo=timezone.utc)


def test_naive_datetime_is_taken_as_utc():
    got = parse_completed_at("2026-08-18T09:15:00", now=NOW)
    assert got == datetime(2026, 8, 18, 9, 15, 0, tzinfo=timezone.utc)


def test_zulu_suffix_parses():
    got = parse_completed_at("2026-08-18T09:15:00Z", now=NOW)
    assert got == datetime(2026, 8, 18, 9, 15, 0, tzinfo=timezone.utc)


def test_datetime_within_skew_passes():
    inside = (NOW + FUTURE_SKEW - timedelta(seconds=1)).isoformat()
    assert parse_completed_at(inside, now=NOW) is not None


def test_datetime_beyond_skew_is_future():
    beyond = (NOW + FUTURE_SKEW + timedelta(minutes=1)).isoformat()
    with pytest.raises(CompletedAtError) as exc:
        parse_completed_at(beyond, now=NOW)
    assert exc.value.code == "FUTURE_COMPLETED_AT"


@pytest.mark.parametrize("bad", ["", "   ", "yesterday", "2026-13-40", "08/19/2026", 12345])
def test_unparseable_is_invalid(bad):
    with pytest.raises(CompletedAtError) as exc:
        parse_completed_at(bad, now=NOW)
    assert exc.value.code == "INVALID_COMPLETED_AT"


def test_detail_body_names_field_reason_and_received_value():
    # The 422 body is user-facing through GP's verbatim passthrough:
    # it must say which field, why, and what arrived.
    with pytest.raises(CompletedAtError) as exc:
        parse_completed_at("not-a-date-x", now=NOW)
    detail = exc.value.detail()
    assert detail["field"] == "completed_at"
    assert detail["code"] == "INVALID_COMPLETED_AT"
    assert detail["received"] == "not-a-date-x"
    assert "ISO 8601" in detail["message"]


def test_received_value_is_capped():
    with pytest.raises(CompletedAtError) as exc:
        parse_completed_at("x" * 500, now=NOW)
    assert len(exc.value.detail()["received"]) <= 80


def test_returns_aware_utc():
    # The column is TIMESTAMPTZ; an ambiguous naive instant must never
    # leave this module.
    for raw in ("2026-08-14", "2026-08-18T09:15:00", "2026-08-19T14:00:00-07:00"):
        got = parse_completed_at(raw, now=NOW)
        assert got.tzinfo is not None
        assert got.utcoffset() == timedelta(0)
