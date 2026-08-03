"""Unit tests for format_open_commitments_block (deadline sweep PR).

The fetch/ordering and the sweep loop itself are DB-coupled and follow
the repo's post-deploy smoke convention; these pin the prompt block
rendering, including the overdue annotations the resolution detector
now relies on.
"""

from datetime import datetime

from src.contextquilt.services.extraction_prompts import format_open_commitments_block

NOW = datetime(2026, 6, 11, 12, 0, 0)


def _commit(pid, text, created=None, deadline_date=None):
    return {
        "patch_id": pid,
        "text": text,
        "created_at": created or datetime(2026, 6, 8, 9, 0, 0),
        "deadline_date": deadline_date,
    }


def test_empty_returns_empty_string():
    assert format_open_commitments_block([], now=NOW) == ""
    assert format_open_commitments_block(None, now=NOW) == ""


def test_basic_block_shape():
    out = format_open_commitments_block([_commit("id-1", "Send the deck")], now=NOW)
    assert "Open commitments from your prior meetings" in out
    assert "[id-1] Send the deck (committed 3d ago)" in out
    assert "resolved_commitments" in out


def test_overdue_annotation():
    out = format_open_commitments_block(
        [_commit("id-1", "Send the deck", deadline_date="2026-06-09")], now=NOW
    )
    assert "due 2026-06-09 (OVERDUE)" in out


def test_future_deadline_annotated_without_overdue():
    out = format_open_commitments_block(
        [_commit("id-1", "Send the deck", deadline_date="2026-06-20")], now=NOW
    )
    assert "due 2026-06-20" in out
    assert "OVERDUE" not in out


def test_no_deadline_renders_age_only():
    out = format_open_commitments_block([_commit("id-1", "Send the deck")], now=NOW)
    assert "due" not in out
    assert "committed 3d ago" in out


def test_garbage_deadline_date_never_raises():
    out = format_open_commitments_block(
        [_commit("id-1", "Send the deck", deadline_date="someday")], now=NOW
    )
    assert "due someday" in out  # rendered bare, no OVERDUE, no crash
    assert "OVERDUE" not in out


def test_long_text_truncated_and_newlines_flattened():
    out = format_open_commitments_block(
        [_commit("id-1", ("x" * 250) + "\nnewline")], now=NOW
    )
    assert "x" * 197 + "..." in out
    assert "\nnewline" not in out


def test_timezone_aware_created_at_handled():
    from datetime import timezone
    aware = datetime(2026, 6, 8, 9, 0, 0, tzinfo=timezone.utc)
    out = format_open_commitments_block([_commit("id-1", "Send", created=aware)], now=NOW)
    assert "committed 3d ago" in out
