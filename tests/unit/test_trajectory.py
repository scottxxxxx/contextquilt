"""A change has to beat the person's own past to exist, and it may not
put a calendar on a system that holds no dates.

Two failures this file exists to prevent, both caught in review before a
line of either shipped.

The first is arithmetic nobody performed. The design asked for "74% to
41%, about 3% a week". Two pre-divided ratios and a regression slope over
them, none of which any measurement here produces.

The second is worse because it looked sourceable. A weekly sparkline was
DESIGNED AND WRITTEN before ShoulderSurf pointed out that CQ persists no
meeting date at all: one arrives at ingest, is spent resolving relative
deadlines, and is dropped (worker.py, `_process_meeting`). Every
surviving timestamp is an INGEST clock, so a weekly series would have
been a chart of when the importer ran, drawing a cliff wherever a bulk
import happened. The unit is meetings now, and `states_elapsed_time` is
the invariant rather than a line in a prompt.
"""

import pytest

from contextquilt.services.trajectory import (
    MEASURES,
    MIN_GAP_POINTS,
    MIN_INSTANCES_FOR_WORSE,
    MIN_SPAN_MEETINGS,
    MIN_WINDOW_DENOMINATOR,
    MIN_WINDOW_MEETINGS,
    Window,
    allowed_numbers,
    best_change,
    change_for_measure,
    grades_a_neutral_measure,
    meeting_series,
    parse_trajectory_response,
    served_trajectory,
    states_elapsed_time,
)


def mtgs(prefix, n):
    return [f"{prefix}{i}" for i in range(n)]


def window(num, den, prefix="m", meetings=8):
    return Window(num, den, mtgs(prefix, meetings))


def qualifying(measure="closed_late"):
    """A change that clears every gate, so a test can break exactly one."""
    return measure, window(2, 12, "a"), window(8, 11, "b")


# --------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------

def test_a_real_change_qualifies():
    key, earlier, recent = qualifying()
    found = change_for_measure(key, earlier, recent)
    assert found is not None
    assert found["direction"] == "worse"
    assert found["movement"] == "up"


def test_a_thin_earlier_window_disqualifies():
    """The weaker window bounds the claim.

    A change measured from two items against three is not a change, it is
    two small numbers, and the gate has to apply to BOTH halves: the
    roster lens only ever had one window to guard.
    """
    key, _, recent = qualifying()
    thin = window(1, MIN_WINDOW_DENOMINATOR - 1, "a")
    assert change_for_measure(key, thin, recent) is None


def test_a_thin_recent_window_disqualifies():
    key, earlier, _ = qualifying()
    thin = window(3, MIN_WINDOW_DENOMINATOR - 1, "b")
    assert change_for_measure(key, earlier, thin) is None


def test_a_small_gap_is_not_a_change():
    """Being slightly different from yourself is being yourself."""
    key = "closed_late"
    earlier = window(5, 10, "a")
    recent = window(5, 10, "b")
    assert change_for_measure(key, earlier, recent) is None


def test_a_gap_just_over_the_floor_qualifies():
    """Proves the previous test fails for the reason it claims."""
    key = "closed_late"
    earlier = window(3, 10, "a")   # 30 points
    recent = window(6, 10, "b")    # 60 points, gap 30
    found = change_for_measure(key, earlier, recent)
    assert found is not None
    assert abs(found["gap_points"]) >= MIN_GAP_POINTS


def test_one_instance_cannot_carry_an_unflattering_claim():
    """A pattern needs instances, not merely a rate.

    The roster lens shipped "hands work back more often than others" off
    ONE occurrence. Same rule, applied to the window the claim is about.
    """
    key = "closed_late"
    earlier = window(0, 12, "a")          # 0 points
    recent = Window(1, 5, mtgs("b", 4))   # 20 points, ONE instance
    # The numbers are pinned so this test reaches the gate it names.
    # Its first draft used 1 of 6, which is 17 points, and was being
    # stopped by the GAP floor two gates earlier: deleting the instances
    # gate entirely left it green. A sabotage run caught that, which is
    # the only way it could have been caught, and the assertions below
    # are what stop it drifting back.
    assert recent.numerator < MIN_INSTANCES_FOR_WORSE
    assert abs(recent.rate_points - earlier.rate_points) >= MIN_GAP_POINTS
    assert recent.meetings >= MIN_WINDOW_MEETINGS
    assert change_for_measure(key, earlier, recent) is None


def test_one_instance_is_fine_in_the_flattering_direction():
    """A low numerator is the EVIDENCE when the claim is that it stopped.

    Only the unflattering direction is gated, and this test is what stops
    somebody "simplifying" the gate into both directions.
    """
    key = "closed_late"
    earlier = window(9, 12, "a")
    recent = Window(1, 11, mtgs("b", 8))
    found = change_for_measure(key, earlier, recent)
    assert found is not None
    assert found["direction"] == "better"


def test_too_few_meetings_in_a_window_disqualifies():
    key = "closed_late"
    earlier = Window(2, 12, mtgs("a", MIN_WINDOW_MEETINGS - 1))
    recent = window(8, 11, "b")
    assert change_for_measure(key, earlier, recent) is None


def test_too_short_a_span_disqualifies():
    """A trajectory is a claim about a stretch, so it needs one."""
    key = "closed_late"
    earlier = Window(2, 12, mtgs("a", 3))
    recent = Window(8, 11, mtgs("b", 3))
    assert (earlier.meetings + recent.meetings) < MIN_SPAN_MEETINGS
    assert change_for_measure(key, earlier, recent) is None


def test_span_counts_distinct_meetings_not_the_sum():
    """Overlapping windows must not inflate the span.

    Six shared meetings: SUMMED they are 12 and would clear the floor of
    8, UNIONED they are 6 and do not. A caller bug should fail the gate
    rather than be laundered by it. The numbers are chosen so the two
    arithmetics disagree; at eight shared meetings both happen to pass
    and the test would prove nothing.
    """
    key = "closed_late"
    shared = mtgs("a", 6)
    earlier = Window(2, 12, shared)
    recent = Window(8, 11, shared)
    assert earlier.meetings + recent.meetings >= MIN_SPAN_MEETINGS
    assert len(set(shared)) < MIN_SPAN_MEETINGS
    assert change_for_measure(key, earlier, recent) is None


def test_meetings_is_derived_from_the_ids_it_counts():
    """The count and the receipts cannot disagree, structurally.

    SS's finding: "19 observations across 14 meetings" printed one number
    while holding another. A count computed from the very list it counts
    cannot do that, and duplicates collapse.
    """
    w = Window(3, 9, ["m1", "m2", "m2", "m3"])
    assert w.meetings == 3
    assert w.as_dict()["meetings"] == 3


# --------------------------------------------------------------------
# Neutral measures stay neutral
# --------------------------------------------------------------------

def test_a_neutral_measure_gets_a_movement_not_a_verdict():
    """Neither end of "speaks more" is good or bad, and the compass spec
    says so explicitly. A direction of "worse" here would be a judgement
    nothing observed."""
    earlier = window(214, 8, "a")
    recent = window(96, 8, "b")
    found = change_for_measure("speaking_turns", earlier, recent)
    assert found is not None
    assert found["direction"] == "down"
    assert found["direction"] not in ("worse", "better")


def test_a_neutral_measure_skips_the_instances_gate():
    """That gate exists to stop an unflattering pattern claim off one
    event. A neutral measure makes no such claim, so applying it would
    silently drop honest cards."""
    earlier = window(40, 8, "a")
    recent = Window(1, 8, mtgs("b", 8))
    assert change_for_measure("speaking_turns", earlier, recent) is not None


@pytest.mark.parametrize("phrase", [
    "his participation has declined",
    "follow-up has deteriorated",
    "he seems to be checked out",
    "engagement has improved",
    "things have slipped",
])
def test_grading_words_are_caught(phrase):
    assert grades_a_neutral_measure(phrase)


@pytest.mark.parametrize("phrase", [
    "he took fewer turns across your last 8 meetings",
    "the count moved from 214 to 96",
    "meetings are running with less back and forth",
])
def test_plain_movement_is_not_grading(phrase):
    assert not grades_a_neutral_measure(phrase)


# --------------------------------------------------------------------
# The calendar ban
# --------------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "over the past eleven weeks",
    "since the summer",
    "in the last three months",
    "since June",
    "lately he has been closing later",
    "over the last few days",
])
def test_elapsed_time_is_caught(phrase):
    """CQ holds no meeting date, so every one of these is invented."""
    assert states_elapsed_time(phrase)


def test_elapsed_time_is_caught_without_a_digit():
    """The whole point of a separate check.

    "since the summer" carries no number, so an invented-number check
    cannot see it. The UNIT is the defect, not the figure.
    """
    assert states_elapsed_time("since the summer")
    assert not any(c.isdigit() for c in "since the summer")


@pytest.mark.parametrize("phrase", [
    "across your last 8 meetings together",
    "in the 8 meetings before those",
    "closed 7 of 11 dated items after the date",
])
def test_counting_in_meetings_is_allowed(phrase):
    assert not states_elapsed_time(phrase)


# --------------------------------------------------------------------
# The series
# --------------------------------------------------------------------

def test_series_is_ordered_pairs_keyed_on_origin():
    series = meeting_series([
        {"origin_id": "m1", "numerator": 0, "denominator": 2},
        {"origin_id": "m2", "numerator": 1, "denominator": 1},
    ])
    assert [p["origin_id"] for p in series] == ["m1", "m2"]
    assert [p["sequence"] for p in series] == [0, 1]
    assert all(isinstance(p["numerator"], int) for p in series)


def test_series_keeps_empty_meetings_rather_than_dropping_them():
    """A gap is data. Dropping a zero-denominator meeting draws a
    straight line through a period where nothing was measured."""
    series = meeting_series([
        {"origin_id": "m1", "numerator": 0, "denominator": 0},
        {"origin_id": "m2", "numerator": 1, "denominator": 1},
    ])
    assert len(series) == 2
    assert series[0]["denominator"] == 0


def test_series_carries_no_dates_at_all():
    """The regression guard. If a date key ever reappears here, someone
    has re-derived a time axis from ingest clocks."""
    series = meeting_series([{"origin_id": "m1", "numerator": 1,
                             "denominator": 2, "week_start": "2026-06-01"}])
    assert set(series[0]) == {"origin_id", "sequence", "numerator", "denominator"}


def test_series_carries_no_floats():
    """GP's proxy replaces non-finite floats with null and never visits
    ints. A float series has a silent null-shaped failure mode that an
    integer series does not have."""
    series = meeting_series([{"origin_id": "m1", "numerator": 1, "denominator": 2}])
    for value in series[0].values():
        assert not isinstance(value, float)


def test_a_row_with_no_origin_is_dropped_not_guessed():
    assert meeting_series([{"numerator": 1, "denominator": 2}]) == []


# --------------------------------------------------------------------
# What the writer may say
# --------------------------------------------------------------------

def test_allowed_numbers_holds_no_unit_of_time():
    key, earlier, recent = qualifying()
    facts = served_trajectory(change_for_measure(key, earlier, recent), "Suresh")
    permitted = allowed_numbers(facts)
    assert 2 in permitted and 12 in permitted
    assert 8 in permitted and 11 in permitted
    assert facts["span_meetings"] in permitted
    assert "span_weeks" not in facts


def test_served_shape_carries_supersedes_as_a_list():
    """Empty is not absent. A client renders differently on each."""
    key, earlier, recent = qualifying()
    chosen = change_for_measure(key, earlier, recent)
    assert served_trajectory(chosen, "Suresh")["supersedes"] == []
    with_sup = served_trajectory(chosen, "Suresh",
                                 supersedes=["how_they_follow_through"])
    assert with_sup["supersedes"] == ["how_they_follow_through"]


def good_answer(**over):
    base = {
        "skip": False,
        "text": ("Closed 8 of his last 11 dated items after the date, "
                 "against 2 of 12 in the 8 meetings before that."),
        "narrative": ("The shift shows only in aggregate: any single "
                      "meeting looks normal and the items still close. "
                      "What moved is when."),
        "do": "Ask which open items still have a date behind them that holds.",
    }
    base.update(over)
    return base


def facts_for_parse():
    key, earlier, recent = qualifying()
    return served_trajectory(change_for_measure(key, earlier, recent), "Suresh")


def test_a_good_answer_parses():
    facts = facts_for_parse()
    out = parse_trajectory_response(
        good_answer(), allowed_numbers(facts), "Suresh", facts=facts)
    assert out is not None
    assert out["lens"] == "how_theyre_changing"
    assert out["narrative"]


def test_a_claim_stating_only_the_recent_half_is_rejected():
    """A change stated without its starting point is an accusation the
    reader cannot check. ShoulderSurf renders the claim verbatim and
    correctly refuses to police it, so the guarantee lives here."""
    facts = facts_for_parse()
    defects = []
    out = parse_trajectory_response(
        good_answer(text="Closed 8 of his last 11 dated items after the date."),
        allowed_numbers(facts), "Suresh", defects=defects, facts=facts)
    assert out is None
    assert "one_window_only" in defects


def test_an_invented_percentage_is_rejected():
    """The design's own headline number, refused."""
    facts = facts_for_parse()
    defects = []
    out = parse_trajectory_response(
        good_answer(text="Follow-through fell from 74 percent to 41 percent."),
        allowed_numbers(facts), "Suresh", defects=defects, facts=facts)
    assert out is None
    assert "invented_number" in defects


def test_a_calendar_claim_is_rejected():
    facts = facts_for_parse()
    defects = []
    out = parse_trajectory_response(
        good_answer(narrative="Over the past eleven weeks the pattern held."),
        allowed_numbers(facts), "Suresh", defects=defects, facts=facts)
    assert out is None
    assert "stated_elapsed_time" in defects


def test_grading_a_neutral_measure_is_rejected():
    earlier, recent = window(214, 8, "a"), window(96, 8, "b")
    facts = served_trajectory(
        change_for_measure("speaking_turns", earlier, recent), "Suresh")
    defects = []
    out = parse_trajectory_response(
        good_answer(
            text="Took 96 turns across your last 8 meetings, against 214 before.",
            narrative="His participation has declined across the run.",
            do="Ask what would make the meeting worth more of his time."),
        allowed_numbers(facts), "Suresh", defects=defects, facts=facts)
    assert out is None
    assert "graded_a_neutral_measure" in defects


def test_the_same_grading_word_is_allowed_on_a_valenced_measure():
    """Proves the previous test is about VALENCE, not about the word.

    Without this, someone could satisfy the neutral test by banning the
    word everywhere, which would silently gag the one measure entitled
    to say it.
    """
    facts = facts_for_parse()
    assert facts["valence"] == "unflattering_up"
    out = parse_trajectory_response(
        good_answer(narrative="Closing has got worse across the run, "
                              "though every single meeting looks normal."),
        allowed_numbers(facts), "Suresh", facts=facts)
    assert out is not None


def test_a_dash_is_still_a_defect_here():
    facts = facts_for_parse()
    defects = []
    out = parse_trajectory_response(
        good_answer(text="Closed 8 of 11 after the date — against 2 of 12 before."),
        allowed_numbers(facts), "Suresh", defects=defects, facts=facts)
    assert out is None


def test_a_missing_narrative_is_not_silently_shipped():
    facts = facts_for_parse()
    out = parse_trajectory_response(
        {"skip": False, "text": good_answer()["text"], "do": good_answer()["do"]},
        allowed_numbers(facts), "Suresh", facts=facts)
    assert out is None


def test_skip_is_honoured():
    assert parse_trajectory_response({"skip": True, "text": "", "do": ""}) is None


def test_best_change_picks_the_largest_gap_and_ties_stably():
    windows = {
        "closed_late": (window(2, 12, "a"), window(8, 11, "b")),
        "speaking_turns": (window(50, 8, "a"), window(48, 8, "b")),
    }
    chosen = best_change(windows)
    assert chosen["measure_key"] == "closed_late"
    assert best_change(windows)["measure_key"] == chosen["measure_key"]


def test_no_qualifying_measure_returns_none_rather_than_a_weak_card():
    windows = {"closed_late": (window(5, 10, "a"), window(5, 10, "b"))}
    assert best_change(windows) is None


def test_every_measure_declares_a_valence():
    """A measure added without one would default to being graded."""
    for key, measure in MEASURES.items():
        assert measure.valence in ("unflattering_up", "neutral"), key


# --------------------------------------------------------------------
# Proportions and rates are different animals
#
# The defect these prevent nearly shipped, and it was found from two
# sides at once. The model-selection eval showed BOTH writers refusing
# every rate case as "mathematically impossible", because the prompt said
# "214 out of 8"; the eval was measuring this bug and reporting it as
# model quality. ShoulderSurf hit the same conflation the same afternoon
# from the rendering end: a rate pinned flat against the top of a 0..1
# axis and the chart said nothing. Neither side could see the other's
# half, which is why it took both.
# --------------------------------------------------------------------

from contextquilt.services.trajectory import (           # noqa: E402
    MIN_RATE_RELATIVE_CHANGE,
    build_trajectory_content,
)


def test_a_rate_is_not_phrased_as_a_proportion():
    """"214 out of 8" is an impossible sentence and a model is right to
    refuse it. The pair kind decides the phrasing, not the caller."""
    chosen = change_for_measure("speaking_turns", window(214, 8, "a"),
                                window(96, 8, "b"))
    content = build_trajectory_content("Suresh", served_trajectory(chosen, "Suresh"))
    assert "214 speaking turns across 8 meetings" in content
    # Scoped to the DATA lines. The prompt's own prohibition names the
    # forbidden phrasing ("must never be written as N out of M"), so a
    # whole-content assertion trips on the guardrail it is checking for.
    stretch = [l for l in content.split("\n") if "stretch" in l]
    assert stretch and not any("out of" in l for l in stretch)


def test_a_proportion_is_still_phrased_as_one():
    chosen = change_for_measure("closed_late", window(2, 12, "a"), window(8, 11, "b"))
    content = build_trajectory_content("Suresh", served_trajectory(chosen, "Suresh"))
    assert "2 out of 12" in content


def test_the_rate_gate_actually_bites():
    """The proportion gate is VACUOUS on a rate and this is the proof.

    216 turns over 8 meetings is 2700 "percentage points" against 2400,
    a gap of 300, so the 20 point floor clears trivially and stops
    existing. As a relative change it is 11 percent and is not a finding.
    """
    earlier, recent = window(216, 8, "a"), window(192, 8, "b")
    assert abs(recent.rate_points - earlier.rate_points) >= MIN_GAP_POINTS
    assert abs(recent.rate_points - earlier.rate_points) / earlier.rate_points \
        < MIN_RATE_RELATIVE_CHANGE
    assert change_for_measure("speaking_turns", earlier, recent) is None


def test_a_large_relative_rate_change_qualifies():
    """Proves the previous test fails for the reason it claims."""
    found = change_for_measure("speaking_turns", window(214, 8, "a"),
                               window(96, 8, "b"))
    assert found is not None
    assert abs(found["relative_change"]) >= MIN_RATE_RELATIVE_CHANGE


def test_a_rate_with_no_earlier_baseline_is_declined():
    """"Up from nothing" is a different claim and this lens does not make
    it. It is also a division by zero waiting to happen."""
    assert change_for_measure("speaking_turns", window(0, 8, "a"),
                              window(96, 8, "b")) is None


def test_pair_kind_is_served_rather_than_inferred():
    """A client that guesses draws a rate on a 0..1 axis."""
    prop = served_trajectory(
        change_for_measure("closed_late", window(2, 12, "a"), window(8, 11, "b")), "X")
    rate = served_trajectory(
        change_for_measure("speaking_turns", window(214, 8, "a"), window(96, 8, "b")), "X")
    assert prop["pair_kind"] == "proportion"
    assert rate["pair_kind"] == "rate"
    assert rate["counted_noun"] == "speaking turns"


def test_ranking_compares_kinds_on_relative_distance():
    """Raw gap_points would hand every contest to whichever measure has
    the bigger units, which is a fact about turns versus items and not
    about the person. Here the proportion is the larger RELATIVE change
    and must win despite a far smaller raw gap.
    """
    windows = {
        # 17 points to 73: a 4.3x relative change, raw gap 56.
        "closed_late": (window(2, 12, "a"), window(8, 11, "b")),
        # 2675 to 1738: a 0.35 relative change, raw gap 937.
        "speaking_turns": (window(214, 8, "a"), window(139, 8, "b")),
    }
    chosen = best_change(windows)
    assert chosen is not None
    assert chosen["measure_key"] == "closed_late"


def test_every_measure_declares_a_pair_kind_and_a_noun():
    for key, measure in MEASURES.items():
        assert measure.pair_kind in ("proportion", "rate"), key
        assert measure.counted_noun, key


@pytest.mark.parametrize("phrase", [
    "across your last 8 meetings this quarter",
    "in the 8 meetings before those, back in June",
    "over your last 6 meetings together, about a month ago",
])
def test_a_legitimate_meeting_count_cannot_smuggle_a_calendar(phrase):
    """ShoulderSurf's suggested shape, and worth pinning explicitly.

    "across your last 8 meetings" is a legitimate frame and the tail is
    not. The risk they named is a check that whitelists the known-good
    frame and lets the trailing "this quarter" ride along. This one does
    not whitelist anything: it looks for the forbidden UNIT wherever it
    appears, so a valid prefix buys the rest of the sentence nothing.
    """
    assert states_elapsed_time(phrase)


# --------------------------------------------------------------------
# Splitting a relationship into two stretches
# --------------------------------------------------------------------

from contextquilt.services.trajectory import (          # noqa: E402
    MAX_WINDOW_MEETINGS,
    split_meetings,
)


def test_split_gives_two_adjacent_disjoint_stretches_oldest_first():
    ids = [f"m{i}" for i in range(20, 0, -1)]   # newest first: m20..m1
    earlier, recent = split_meetings(ids)
    assert len(recent) == MAX_WINDOW_MEETINGS
    assert len(earlier) == MAX_WINDOW_MEETINGS
    assert not set(earlier) & set(recent)
    # oldest first within each, and earlier sits immediately before recent
    assert recent == [f"m{i}" for i in range(13, 21)]
    assert earlier == [f"m{i}" for i in range(5, 13)]


def test_split_declines_a_relationship_too_short_to_split():
    assert split_meetings([f"m{i}" for i in range(5)]) is None


def test_split_never_returns_a_lopsided_pair():
    """Eleven meetings could give 8 and 3. It gives 5 and 5 instead: two
    windows of different weight are not a before and an after, and the
    smaller one would carry the whole claim."""
    earlier, recent = split_meetings([f"m{i}" for i in range(11)])
    assert len(earlier) == len(recent) == 5


def test_split_caps_the_window_so_drift_is_not_averaged_away():
    earlier, recent = split_meetings([f"m{i}" for i in range(60)])
    assert len(earlier) == len(recent) == MAX_WINDOW_MEETINGS


def test_split_dedupes_repeated_meeting_ids():
    """One person can hold several rows for a meeting. A repeated id
    would inflate a window with no extra evidence behind it."""
    ids = ["m9", "m9", "m8", "m7", "m6", "m5", "m5", "m4", "m3", "m2", "m1"]
    earlier, recent = split_meetings(ids)
    assert len(set(earlier) | set(recent)) == len(earlier) + len(recent)


# --------------------------------------------------------------------
# What the mechanical checks used to miss
#
# Both of these were found by the writer-selection eval rather than by
# review, and both had SHIPPED on the smaller model, because every number
# in them is permitted and every word is allowed. They are the residue
# after "the model may not count": a model that cannot invent a number
# can still attach a real one to the wrong noun.
# --------------------------------------------------------------------

from contextquilt.services.trajectory import (          # noqa: E402
    conflates_the_denominator,
    opens_with_a_preamble,
)


def test_a_proportion_denominator_may_not_be_called_meetings():
    """Measured live: "Closes work after its date in 11 of the last 11
    meetings, against 1 of 30 across the earlier 12 meetings." The
    denominator counts ITEMS. Both 11s are permitted numbers, so the
    invented-number check cannot see it, and the sentence is false."""
    assert conflates_the_denominator(
        "Closes work after its date in 11 of the last 11 meetings.", "proportion")


def test_a_rate_denominator_IS_meetings_and_must_not_be_rejected():
    """The guard that stops this being 'fixed' into rejecting the honest
    rate phrasing, where the denominator really is a meeting count."""
    assert not conflates_the_denominator(
        "Took 96 turns across your last 8 meetings, against 214 before.", "rate")


def test_a_proportion_may_still_reference_meetings_legitimately():
    """"across the 8 meetings before that" is a span, not a denominator."""
    assert not conflates_the_denominator(
        "Closed 7 of 11 dated items late, against 3 of 12 across the 8 "
        "meetings before that.", "proportion")


@pytest.mark.parametrize("do", [
    "In your next meeting, ask which of the open items have a hard deadline.",
    "Consider asking which dates still hold.",
    "Next time, confirm the runbook handover date.",
    "When you next speak, ask what changed.",
])
def test_do_line_preambles_are_rejected(do):
    assert opens_with_a_preamble(do)


@pytest.mark.parametrize("do", [
    "Ask which of the open items still have a date behind them that holds.",
    "Confirm the due date for the runbook handover.",
    "Leave a beat after your update and ask her directly for the read.",
])
def test_a_verb_first_do_line_passes(do):
    assert not opens_with_a_preamble(do)


def test_both_new_defects_reject_a_whole_card():
    facts = facts_for_parse()
    for over, expect in [
        ({"text": "Closes late in 8 of the last 11 meetings, against 2 of 12 before."},
         "denominator_wrong_unit"),
        ({"do": "In your next meeting, ask which items still have a date."},
         "do_line_preamble"),
    ]:
        defects = []
        assert parse_trajectory_response(
            good_answer(**over), allowed_numbers(facts), "Suresh",
            defects=defects, facts=facts) is None
        assert expect in defects
