"""The most inferentially dangerous surface in the People object.

Two failures these tests exist to prevent, and both are failures of
SOUNDING right rather than of arithmetic.

The first is the non sequitur with real numbers in it. The design's move
cards attach a "why it works" backed by observation counts, and its own
example ("he acted on 9 of 11 process changes he proposed himself")
needs proposal AUTHORSHIP, which ShoulderSurf established nothing on
either side records. The tempting move is to keep the sentence shape and
attach the counts we DO have, which yields "a calibrated question works
on him, because 23 of his 46 items went quiet". Real counts, and a
sentence that follows from none of them.

The second is the script that quotes a statistic at a colleague. "You
have gone quiet on 23 of your 46 open items" is accurate, checkable, and
would end a working relationship. The counts are why the card exists.
They are not the words.
"""

import pytest

from contextquilt.services.working_with import (
    GENERAL,
    MAX_MOVES,
    MIN_EVIDENCE,
    OBSERVED,
    TECHNIQUE_FOR,
    TECHNIQUES,
    Move,
    claims_it_works_on_them,
    move_defect,
    rank_moves,
    served_move,
    your_half,
)


def move(key, num=6, den=12, meetings=8, ids=("p1", "p2")):
    return Move(key, num, den, f"subject for {key}", meetings, ids)


# --------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------

def test_a_move_needs_evidence_under_it():
    assert not move("went_quiet", 2, MIN_EVIDENCE - 1).qualifies()
    assert move("went_quiet", 2, MIN_EVIDENCE).qualifies()


def test_a_move_with_a_zero_numerator_is_not_a_situation():
    """A denominator alone says the measure applied, not that anything
    happened. Nothing to raise means nothing to say."""
    assert not move("went_quiet", 0, 20).qualifies()


def test_an_unmapped_situation_produces_no_move():
    """The mapping is the authority. A situation nobody assigned a
    technique to must not fall through to a default one: a default is a
    model picking, moved into code and made invisible."""
    assert not move("something_new").qualifies()


def test_every_mapped_situation_names_a_real_technique():
    for situation, key in TECHNIQUE_FOR.items():
        assert key in TECHNIQUES, situation


# --------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------

def test_moves_rank_on_evidence_not_on_severity():
    """Ranking on how bad it looks puts the flimsiest, most alarming
    thing at the top of a screen whose job is to be actionable."""
    thin_but_awful = move("handed_back", num=5, den=5)      # 100 percent
    solid_but_mild = move("went_quiet", num=6, den=40)      # 15 percent
    ranked = rank_moves([thin_but_awful, solid_but_mild])
    assert ranked[0].situation_key == "went_quiet"


def test_one_move_per_technique():
    """went_quiet and restated both map to the calibrated question, and a
    person can qualify on both. Two of them on one screen is one idea
    printed twice, which the fixed mapping makes easy to do by accident."""
    ranked = rank_moves([move("went_quiet", den=40), move("restated", den=30)])
    assert len(ranked) == 1
    assert {m.technique.key for m in ranked} == {"calibrated_question"}


def test_the_screen_is_capped():
    ranked = rank_moves([
        move("went_quiet", den=40), move("closed_late", den=30),
        move("handed_back", den=20), move("speaking_turns", den=10),
    ])
    assert len(ranked) <= MAX_MOVES


def test_ranking_is_stable_across_identical_runs():
    """A screen that reshuffles between cycles reads as the system
    changing its mind."""
    candidates = [move("closed_late", den=20), move("handed_back", den=20)]
    assert [m.situation_key for m in rank_moves(candidates)] == \
           [m.situation_key for m in rank_moves(list(candidates))]


def test_nothing_qualifying_gives_an_empty_screen_not_a_filler_move():
    assert rank_moves([move("went_quiet", 1, 2)]) == []


# --------------------------------------------------------------------
# The observed / general seam
# --------------------------------------------------------------------

def test_the_situation_is_observed_and_the_technique_is_not():
    """The seam that stops this being a personality assessment. A client
    cannot infer it, so it ships as a field."""
    served = served_move(move("went_quiet"), 1, "Say this.",
                         "Hand over the method.", "When items carry over")
    assert served["basis"] == GENERAL
    assert served["situation"]["basis"] == OBSERVED


def test_no_field_can_hold_why_it_works_on_this_person():
    """Nothing observed could fill such a field, so it must not exist.
    A served field invites a value."""
    served = served_move(move("went_quiet"), 1, "Say this.",
                         "Hand over the method.", "When items carry over")
    assert served["why"] == TECHNIQUES["calibrated_question"].why_situation
    assert not any("person" in k or "fit" in k for k in served)


def test_the_situation_carries_its_receipts():
    served = served_move(move("went_quiet"), 1, "Say this.", "Hand over.", "When")
    assert served["situation"]["patch_ids"] == ["p1", "p2"]
    assert served["situation"]["numerator"] == 6


@pytest.mark.parametrize("phrase", [
    "This works well on him, so lead with it.",
    "He responds well to a headline first.",
    "She prefers the recommendation up front.",
    "They tend to respond to a direct ask.",
])
def test_general_practice_dressed_as_personal_knowledge_is_caught(phrase):
    """`basis` says GENERAL on the wire and a reader believes the
    SENTENCE, not the field."""
    assert claims_it_works_on_them(phrase)


@pytest.mark.parametrize("phrase", [
    "Naming the objection first buys the rest of the sentence a hearing.",
    "A how question hands the problem over rather than the request.",
])
def test_a_claim_about_the_practice_itself_is_fine(phrase):
    assert not claims_it_works_on_them(phrase)


# --------------------------------------------------------------------
# The script
# --------------------------------------------------------------------

def test_a_script_may_not_quote_a_statistic_at_a_colleague():
    """"You have gone quiet on 23 of your 46 open items" is accurate,
    checkable, and would end a working relationship."""
    assert move_defect(
        "When items carry over", "Hand over the method",
        "You have gone quiet on 23 of your 46 open items.",
    ) == "script_quotes_a_number"


def test_a_headline_may_carry_a_count_even_though_a_script_may_not():
    """The distinction IS the rule, so it gets its own test. Without
    this, someone bans numbers everywhere and the card loses the thing
    that made it checkable."""
    assert move_defect(
        "When items carry over",
        "6 of the last 12 went unmentioned",
        "How do you want me to raise things that carry over?",
    ) is None


def test_a_clean_move_passes():
    assert move_defect(
        "Opening your update",
        "Name the cost of your own ask before he has to",
        "I know we are tight on time. One blocker, one decision.",
    ) is None


def test_a_dash_is_a_defect_here_too():
    assert move_defect(
        "Opening", "Name the cost first",
        "I know we are tight — one blocker, one decision.",
    ) == "dash_punctuation"


def test_an_over_long_script_is_rejected_whole():
    assert move_defect("Opening", "Name the cost", "x" * 400) == "script_too_long"


# --------------------------------------------------------------------
# your_half
# --------------------------------------------------------------------

def test_your_half_serves_both_sides_as_integers():
    half = your_half(412, 96, 14, 2, meetings=8)
    keys = {s["key"] for s in half["stats"]}
    assert keys == {"speaking_turns", "questions_explicit"}
    assert half["basis"] == OBSERVED
    for stat in half["stats"]:
        assert isinstance(stat["value"], int)
        assert isinstance(stat["counterpart_value"], int)


def test_an_unknown_turn_count_is_not_served_as_a_zero():
    """NULL is unknown, never zero. Migration 34 is explicit that there
    is no backfill because the transcripts are gone, so a null rendered
    as a zero says somebody sat silent through eight meetings, which is a
    claim about a person built out of a missing column."""
    half = your_half(None, 96, 14, 2, meetings=8)
    assert {s["key"] for s in half["stats"]} == {"questions_explicit"}


def test_both_sides_unknown_serves_nothing_rather_than_an_empty_card():
    assert your_half(None, None, None, None, meetings=8) is None


def test_the_question_labels_say_what_was_actually_counted():
    """Migration 37's explicit column counts a vocative at a sentence
    edge, not every question. "Questions you asked them" would assert
    more than was observed; doc 16 5.13."""
    half = your_half(412, 96, 14, 2, meetings=8)
    stat = [s for s in half["stats"] if s["key"] == "questions_explicit"][0]
    assert "by name" in stat["label"]
    assert "by name" in stat["counterpart_label"]
