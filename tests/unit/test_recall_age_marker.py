"""The age marker (the Vijay lesson): stale episodic recall lines say
when they were observed, so a model composing a time-scoped answer can
discount what an undated line would have forced it to trust.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from contextquilt.services.recall_formatter import (
    AGE_MARKER_DAYS,
    AGE_MARKER_EXEMPT_TYPES,
    _format_patch_line,
)

TODAY = date(2026, 8, 11)


def _row(ptype, text, observed=None, created=None, value_extra=None):
    import json
    v = {"text": text}
    v.update(value_extra or {})
    return {
        "patch_type": ptype,
        "value": json.dumps(v),
        "last_observed_at": observed,
        "created_at": created,
    }


def test_stale_blocker_carries_the_observation_date():
    """The Vijay case verbatim: a five-week-old 'unreachable' blocker
    must not read like yesterday's."""
    line = _format_patch_line(
        _row("blocker", "Vijay moved back to India and is unreachable",
             observed=datetime(2026, 7, 6, tzinfo=timezone.utc)),
        today=TODAY,
    )
    assert "last observed 2026-07-06" in line


def test_fresh_lines_stay_byte_identical():
    line = _format_patch_line(
        _row("blocker", "QA env down",
             observed=datetime(2026, 8, 1, tzinfo=timezone.utc)),
        today=TODAY,
    )
    assert "last observed" not in line


def test_threshold_is_a_day_boundary():
    on = _format_patch_line(
        _row("event", "x" * 20,
             observed=datetime(2026, 7, 14, tzinfo=timezone.utc)),
        today=TODAY,
    )
    off = _format_patch_line(
        _row("event", "x" * 20,
             observed=datetime(2026, 7, 15, tzinfo=timezone.utc)),
        today=TODAY,
    )
    assert (TODAY - date(2026, 7, 14)).days == AGE_MARKER_DAYS
    assert "last observed" in on and "last observed" not in off


def test_self_typed_types_are_exempt():
    """Their freshness model already penalizes rank; a durable trait
    wearing an old date invites doubt the type does not deserve."""
    assert "trait" in AGE_MARKER_EXEMPT_TYPES
    line = _format_patch_line(
        _row("preference", "prefers written summaries over calls",
             observed=datetime(2026, 2, 1, tzinfo=timezone.utc)),
        today=TODAY,
    )
    assert "last observed" not in line


def test_coexists_with_deadline_markers():
    line = _format_patch_line(
        _row("commitment", "deliver the fallback path",
             observed=datetime(2026, 7, 1, tzinfo=timezone.utc),
             value_extra={"owner": "Suresh", "deadline_date": "2026-07-10"}),
        today=TODAY,
    )
    assert "OVERDUE" in line
    assert "last observed 2026-07-01" in line


def test_missing_dates_render_nothing():
    line = _format_patch_line(_row("takeaway", "note text here"), today=TODAY)
    assert "last observed" not in line


def test_created_at_is_the_fallback_anchor():
    line = _format_patch_line(
        _row("takeaway", "note text here",
             created=datetime(2026, 6, 1, tzinfo=timezone.utc)),
        today=TODAY,
    )
    assert "last observed 2026-06-01" in line
