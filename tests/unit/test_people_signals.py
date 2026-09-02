"""The 17a list signals: honest inputs for the situation sections."""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.people_signals import (
    CADENCE_MIN_MEETINGS,
    compute_person_signals,
    compute_question_totals,
    is_presence_grade,
    presence_anchor,
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
    assert s["cadence"] == {"median_interval_days": 7, "days_observed": 5}
    assert s["days_present_30d"] == 4 and s["days_present_7d"] == 0
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
    assert s["days_present_7d"] == 0 and s["days_present_30d"] == 0
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
    assert s["days_present_30d"] == 2
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


# Follow up pressure: the per person totals over the question counts
# captured at ingest (migration 37). Counts and denominators only. There
# is no ratio here and no served string naming a pattern, which is the
# product rule, not an oversight.


def _measured(**over):
    row = {
        "last_seen_at": datetime(2026, 8, 10, tzinfo=timezone.utc),
        "capacities": ["speaker"],
        "questions_asked": 1,
        "questions_received_explicit": 2,
        "questions_received_inferred": 1,
        "questions_from_user_explicit": 2,
        "questions_from_user_inferred": 1,
        "meeting_questions_by_user": 9,
    }
    row.update(over)
    return row


def test_question_totals_sum_across_measured_meetings():
    t = compute_question_totals([_measured(), _measured()])
    assert t["meetings_measured"] == 2
    assert t["asked"] == 2
    assert t["received_explicit"] == 4
    assert t["received_inferred"] == 2
    assert t["from_user_explicit"] == 4
    assert t["user_asked_total"] == 18


def test_the_two_grades_stay_separate():
    t = compute_question_totals([_measured()])
    assert "received" not in t
    assert t["received_explicit"] != t["received_inferred"]


def test_unmeasured_meetings_are_null_not_zero():
    # Every meeting that predates the metric. "Was asked nothing" is a
    # claim CQ must not make from a transcript it never parsed.
    old = {"last_seen_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
           "capacities": ["speaker"]}
    t = compute_question_totals([old, old])
    assert t["meetings_measured"] == 0
    assert t["meetings_present"] == 2
    assert t["asked"] is None
    assert t["received_explicit"] is None
    assert t["user_asked_total"] is None


def test_a_measured_zero_is_a_real_zero():
    t = compute_question_totals([_measured(questions_asked=0)])
    assert t["asked"] == 0


def test_nulls_are_per_field_not_per_row():
    # A meeting can know how many questions a person was asked and still
    # not know which speaker was the user, and the from_user pair has to
    # say so rather than report a zero.
    t = compute_question_totals([_measured(
        questions_from_user_explicit=None,
        questions_from_user_inferred=None,
        meeting_questions_by_user=None,
    )])
    assert t["received_explicit"] == 2
    assert t["from_user_explicit"] is None
    assert t["from_user_inferred"] is None
    assert t["user_asked_total"] is None


def test_no_appearances_at_all_is_all_null():
    t = compute_question_totals([])
    assert t["meetings_measured"] == 0
    assert t["asked"] is None


def test_presence_grade_is_one_predicate_for_every_surface():
    # The ledger's "meetings since this was last said" and the cadence
    # here must count the same meetings.
    assert is_presence_grade({"capacities": ["speaker"]}) is True
    assert is_presence_grade({"capacities": ["ownership"]}) is True
    assert is_presence_grade({"capacities": []}) is True
    assert is_presence_grade({"capacities": None}) is True
    assert is_presence_grade({"capacities": ["mention"]}) is False


# ============================================================
# presence_anchor: one implementation, two screens
# ============================================================


def test_presence_anchor_matches_what_the_list_signals_serve():
    # The whole point of splitting it out. If these ever disagree, the
    # person page and the person list disagree about when the user last
    # met someone, which is the defect SS just fixed on their side.
    from contextquilt.services.people_signals import presence_anchor

    apps = _apps((8, 1, 3), (8, 5, 9), (8, 9, 2))
    anchor = presence_anchor(apps)
    signals = compute_person_signals(apps, [], [], today=TODAY)
    assert anchor["first_present_at"] == signals["first_present_at"]
    assert anchor["last_present_at"] == signals["last_present_at"]


def test_presence_anchor_is_null_for_a_mention_only_person():
    # Null means NOT PRESENT, not "we do not know". A client must omit
    # the line rather than fall back to any other date: the entity-level
    # last_seen_at is mention-inclusive AND moves on a rename, so
    # substituting it claims a meeting that never happened.
    from contextquilt.services.people_signals import presence_anchor

    mention_only = [{"last_seen_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
                     "turn_count": None, "capacities": ["mention"]}]
    assert all(is_presence_grade(a) is False for a in mention_only)
    anchor = presence_anchor(mention_only)
    assert anchor["first_present_at"] is None
    assert anchor["last_present_at"] is None
    assert anchor["days_present"] == 0


def test_presence_anchor_counts_days_not_rows():
    # Two appearances on one day is one meeting-day, same as the list.
    from contextquilt.services.people_signals import presence_anchor

    same_day = [
        {"last_seen_at": datetime(2026, 8, 5, 9, tzinfo=timezone.utc), "turn_count": 4},
        {"last_seen_at": datetime(2026, 8, 5, 17, tzinfo=timezone.utc), "turn_count": 6},
    ]
    assert presence_anchor(same_day)["days_present"] == 1


# ---------------------------------------------- you_owe honesty gate

from datetime import datetime, timezone
from contextquilt.services.people_signals import (
    OWED_TO_OBSERVABLE_SINCE, owed_to_instrument_has_looked)

_C = {"commitment", "blocker"}
_self = lambda o: o == "Scott"
_AFTER = OWED_TO_OBSERVABLE_SINCE
_BEFORE = datetime(2026, 8, 15, tzinfo=timezone.utc)


def _row(**kw):
    base = {"patch_type": "commitment", "origin_id": "m1",
            "created_at": _AFTER, "owner": "Scott"}
    base.update(kw)
    return base


def test_an_empty_answer_needs_a_working_instrument():
    """`[]` asserts "we looked and found nothing". Before the fix the
    instrument had never worked, so nothing captured then can support
    that claim. Scott's card said "nothing open" above two items that
    named the person."""
    assert owed_to_instrument_has_looked([_row(created_at=_BEFORE)], _C, _self) is False


def test_a_post_fix_extracted_commitment_is_enough():
    assert owed_to_instrument_has_looked([_row()], _C, _self) is True


def test_a_hand_written_item_is_not_evidence_either_way():
    # The client composer sends no owed_to, so a manual item (no
    # origin) could never carry the edge and cannot vouch for absence.
    assert owed_to_instrument_has_looked([_row(origin_id=None)], _C, _self) is False


def test_somebody_elses_commitment_does_not_count():
    # you_owe is about what the USER owes; a counterparty's item says
    # nothing about the user's edges.
    assert owed_to_instrument_has_looked([_row(owner="Steven")], _C, _self) is False


def test_a_non_completable_type_does_not_count():
    assert owed_to_instrument_has_looked([_row(patch_type="decision")], _C, _self) is False


def test_the_boundary_day_itself_counts():
    # On or after, so the fix day is inside the window.
    assert owed_to_instrument_has_looked([_row(created_at=_AFTER)], _C, _self) is True


def test_one_unobservable_item_poisons_the_claim():
    """The first version passed this with True, and Scott's card kept
    saying "nothing open" above 601 items the instrument could never
    see. Looking at SOME of the evidence does not license a claim about
    all of it: one pre-fix or hand-written item might be the one owed."""
    rows = [_row(created_at=_BEFORE), _row(origin_id=None), _row()]
    assert owed_to_instrument_has_looked(rows, _C, _self) is False


def test_all_observable_is_the_only_way_to_earn_an_empty_list():
    assert owed_to_instrument_has_looked([_row(), _row(), _row()], _C, _self) is True


def test_somebody_elses_unobservable_item_does_not_poison_it():
    # Only the USER'S items bear on what the user owes.
    rows = [_row(), _row(owner="Steven", created_at=_BEFORE)]
    assert owed_to_instrument_has_looked(rows, _C, _self) is True


def test_the_nd_counts_are_days_and_the_honest_names_say_so():
    """SS read a device payload on 2026-09-01: six meetings on four days
    served as meetings_30d == 4 under a tile reading "4 MEETINGS, 30D".
    The count is distinct days present, which is the right measure of a
    rhythm, and the names now on the wire say days. The old names keep
    serving the same values until every client has moved."""
    today = date(2026, 9, 1)
    apps = _apps((9, 1, 3), (8, 28, 4), (8, 28, 2), (8, 28, 5), (8, 21, 1), (8, 17, 2))
    s = compute_person_signals(apps, [], None, today=today)
    assert len(apps) == 6
    assert s["days_present_30d"] == 4 and s["days_present_7d"] == 2
    assert s["cadence"]["days_observed"] == 4
    # RETIRED 2026-09-02 after SS verified identical values on the wire
    # for all 381 people. The misnamed keys are off the wire; a client
    # reading them now gets a KeyError rather than a wrong unit.
    assert "meetings_30d" not in s and "meetings_7d" not in s
    assert "meetings_observed" not in s["cadence"]


def test_null_cadence_stays_null_with_the_new_key():
    s = compute_person_signals(_apps((8, 1, 5), (8, 8, 5)), [], None, today=TODAY)
    assert s["cadence"] is None
    assert s["days_present_30d"] == 2


def test_presence_anchor_serves_days_present_and_the_signals_block_does_not_leak_it():
    """`presence.meetings_present` on the detail route was always
    len(days); `questions.meetings_present` beside it counts rows. The
    honest key rides alongside; the list's signals block carries neither."""
    apps = _apps((8, 10, 1), (8, 10, 2), (8, 4, 3))
    anchor = presence_anchor(apps)
    assert anchor["days_present"] == 2
    assert "meetings_present" not in anchor          # retired 2026-09-02
    s = compute_person_signals(apps, [], None, today=TODAY)
    assert "days_present" not in s and "meetings_present" not in s
