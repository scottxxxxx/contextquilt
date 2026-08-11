"""The 17a list signals: honest inputs for the situation sections."""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.people_signals import (
    CADENCE_MIN_MEETINGS,
    compute_person_signals,
)

TODAY = date(2026, 8, 11)


def _apps(*day_turn):
    return [
        {"last_seen_at": datetime(2026, m, d, 10, tzinfo=timezone.utc), "turn_count": t}
        for (m, d, t) in day_turn
    ]


def test_weekly_cadence_and_recent_weights():
    s = compute_person_signals(
        _apps((7, 7, 10), (7, 14, 8), (7, 21, None), (7, 28, 12), (8, 4, 6)),
        [], None, today=TODAY,
    )
    assert s["cadence"] == {"median_interval_days": 7, "meetings_observed": 5}
    assert s["meetings_30d"] == 4 and s["meetings_7d"] == 0
    assert s["turns_30d"] == 26  # non-null in-window turns: 8 + 12 + 6


def test_thin_history_serves_null_cadence():
    s = compute_person_signals(_apps((8, 1, 5), (8, 8, 5)), [], None, today=TODAY)
    assert s["cadence"] is None
    assert CADENCE_MIN_MEETINGS > 2


def test_open_between_counts_match_the_rows_and_you_owe_stays_null():
    they = [
        {"patch_id": "a", "deadline_date": "2026-07-01", "value": {"text": "old"}},
        {"patch_id": "b", "deadline_date": "2026-08-20", "value": {"text": "soon"}},
        {"patch_id": "c", "deadline_date": None, "value": {"text": "undated"}},
    ]
    s = compute_person_signals([], they, None, today=TODAY)
    ob = s["open_between"]
    assert ob["they_owe_open"] == 3 and ob["they_owe_overdue"] == 1
    assert ob["you_owe_open"] is None  # cannot-tell, never zero
    assert ob["next_open_item"]["patch_id"] == "a"  # overdue beats dated beats undated
    assert ob["next_open_item"]["overdue"] is True
    assert ob["next_open_item"]["direction"] == "they_owe"


def test_you_owe_can_win_urgency_and_json_string_values_parse():
    they = [{"patch_id": "b", "deadline_date": "2026-09-01", "value": '{"text": "later"}'}]
    you = [{"patch_id": "y", "deadline_date": "2026-08-01", "value": '{"text": "review notes"}'}]
    s = compute_person_signals([], they, you, today=TODAY)
    top = s["open_between"]["next_open_item"]
    assert top["direction"] == "you_owe" and top["text"] == "review notes"
    assert s["open_between"]["you_owe_open"] == 1


def test_empty_person_serves_measured_zeroes():
    s = compute_person_signals([], [], [], today=TODAY)
    assert s["open_between"]["they_owe_open"] == 0
    assert s["open_between"]["you_owe_open"] == 0  # measured, capability on
    assert s["open_between"]["next_open_item"] is None
    assert s["turns_30d"] is None  # no turn data is unknown, not zero


def test_list_rows_carry_signals():
    MAIN = (ROOT / "src" / "main.py").read_text()
    assert '"signals": compute_person_signals(appearances, they_owe, you_owe)' in MAIN


def test_mentions_are_not_meetings():
    """The RV/Raj field report: a person only NAMED in a room did not
    attend it. Mention-only appearances feed no presence number and the
    presence anchors stay null, which renders as never-met, not
    met-recently."""
    apps = [
        {"last_seen_at": datetime(2026, 8, 11, 14, tzinfo=timezone.utc),
         "turn_count": None, "capacities": ["mention"]},
    ]
    s = compute_person_signals(apps, [], None, today=TODAY)
    assert s["meetings_7d"] == 0 and s["meetings_30d"] == 0
    assert s["first_present_at"] is None and s["last_present_at"] is None


def test_presence_grades_and_unknown_capacity_count_as_present():
    apps = [
        {"last_seen_at": datetime(2026, 8, 10, tzinfo=timezone.utc),
         "turn_count": 5, "capacities": ["speaker", "mention"]},
        {"last_seen_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
         "turn_count": None, "capacities": []},  # pre-migration-31: unknown = present
        {"last_seen_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
         "turn_count": 9, "capacities": ["mention"]},  # excluded
    ]
    s = compute_person_signals(apps, [], None, today=TODAY)
    assert s["meetings_30d"] == 2
    assert s["turns_30d"] == 5  # the mention-only row's turns never count
    assert s["first_present_at"] == "2026-08-04"
    assert s["last_present_at"] == "2026-08-10"


def test_observed_projects_are_presence_grade():
    """Source guard for the rollup in _people_core: mention-grade rows
    never grant project membership; the filter stands on this."""
    MAIN = (ROOT / "src" / "main.py").read_text()
    block = MAIN.split("project_counts: dict = {}")[1].split("observed = [")[0]
    assert 'caps & {"speaker", "ownership"}' in block
    assert "continue" in block
