"""Closure mode: the classifier, at its boundaries.

Two of these matter more than the rest.

The first is `re_dated` against `not_raised_since`, because they look
identical in a list of open overdue items and mean different things. A
re-dated item is being managed against a calendar. A not_raised_since one
has not come up, and the test for that is meetings with THAT PERSON,
never elapsed days: a fortnight of silence while neither of them was in a
room together is not evidence of anything.

That mode is also the only one in the ledger where ABSENCE does the work,
which is why its name says what was observed rather than what it probably
means. The item may have been finished by email on the Tuesday and never
mentioned again, and CQ would hold identical evidence either way, so
every test here asserts the conversation and none of them asserts the
work.

The second is the empty `deadline_history` case, because that is the
state of every completable in production on the day this ships. Nothing
here may need a restatement to work.
"""

import re
from datetime import date, datetime
from pathlib import Path

import pytest

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()

from contextquilt.services.commitment_ledger import (
    ABSORBED_BY_USER,
    DELIVERED,
    MODE_PRECEDENCE,
    OPEN,
    REASSIGNED,
    RESTATED,
    RE_DATED,
    NOT_RAISED_SINCE,
    classify_item,
    classify_items,
    object_regression,
    summarize,
)

TODAY = date(2026, 8, 13)


def item(**over):
    """A plain open commitment, shaped exactly as the read query serves
    it: no restatements, no deadline history, which is production."""
    row = {
        "patch_id": "p1",
        "patch_type": "commitment",
        "text": "Send the vendor shortlist",
        "owner": "Marcus",
        "origin_id": "meeting-1",
        "created_at": datetime(2026, 6, 1, 9, 0),
        "deadline_date": None,
        "deadline_history": [],
        "restatements": [],
        "restatement_count": None,
        "completed_at": None,
        "shelved_at": None,
    }
    row.update(over)
    return row


def met(*days):
    """Presence-grade appearances on the given days."""
    return [
        {"last_seen_at": datetime(2026, d[0], d[1], 10, 0), "capacities": ["speaker"]}
        for d in days
    ]


def restatement(day, text, owner="Marcus", deadline_date=None, origin="m2"):
    return {
        "observed_at": datetime(2026, day[0], day[1], 10, 0).isoformat(),
        "text": text,
        "owner": owner,
        "deadline": None,
        "deadline_date": deadline_date,
        "origin_id": origin,
    }


class TestProductionShapedData:
    """Nothing in the classifier may require a restatement to work: on
    the day this ships, every one of the 1021 stored completables has an
    empty history."""

    def test_a_bare_open_item_is_open(self):
        got = classify_item(item(), TODAY)
        assert got["mode"] == OPEN
        assert got["modes"] == [OPEN]
        assert got["hop_count"] == 0
        assert got["restatements"] == []

    def test_a_bare_item_still_reports_its_age(self):
        got = classify_item(item(), TODAY)
        assert got["first_stated_on"] == "2026-06-01"
        assert got["last_stated_on"] == "2026-06-01"
        assert got["days_open"] == 73

    def test_a_past_due_item_nobody_has_raised_in_two_meetings_is_dropped(self):
        # The production-reachable mode: stated once, past its date, and
        # the two of them have met twice since without it coming up.
        got = classify_item(
            item(deadline_date="2026-06-20"),
            TODAY,
            meeting_days=[d.date() for d in
                          (datetime(2026, 7, 2), datetime(2026, 7, 30))],
        )
        assert got["mode"] == NOT_RAISED_SINCE
        assert got["meetings_since_last_statement"] == 2

    def test_missing_created_at_is_a_cannot_tell_not_a_zero(self):
        got = classify_item(item(created_at=None, deadline_date="2026-06-20"), TODAY)
        assert got["days_open"] is None
        assert got["first_stated_on"] is None
        assert got["meetings_since_last_statement"] is None
        assert got["mode"] == OPEN


class TestDroppedVersusReDated:
    ONE_MEETING = [date(2026, 7, 2)]
    TWO_MEETINGS = [date(2026, 7, 2), date(2026, 7, 30)]

    def test_one_meeting_since_is_not_a_drop(self):
        got = classify_item(
            item(deadline_date="2026-06-20"), TODAY, meeting_days=self.ONE_MEETING
        )
        assert got["mode"] == OPEN
        assert got["meetings_since_last_statement"] == 1

    def test_no_meetings_at_all_is_not_a_drop(self):
        # Two months of silence while they never met. Elapsed days would
        # call this abandoned; meetings say nothing has been asked yet.
        got = classify_item(item(deadline_date="2026-06-20"), TODAY, meeting_days=[])
        assert got["mode"] == OPEN
        assert got["meetings_since_last_statement"] == 0

    def test_meetings_before_the_last_statement_do_not_count(self):
        # They met twice, but the item was restated after both, so it has
        # not fallen out of anything.
        got = classify_item(
            item(
                deadline_date="2026-06-20",
                restatements=[restatement((8, 5), "Still on it, this week")],
            ),
            TODAY,
            meeting_days=self.TWO_MEETINGS,
        )
        assert got["meetings_since_last_statement"] == 0
        assert got["mode"] == RESTATED

    def test_an_item_still_ahead_of_its_date_is_never_dropped(self):
        got = classify_item(
            item(deadline_date="2026-09-30"), TODAY, meeting_days=self.TWO_MEETINGS
        )
        assert got["mode"] == OPEN

    def test_an_undated_item_is_never_dropped(self):
        # No date means the question was never asked of it.
        got = classify_item(item(), TODAY, meeting_days=self.TWO_MEETINGS)
        assert got["mode"] == OPEN

    def test_a_shelved_item_is_never_dropped(self):
        # "Let it go" is the user releasing the item, so the silence
        # after it is the user's decision, not the other person's.
        got = classify_item(
            item(deadline_date="2026-06-20", shelved_at="2026-07-01T00:00:00"),
            TODAY, meeting_days=self.TWO_MEETINGS,
        )
        assert got["mode"] == OPEN

    def test_a_re_dated_item_is_re_dated_when_it_is_still_being_raised(self):
        got = classify_item(
            item(
                deadline_date="2026-09-01",
                deadline_history=[{"deadline_date": "2026-06-20"}],
                restatements=[restatement((8, 5), "New date, first week of Sep")],
            ),
            TODAY, meeting_days=self.TWO_MEETINGS,
        )
        assert got["mode"] == RE_DATED
        assert got["deadline_moves"] == 1

    def test_a_re_dated_item_that_went_quiet_reports_both(self):
        # The boundary. It was managed once, then stopped being managed,
        # and the headline is the thing that stopped. Nothing is lost:
        # the re-date is still on the item, in `modes`.
        got = classify_item(
            item(
                deadline_date="2026-06-25",
                deadline_history=[{"deadline_date": "2026-06-01"}],
                restatements=[restatement((6, 15), "Pushing it a week")],
            ),
            TODAY, meeting_days=self.TWO_MEETINGS,
        )
        assert got["mode"] == NOT_RAISED_SINCE
        assert got["modes"] == [NOT_RAISED_SINCE, RE_DATED]

    def test_a_completed_item_is_never_dropped(self):
        got = classify_item(
            item(deadline_date="2026-06-20", completed_at=datetime(2026, 7, 1)),
            TODAY, meeting_days=self.TWO_MEETINGS,
        )
        assert got["mode"] == DELIVERED


class TestTheMolt:
    def test_restated_with_no_date_movement_is_the_molt(self):
        # The shape the design is for: three meetings, three
        # commitments, one object, no state change.
        got = classify_item(
            item(
                deadline_date="2026-09-30",
                restatements=[
                    restatement((6, 20), "Partial, trying to get on Renata's calendar"),
                    restatement((7, 18), "Request in with legal, honestly? Weeks"),
                ],
            ),
            TODAY,
        )
        assert got["mode"] == RESTATED
        assert got["hop_count"] == 2
        assert got["deadline_moves"] == 0
        assert [r["text"] for r in got["restatements"]] == [
            "Partial, trying to get on Renata's calendar",
            "Request in with legal, honestly? Weeks",
        ]

    def test_the_counter_survives_the_capped_array(self):
        # Fourteen hops, ten receipts. The count is the claim.
        got = classify_item(
            item(restatement_count=14, restatements=[restatement((7, 1), "Soon")]),
            TODAY,
        )
        assert got["hop_count"] == 14
        assert len(got["restatements"]) == 1

    def test_a_stored_count_that_is_not_a_number_is_ignored(self):
        got = classify_item(item(restatement_count="lots"), TODAY)
        assert got["hop_count"] == 0

    def test_the_count_arrives_as_text_from_a_json_select(self):
        got = classify_item(item(restatement_count="3"), TODAY)
        assert got["hop_count"] == 3

    def test_history_length_is_a_hop_floor_for_legacy_rows(self):
        # A row that predates the restatement array still has its moved
        # dates, and two moves is at least two hops.
        got = classify_item(
            item(deadline_history=[{"deadline_date": "1"}, {"deadline_date": "2"}]),
            TODAY,
        )
        assert got["hop_count"] == 2

    def test_a_re_date_is_not_counted_twice(self):
        # On this write path a re-date IS a restatement, so the two
        # signals are maxed, never added.
        got = classify_item(
            item(
                restatement_count=2,
                restatements=[restatement((7, 1), "a"), restatement((7, 20), "b")],
                deadline_history=[{"deadline_date": "1"}, {"deadline_date": "2"}],
            ),
            TODAY,
        )
        assert got["hop_count"] == 2

    def test_json_encoded_arrays_are_read(self):
        # jsonb arrives as a string from some fetch paths.
        got = classify_item(
            item(
                restatements='[{"observed_at": "2026-07-01", "text": "Soon", "owner": "Marcus"}]',
                deadline_history='[{"deadline_date": "2026-06-01"}]',
            ),
            TODAY,
        )
        assert got["hop_count"] == 1
        assert got["deadline_moves"] == 1

    def test_a_history_that_arrives_as_a_count_is_read(self):
        got = classify_item(item(deadline_history=3), TODAY)
        assert got["deadline_moves"] == 3

    def test_unparseable_history_degrades_to_none_rather_than_raising(self):
        got = classify_item(item(restatements="{not json", deadline_history=None), TODAY)
        assert got["hop_count"] == 0


class TestOwnership:
    def test_a_new_owner_named_in_a_restatement_is_a_reassignment(self):
        got = classify_item(
            item(restatements=[restatement((7, 1), "Priya is picking this up", owner="Priya")]),
            TODAY, user_label="Scott Guida",
        )
        assert got["mode"] == REASSIGNED
        assert got["owner_change"]["from"] == "Marcus"
        assert got["owner_change"]["to"] == "Priya"
        assert got["owner_change"]["to_user"] is False

    def test_an_item_that_became_the_users_own_problem_says_so(self):
        # The count the phase two view is built on: whose work the user
        # ends up holding themselves.
        got = classify_item(
            item(restatements=[restatement((7, 1), "I will just do it", owner="Scott")]),
            TODAY, user_label="Scott Guida",
        )
        assert got["mode"] == ABSORBED_BY_USER
        assert got["owner_change"]["to_user"] is True

    def test_the_same_owner_restated_is_not_a_handover(self):
        got = classify_item(
            item(restatements=[restatement((7, 1), "Still mine", owner="  marcus ")]),
            TODAY, user_label="Scott Guida",
        )
        assert got["owner_change"] is None
        assert got["mode"] == RESTATED

    def test_a_restatement_with_no_owner_is_not_a_handover_to_the_user(self):
        # is_self_owned treats an empty owner as the user's own, which is
        # right for the you_owe ledger and would be a fabricated
        # handover here.
        got = classify_item(
            item(restatements=[restatement((7, 1), "Still open", owner=None)]),
            TODAY, user_label="Scott Guida",
        )
        assert got["owner_change"] is None
        assert got["mode"] == RESTATED

    def test_the_last_handover_wins(self):
        got = classify_item(
            item(restatements=[
                restatement((7, 1), "Priya has it", owner="Priya"),
                restatement((7, 20), "Back to me", owner="Scott"),
            ]),
            TODAY, user_label="Scott Guida",
        )
        assert got["mode"] == ABSORBED_BY_USER
        assert got["owner_change"]["to"] == "Scott"

    def test_a_handover_outranks_a_drop_and_keeps_both(self):
        got = classify_item(
            item(
                deadline_date="2026-06-20",
                restatements=[restatement((6, 25), "I will take it", owner="Scott")],
            ),
            TODAY, user_label="Scott Guida",
            meeting_days=[date(2026, 7, 2), date(2026, 7, 30)],
        )
        assert got["mode"] == ABSORBED_BY_USER
        assert NOT_RAISED_SINCE in got["modes"]

    def test_delivery_outranks_everything(self):
        got = classify_item(
            item(
                completed_at=datetime(2026, 7, 5),
                deadline_history=[{"deadline_date": "1"}],
                restatements=[restatement((7, 1), "Priya has it", owner="Priya")],
            ),
            TODAY, user_label="Scott Guida",
        )
        assert got["mode"] == DELIVERED
        assert got["modes"][0] == DELIVERED
        assert REASSIGNED in got["modes"]


class TestModesAreOrderedAndClosed:
    def test_every_mode_a_classifier_can_emit_has_a_precedence(self):
        got = classify_item(
            item(
                deadline_date="2026-06-01",
                deadline_history=[{"deadline_date": "1"}],
                restatements=[restatement((6, 5), "Priya has it", owner="Priya")],
            ),
            TODAY, meeting_days=[date(2026, 7, 1), date(2026, 8, 1)],
        )
        assert got["modes"] == sorted(got["modes"], key=MODE_PRECEDENCE.index)
        assert got["mode"] == got["modes"][0]

    def test_restated_and_re_dated_are_exclusive(self):
        # A date that moved is a re-date, not a molt: the molt is the
        # item whose date never moves.
        got = classify_item(
            item(
                deadline_date="2026-09-30",
                deadline_history=[{"deadline_date": "2026-06-01"}],
                restatements=[restatement((7, 1), "New date")],
            ),
            TODAY,
        )
        assert RESTATED not in got["modes"]
        assert got["mode"] == RE_DATED


class TestRegressionSeam:
    def test_object_regression_is_honestly_null(self):
        assert object_regression({"restatements": []}) is None
        got = classify_item(
            item(restatements=[
                restatement((7, 1), "A name"),
                restatement((7, 20), "A shortlist that still needs cleaning up"),
            ]),
            TODAY,
        )
        assert got["object_regression"] is None

    def test_an_injected_verdict_flows_through_unchanged(self):
        # The seam a cold path judge lands in, with no served field
        # changing shape.
        items = classify_items(
            [item(patch_id="p9", restatements=[restatement((7, 1), "Soon")])],
            TODAY, regressions={"p9": True},
        )
        assert items[0]["object_regression"] is True

    def test_a_non_boolean_verdict_is_refused(self):
        items = classify_items(
            [item(patch_id="p9")], TODAY, regressions={"p9": "probably"},
        )
        assert items[0]["object_regression"] is None


class TestMeetingsAreCountedByPresence:
    def test_a_mention_is_not_a_meeting(self):
        # Someone named in two rooms they were never in has not been met
        # twice, and an item cannot fall out of a conversation that did
        # not happen.
        mentions = [
            {"last_seen_at": datetime(2026, 7, 2), "capacities": ["mention"]},
            {"last_seen_at": datetime(2026, 7, 30), "capacities": ["mention"]},
        ]
        got = classify_items(
            [item(deadline_date="2026-06-20")], TODAY, appearances=mentions
        )
        assert got[0]["meetings_since_last_statement"] == 0
        assert got[0]["mode"] == OPEN

    def test_rows_with_no_capacity_count_as_presence(self):
        # Migration 31's rule: unknown must not become "did not attend".
        legacy = [
            {"last_seen_at": datetime(2026, 7, 2), "capacities": []},
            {"last_seen_at": datetime(2026, 7, 30), "capacities": None},
        ]
        got = classify_items(
            [item(deadline_date="2026-06-20")], TODAY, appearances=legacy
        )
        assert got[0]["mode"] == NOT_RAISED_SINCE

    def test_two_appearances_on_one_day_are_one_meeting_day(self):
        same_day = [
            {"last_seen_at": datetime(2026, 7, 2, 9), "capacities": ["speaker"]},
            {"last_seen_at": datetime(2026, 7, 2, 16), "capacities": ["speaker"]},
        ]
        got = classify_items(
            [item(deadline_date="2026-06-20")], TODAY, appearances=same_day
        )
        assert got[0]["meetings_since_last_statement"] == 1
        assert got[0]["mode"] == OPEN


class TestSummary:
    def test_counts_carry_their_denominator_and_their_ids(self):
        items = classify_items(
            [
                item(patch_id="a", restatements=[restatement((7, 1), "Soon")]),
                item(patch_id="b", restatements=[
                    restatement((7, 1), "Soon"), restatement((7, 20), "Weeks"),
                ]),
                item(patch_id="c", completed_at=datetime(2026, 7, 5)),
            ],
            TODAY,
        )
        s = summarize(items)
        assert s["items"] == 3
        assert s["by_mode"][RESTATED] == 2
        assert s["by_mode"][DELIVERED] == 1
        assert sorted(s["patch_ids_by_mode"][RESTATED]) == ["a", "b"]
        assert s["patch_ids_by_mode"][DELIVERED] == ["c"]
        # Every mode key is present, so a client decodes one shape.
        assert set(s["by_mode"]) == set(MODE_PRECEDENCE)
        # The counts add up to the denominator: nothing is double
        # counted, whatever else is true of an item.
        assert sum(s["by_mode"].values()) == s["items"]

    def test_no_ratio_is_ever_served(self):
        items = classify_items([item(patch_id="a")], TODAY)
        s = summarize(items)
        assert not any(
            k for k in s
            if "percent" in k or "rate" in k or "ratio" in k or "score" in k
        )

    def test_the_median_is_null_on_an_empty_set_never_nan(self):
        # GhostPour serializes with allow_nan false: one non finite float
        # turns the whole person payload into a 502.
        s = summarize([])
        assert s["median_hop_count"] is None
        assert s["max_hop_count"] is None
        assert s["items"] == 0

    def test_the_median_is_a_finite_number(self):
        items = classify_items(
            [
                item(patch_id="a", restatement_count=1),
                item(patch_id="b", restatement_count=2),
                item(patch_id="c", restatement_count=3),
                item(patch_id="d", restatement_count=6),
            ],
            TODAY,
        )
        s = summarize(items)
        assert s["median_hop_count"] == 2.5
        assert s["max_hop_count"] == 6

    def test_not_raised_since_is_pulled_out_by_name(self):
        items = classify_items(
            [item(patch_id="a", deadline_date="2026-06-01")],
            TODAY, appearances=met((7, 1), (8, 1)),
        )
        s = summarize(items)
        assert s["not_raised_since"] == 1
        assert s["by_mode"][NOT_RAISED_SINCE] == 1
        # The number a client renders the sentence from: "has not come
        # up in your last 2 meetings with her", which is true whatever
        # happened offline.
        assert s["max_meetings_not_raised"] == 2
        assert items[0]["meetings_since_last_statement"] == 2

    def test_the_peak_is_null_not_zero_when_nothing_is_in_that_mode(self):
        s = summarize(classify_items([item()], TODAY))
        assert s["not_raised_since"] == 0
        assert s["max_meetings_not_raised"] is None

    def test_the_mode_never_claims_the_work_stopped(self):
        """The name is the guard. An item finished by email on the
        Tuesday and never mentioned again produces exactly this state,
        so nothing may be served that asserts otherwise."""
        items = classify_items(
            [item(patch_id="a", deadline_date="2026-06-01")],
            TODAY, appearances=met((7, 1), (8, 1)),
        )
        got = items[0]
        assert got["mode"] == "not_raised_since"
        # Everything served about it is a fact about the conversation.
        assert got["meetings_since_last_statement"] == 2
        assert got["last_stated_on"] == "2026-06-01"
        blob = repr(got) + repr(summarize(items))
        for verdict in ("dropped", "abandoned", "ignored", "stalled", "neglect"):
            assert verdict not in blob


class TestOrdering:
    def test_items_come_back_in_a_total_order(self):
        rows = [
            item(patch_id="b", created_at=datetime(2026, 5, 1)),
            item(patch_id="a", created_at=datetime(2026, 5, 1)),
            item(patch_id="c", restatement_count=4, created_at=datetime(2026, 7, 1)),
        ]
        first = [i["patch_id"] for i in classify_items(rows, TODAY)]
        second = [i["patch_id"] for i in classify_items(list(reversed(rows)), TODAY)]
        # Most hops first, then oldest, then id.
        assert first == ["c", "a", "b"]
        assert first == second


class TestOwnerIsNeverCanonicalized:
    def test_the_raw_surface_form_is_what_is_served(self):
        got = classify_item(item(owner="  marcus v. "), TODAY)
        assert got["owner"] == "  marcus v. "


class TestChases:
    """The metric that carries the follow up finding, after the first one
    was measured and did not hold.

    Questions RECEIVED is nearly level across people whose follow up is
    nothing alike (twelve against ten on the real transcripts), because a
    chase and a substantive probe both count as one question. What
    separates them is whether the item moved. So the count is chases on
    items already in the ledger that produced no advance, and question
    volume stays its own number: conflating the two is what produced a
    false claim.
    """

    def _appearances(self, *rows):
        """(month, day, from_user_explicit, from_user_inferred, origin)."""
        return [
            {
                "origin_id": o,
                "last_seen_at": datetime(2026, m, d, 10, 0),
                "capacities": ["speaker"],
                "questions_from_user_explicit": ex,
                "questions_from_user_inferred": inf,
            }
            for m, d, ex, inf, o in rows
        ]

    def test_a_chase_that_moved_nothing_is_counted(self):
        # The item came up in a meeting where the user pressed, and it
        # had not closed by the next meeting with that person.
        got = classify_items(
            [item(restatements=[restatement((7, 1), "Still on it", origin="m2")])],
            TODAY,
            appearances=self._appearances(
                (7, 1, 2, 0, "m2"), (7, 20, 0, 0, "m3"),
            ),
        )
        c = got[0]["chases"]
        assert c["total"] == 1
        assert c["without_advance"] == 1
        assert c["with_advance"] == 0
        assert c["unresolved"] == 0
        assert c["occasions"][0]["next_meeting_on"] == "2026-07-20"
        assert c["occasions"][0]["advanced"] is False

    def test_a_chase_the_item_closed_after_is_an_advance(self):
        got = classify_items(
            [item(
                completed_at=datetime(2026, 7, 10),
                restatements=[restatement((7, 1), "On it", origin="m2")],
            )],
            TODAY,
            appearances=self._appearances((7, 1, 1, 0, "m2"), (7, 20, 0, 0, "m3")),
        )
        c = got[0]["chases"]
        assert c["with_advance"] == 1
        assert c["without_advance"] == 0
        assert c["occasions"][0]["advanced"] is True

    def test_closing_after_the_next_meeting_is_not_an_advance_for_that_chase(self):
        # It closed eventually. The question this metric asks is whether
        # THAT chase moved it, and by the next meeting it had not.
        got = classify_items(
            [item(
                completed_at=datetime(2026, 8, 1),
                restatements=[restatement((7, 1), "On it", origin="m2")],
            )],
            TODAY,
            appearances=self._appearances((7, 1, 1, 0, "m2"), (7, 20, 0, 0, "m3")),
        )
        assert got[0]["chases"]["without_advance"] == 1

    def test_a_re_date_is_not_an_advance(self):
        # The whole point. Motion that reads as progress at a checkpoint
        # is the illusion, so only closing counts.
        got = classify_items(
            [item(
                deadline_date="2026-09-01",
                deadline_history=[{"deadline_date": "2026-07-15"}],
                restatements=[restatement(
                    (7, 1), "Pushing to September", deadline_date="2026-09-01",
                    origin="m2",
                )],
            )],
            TODAY,
            appearances=self._appearances((7, 1, 3, 0, "m2"), (7, 20, 0, 0, "m3")),
        )
        assert got[0]["chases"]["without_advance"] == 1
        assert got[0]["chases"]["advance_definition"] == (
            "closed_by_the_next_meeting_with_this_person"
        )

    def test_a_chase_in_the_latest_meeting_is_unresolved_not_a_failure(self):
        # Nothing has had a chance to happen yet, and counting it as no
        # advance would manufacture the finding out of recency.
        got = classify_items(
            [item(restatements=[restatement((7, 20), "This week", origin="m3")])],
            TODAY,
            appearances=self._appearances((7, 1, 0, 0, "m2"), (7, 20, 2, 0, "m3")),
        )
        c = got[0]["chases"]
        assert c["total"] == 1
        assert c["unresolved"] == 1
        assert c["without_advance"] == 0
        assert c["occasions"][0]["advanced"] is None

    def test_an_item_that_came_up_with_no_question_is_not_a_chase(self):
        got = classify_items(
            [item(restatements=[restatement((7, 1), "Mentioned it", origin="m2")])],
            TODAY,
            appearances=self._appearances((7, 1, 0, 0, "m2"), (7, 20, 0, 0, "m3")),
        )
        c = got[0]["chases"]
        assert c["total"] == 0
        assert c["unmeasurable"] == 0

    def test_a_meeting_with_no_question_metric_is_unmeasurable_not_zero(self):
        # Every meeting on the day this ships. A client rendering the
        # chase count without this number is reporting a floor as a
        # total.
        got = classify_items(
            [item(restatements=[restatement((7, 1), "Still on it", origin="m2")])],
            TODAY,
            appearances=self._appearances(
                (7, 1, None, None, "m2"), (7, 20, None, None, "m3"),
            ),
        )
        c = got[0]["chases"]
        assert c["unmeasurable"] == 1
        assert c["total"] == 0
        assert c["without_advance"] == 0

    def test_a_restatement_in_a_room_this_person_was_not_in_is_not_a_chase(self):
        got = classify_items(
            [item(restatements=[restatement((7, 1), "Came up elsewhere", origin="zz")])],
            TODAY,
            appearances=self._appearances((7, 20, 5, 0, "m3")),
        )
        c = got[0]["chases"]
        assert c["total"] == 0
        assert c["unmeasurable"] == 0

    def test_three_chases_on_one_item_across_three_meetings(self):
        # The sentence the finding is actually made of.
        got = classify_items(
            [item(restatements=[
                restatement((6, 1), "End of next week, easy", origin="m1"),
                restatement((7, 1), "Partial, chasing Renata", origin="m2"),
                restatement((7, 20), "Request in with legal, weeks", origin="m3"),
            ])],
            TODAY,
            appearances=self._appearances(
                (6, 1, 2, 0, "m1"), (7, 1, 1, 1, "m2"),
                (7, 20, 3, 0, "m3"), (8, 5, 0, 0, "m4"),
            ),
        )
        c = got[0]["chases"]
        assert c["total"] == 3
        assert c["without_advance"] == 3
        s = summarize(got)
        assert s["chases_without_advance"] == 3
        assert s["items_chased_without_advance"] == 1
        assert s["max_chases_without_advance_on_one_item"] == 3

    def test_both_definitions_travel_with_the_counts(self):
        # A number whose definition lives in a docstring is a number
        # nobody can safely reuse. The chase definition is the honest
        # boundary of the count: the join is meeting level, so an
        # occasion where the item came up and the user asked about
        # something else is in here.
        got = classify_items(
            [item(restatements=[restatement((7, 1), "Still on it", origin="m2")])],
            TODAY,
            appearances=self._appearances((7, 1, 2, 0, "m2"), (7, 20, 0, 0, "m3")),
        )
        c = got[0]["chases"]
        assert c["chase_definition"] == (
            "item_raised_in_a_meeting_where_the_user_asked_this_person_a_question"
        )
        assert c["advance_definition"] == (
            "closed_by_the_next_meeting_with_this_person"
        )
        s = summarize(got)
        assert s["chase_definition"] == c["chase_definition"]
        assert s["chase_advance_definition"] == c["advance_definition"]

    def test_the_summary_totals_and_names_the_definition(self):
        items = classify_items(
            [
                item(patch_id="a", restatements=[
                    restatement((7, 1), "Soon", origin="m2")]),
                item(patch_id="b", restatements=[
                    restatement((7, 1), "Also soon", origin="m2")]),
                item(patch_id="c"),
            ],
            TODAY,
            appearances=self._appearances((7, 1, 4, 0, "m2"), (7, 20, 0, 0, "m3")),
        )
        s = summarize(items)
        assert s["chases"] == 2
        assert s["chases_without_advance"] == 2
        assert sorted(s["patch_ids_chased_without_advance"]) == ["a", "b"]
        assert s["chase_advance_definition"] == (
            "closed_by_the_next_meeting_with_this_person"
        )

    def test_no_chases_leaves_the_peak_null_never_zero(self):
        s = summarize(classify_items([item()], TODAY))
        assert s["chases"] == 0
        assert s["chases_without_advance"] == 0
        assert s["max_chases_without_advance_on_one_item"] is None
        assert s["patch_ids_chased_without_advance"] == []

    def test_production_data_produces_no_chases_and_says_so(self):
        # Today: no restatement has ever been recorded and no meeting
        # carries a question count. Both numbers are zero, and zero
        # chases with zero unmeasurable is the honest reading of a
        # corpus where nothing has been observed yet.
        got = classify_items(
            [item(deadline_date="2026-06-20")],
            TODAY,
            appearances=[
                {"origin_id": "m1", "last_seen_at": datetime(2026, 7, 2),
                 "capacities": ["speaker"]},
                {"origin_id": "m2", "last_seen_at": datetime(2026, 7, 30),
                 "capacities": ["speaker"]},
            ],
        )
        assert got[0]["chases"]["total"] == 0
        assert got[0]["chases"]["unmeasurable"] == 0
        # The modes that do not need a restatement still work.
        assert got[0]["mode"] == NOT_RAISED_SINCE

    def test_volume_and_chases_are_different_numbers_by_construction(self):
        # Two people, level question volume, opposite follow up. The
        # ledger separates them; a count of questions received cannot.
        chased = classify_items(
            [item(patch_id="a", restatements=[
                restatement((6, 1), "Soon", origin="m1"),
                restatement((7, 1), "Still soon", origin="m2"),
            ])],
            TODAY,
            appearances=self._appearances(
                (6, 1, 6, 0, "m1"), (7, 1, 6, 0, "m2"), (7, 20, 0, 0, "m3"),
            ),
        )
        ahead = classify_items(
            [item(patch_id="b", completed_at=datetime(2026, 6, 5))],
            TODAY,
            appearances=self._appearances(
                (6, 1, 6, 0, "n1"), (7, 1, 6, 0, "n2"), (7, 20, 0, 0, "n3"),
            ),
        )
        # Identical volume, 12 questions each.
        assert summarize(chased)["chases_without_advance"] == 2
        assert summarize(ahead)["chases_without_advance"] == 0


class TestServedSurface:
    """Source guards for the wiring (fastapi is absent in the local unit
    environment, so the route bodies are checked as source the same way
    the completion-history and project-rollup guards are)."""

    def test_the_ledger_is_computed_from_the_same_rows_the_arrays_carry(self):
        # If this ever drifts onto a separate fetch, a count could
        # disagree with the list it opens, which is the one thing SS
        # holds CQ to on this surface.
        block = MAIN.split("ledger_items = commitment_ledger.classify_items(")[1]
        assert "they_owe + completed_they_owe" in block.split(")")[0]
        assert "appearances=appearances" in block.split(")")[0]

    def test_the_detail_route_serves_the_ledger_with_its_scope(self):
        block = MAIN.split('"commitment_ledger": {')[1].split("},")[0]
        assert '"scope": row["_ledger_scope"]' in block
        assert '"items": row["_ledger_items"]' in block
        assert "commitment_ledger.summarize(row[\"_ledger_items\"])" in block

    def test_the_private_keys_never_leak_onto_a_public_row(self):
        # _public_person strips underscore keys, which is why the raw
        # item list is stashed under one.
        assert '"_ledger_items": ledger_items' in MAIN
        assert re.search(
            r"def _public_person.*?startswith\(\"_\"\)", MAIN, re.DOTALL
        )

    def test_the_aggregate_is_computed_over_the_unfiltered_population(self):
        # A user-level number must not move when a caller pages or
        # filters the list.
        block = MAIN.split("commitment_pressure = {")[1].split("\n    }")[0]
        assert 'for r in core["people"]' in block
        assert "for r in rows" not in block
        assert '"scope": "open_only"' in block
        assert '"people_considered": total_unfiltered' in block

    def test_the_list_serves_counts_and_the_detail_serves_receipts(self):
        # Stripped by FAMILY, so a receipt key added later cannot quietly
        # start growing a browse payload.
        block = MAIN.split("def _counts_only")[1].split("commitment_pressure = {")[0]
        assert "k not in commitment_ledger.RECEIPT_KEYS" in block
        pressure = MAIN.split("commitment_pressure = {")[1].split("\n    }")[0]
        assert "_counts_only(all_ledger_items)" in pressure
        assert "_counts_only(r[\"_ledger_items\"])" in pressure
        assert "patch_ids" not in pressure
        # The detail route keeps the full summary, ids included.
        detail = MAIN.split('"commitment_ledger": {')[1].split("},")[0]
        assert "commitment_ledger.summarize(" in detail
        assert "RECEIPT_KEYS" not in detail

    def test_every_receipt_key_the_service_produces_is_declared(self):
        # The guard on the guard: a receipt key that never made it into
        # RECEIPT_KEYS would leak onto the list silently.
        from contextquilt.services.commitment_ledger import RECEIPT_KEYS
        s = summarize(classify_items([item(patch_id="a")], TODAY))
        assert {k for k in s if k.startswith("patch_ids")} == set(RECEIPT_KEYS)

    def test_the_aggregate_serves_no_ratio(self):
        block = MAIN.split("commitment_pressure = {")[1].split("\n    }")[0]
        assert not re.search(r"(percent|ratio|_rate|score)", block)

    def test_the_open_item_query_selects_the_molt_columns(self):
        m = re.search(r"open_items = await conn\.fetch\(\s*\"\"\"(.*?)\"\"\"", MAIN, re.DOTALL)
        assert m
        for col in ("'restatements'", "'restatement_count'", "'deadline_history'"):
            assert col in m.group(1)

    def test_meetings_carry_both_question_grades_separately(self):
        block = MAIN.split('"questions": {')[1].split("},")[0]
        assert '"received_explicit": a["questions_received_explicit"]' in block
        assert '"received_inferred": a["questions_received_inferred"]' in block
        assert '"user_asked_total": a["meeting_questions_by_user"]' in block


@pytest.mark.parametrize("mode", MODE_PRECEDENCE)
def test_no_mode_name_describes_a_person(mode):
    """Ship the count, never the cause. Every mode names what happened to
    an ITEM. A vocabulary that grew a word about a human being would put
    a stored trait about a named colleague into a commercial engagement,
    which is a defamation shaped object, not a memory."""
    assert not any(
        w in mode for w in ("unreliable", "flaky", "avoid", "evasive", "lazy")
    )
