"""Closure mode: the classifier, at its boundaries.

Two of these matter more than the rest.

The first is `re_dated` against `silently_dropped`, because they look
identical in a list of open overdue items and mean opposite things. A
re-dated item is being managed against a calendar. A silently dropped one
has fallen out of the conversation, and the test for that is meetings
with THAT PERSON, never elapsed days: a fortnight of silence while
neither of them was in a room together is not a drop.

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
    SILENTLY_DROPPED,
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
        assert got["mode"] == SILENTLY_DROPPED
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
        assert got["mode"] == SILENTLY_DROPPED
        assert got["modes"] == [SILENTLY_DROPPED, RE_DATED]

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
        assert SILENTLY_DROPPED in got["modes"]

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
        assert got[0]["mode"] == SILENTLY_DROPPED

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

    def test_silently_dropped_is_pulled_out_by_name(self):
        items = classify_items(
            [item(patch_id="a", deadline_date="2026-06-01")],
            TODAY, appearances=met((7, 1), (8, 1)),
        )
        s = summarize(items)
        assert s["silently_dropped"] == 1
        assert s["by_mode"][SILENTLY_DROPPED] == 1


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
