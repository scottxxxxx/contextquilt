"""Unit tests for sanitize_deadline_dates / validate_deadline_date."""

from datetime import date

from src.contextquilt.services.extraction_schema import (
    sanitize_deadline_dates,
    validate_deadline_date,
)


MEETING_DATE = date(2026, 6, 10)


def _content(*values):
    return {"patches": [{"type": "commitment", "value": dict(v)} for v in values]}


# ============================================================
# validate_deadline_date
# ============================================================


def test_valid_iso_date_passes_through():
    assert validate_deadline_date("2026-06-19", MEETING_DATE) == "2026-06-19"


def test_whitespace_is_trimmed():
    assert validate_deadline_date("  2026-06-19  ", MEETING_DATE) == "2026-06-19"


def test_non_string_rejected():
    assert validate_deadline_date(None, MEETING_DATE) is None
    assert validate_deadline_date(20260619, MEETING_DATE) is None
    assert validate_deadline_date({"date": "2026-06-19"}, MEETING_DATE) is None


def test_prose_rejected():
    assert validate_deadline_date("end of week", MEETING_DATE) is None
    assert validate_deadline_date("tomorrow", MEETING_DATE) is None
    assert validate_deadline_date("June 19th", MEETING_DATE) is None


def test_timestamp_rejected_must_be_bare_date():
    assert validate_deadline_date("2026-06-19T10:00:00", MEETING_DATE) is None
    assert validate_deadline_date("2026-06-19 10:00", MEETING_DATE) is None


def test_impossible_calendar_date_rejected():
    assert validate_deadline_date("2026-02-30", MEETING_DATE) is None
    assert validate_deadline_date("2026-13-01", MEETING_DATE) is None


def test_plausibility_window_around_meeting_date():
    # A deadline can predate the meeting ("that was due yesterday")
    assert validate_deadline_date("2026-06-08", MEETING_DATE) == "2026-06-08"
    # ...but not by more than the past window (~2 years)
    assert validate_deadline_date("2023-01-01", MEETING_DATE) is None
    # Far future within ~10 years is allowed
    assert validate_deadline_date("2031-01-01", MEETING_DATE) == "2031-01-01"
    # Beyond the future window is a model error (wrong decade)
    assert validate_deadline_date("2040-01-01", MEETING_DATE) is None


def test_no_meeting_date_skips_plausibility_window():
    # Backfill path: historical rows have no anchor, format-only checks
    assert validate_deadline_date("2024-09-11", None) == "2024-09-11"
    assert validate_deadline_date("not a date", None) is None


# ============================================================
# sanitize_deadline_dates
# ============================================================


def test_sanitizer_nulls_invalid_dates_in_place():
    content = _content(
        {"text": "ship it", "deadline": "end of week", "deadline_date": "garbage"},
        {"text": "send email", "deadline": "tomorrow", "deadline_date": "2026-06-11"},
    )
    sanitize_deadline_dates(content, meeting_date=MEETING_DATE)
    assert content["patches"][0]["value"]["deadline_date"] is None
    assert content["patches"][1]["value"]["deadline_date"] == "2026-06-11"


def test_sanitizer_never_touches_free_text_deadline():
    content = _content(
        {"text": "ship it", "deadline": "end of week", "deadline_date": "bogus"},
    )
    sanitize_deadline_dates(content, meeting_date=MEETING_DATE)
    assert content["patches"][0]["value"]["deadline"] == "end of week"


def test_sanitizer_leaves_patches_without_the_key_alone():
    content = _content({"text": "a trait, no deadline fields"})
    sanitize_deadline_dates(content, meeting_date=MEETING_DATE)
    assert "deadline_date" not in content["patches"][0]["value"]


def test_sanitizer_tolerates_malformed_patches():
    content = {
        "patches": [
            "not a dict",
            {"type": "commitment"},  # no value
            {"type": "commitment", "value": "string value"},
        ]
    }
    # Must not raise
    sanitize_deadline_dates(content, meeting_date=MEETING_DATE)


def test_sanitizer_tolerates_missing_patches_key():
    sanitize_deadline_dates({}, meeting_date=MEETING_DATE)
    sanitize_deadline_dates({"patches": None}, meeting_date=MEETING_DATE)


def test_sanitizer_without_meeting_date_still_enforces_format():
    content = _content(
        {"text": "x", "deadline": "soon", "deadline_date": "next Tuesday"},
        {"text": "y", "deadline": "2026-07-01", "deadline_date": "2026-07-01"},
    )
    sanitize_deadline_dates(content, meeting_date=None)
    assert content["patches"][0]["value"]["deadline_date"] is None
    assert content["patches"][1]["value"]["deadline_date"] == "2026-07-01"
