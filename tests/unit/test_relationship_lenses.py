"""A card has to beat the roster to exist.

The failure this replaces: on 2026-08-16 three of the four people on
Scott's person pages carried "gates forward movement until dependencies
resolve" and "responds to concrete blockers", because a model asked to
characterise someone from a corpus of commitments and blockers describes
the corpus. Accuracy was never the problem. Being true of everybody was.
"""

import pytest

from contextquilt.services.relationship_lenses import (
    MIN_DENOMINATOR,
    MIN_GAP_POINTS,
    allowed_numbers,
    best_fact,
    facts_for_person,
    rank_facts,
    roster_baseline,
    served_facts,
)


def counts(**kw):
    base = dict(total_items=40, open_items=20, quiet_items=5,
                closed_items=20, closed_late=5, handed_back=2,
                restated=2, dated_items=20, re_dated=2,
                # items stated INSIDE the window: the proof the window
                # is visible at all. See MIN_RECENT_FOR_QUIET.
                recent_items=15)
    base.update(kw)
    return base


# --- the denominator floor -------------------------------------------

def test_a_fact_under_the_denominator_floor_is_not_emitted():
    """No rate on three items. A statistic on a tiny denominator is
    noise wearing a costume."""
    facts = facts_for_person(counts(closed_items=MIN_DENOMINATOR - 1))
    assert "closed_late" not in {f.key for f in facts}


def test_a_fact_on_the_floor_exactly_is_emitted():
    facts = facts_for_person(counts(closed_items=MIN_DENOMINATOR))
    assert "closed_late" in {f.key for f in facts}


def test_missing_counts_are_skipped_not_zeroed():
    """An absent count is 'not measured', which is not the same claim as
    zero and must never be served as one."""
    c = counts()
    del c["closed_late"]
    assert "closed_late" not in {f.key for f in facts_for_person(c)}


# --- the baseline ------------------------------------------------------

def test_baseline_is_pooled_not_the_mean_of_rates():
    """A person with six items and a person with sixty are not equal
    evidence about what normal looks like."""
    all_facts = {
        "big": facts_for_person(counts(closed_items=90, closed_late=9)),
        "small": facts_for_person(counts(closed_items=10, closed_late=9)),
        "third": facts_for_person(counts(closed_items=100, closed_late=10)),
    }
    base = roster_baseline(all_facts)["closed_late"]
    # pooled 28/200 = 14%; the mean of the three rates would be 40%.
    assert base["rate_points"] == 14


def test_a_key_measured_on_too_few_people_gets_no_baseline():
    all_facts = {
        "a": facts_for_person(counts()),
        "b": facts_for_person(counts()),
    }
    assert roster_baseline(all_facts) == {}


def test_leave_one_out_excludes_the_person_being_judged():
    """Comparing someone against a group containing themselves shrinks
    their own deviation, and on a small roster that hides the person the
    measure is loudest about."""
    all_facts = {
        "heavy": facts_for_person(counts(closed_items=90, closed_late=0)),
        "b": facts_for_person(counts(closed_items=20, closed_late=10)),
        "c": facts_for_person(counts(closed_items=20, closed_late=10)),
        "d": facts_for_person(counts(closed_items=20, closed_late=10)),
    }
    with_self = roster_baseline(all_facts)["closed_late"]["rate_points"]
    without = roster_baseline(all_facts, exclude="heavy")["closed_late"]["rate_points"]
    assert without > with_self
    assert without == 50


# --- the contrast ------------------------------------------------------

def test_a_person_at_the_roster_rate_gets_nothing():
    """The whole point. Accurate and true of everybody is worthless."""
    all_facts = {n: facts_for_person(counts()) for n in "abcd"}
    base = roster_baseline(all_facts, exclude="a")
    assert best_fact(counts(), base) is None


def test_the_worst_person_on_a_measure_everybody_is_bad_at_gets_nothing():
    """Being the worst is not the same as being unusual."""
    all_facts = {
        "a": facts_for_person(counts(closed_items=20, closed_late=18)),
        "b": facts_for_person(counts(closed_items=20, closed_late=17)),
        "c": facts_for_person(counts(closed_items=20, closed_late=17)),
        "d": facts_for_person(counts(closed_items=20, closed_late=17)),
    }
    base = roster_baseline(all_facts, exclude="a")
    chosen = best_fact(counts(closed_items=20, closed_late=18), base)
    assert chosen is None or chosen["fact"].key != "closed_late"


def test_being_unusually_good_earns_a_card_too():
    """The person who never misses is a finding, and it is the one the
    user is least likely to have noticed."""
    all_facts = {
        "star": facts_for_person(counts(closed_items=30, closed_late=1)),
        "b": facts_for_person(counts(closed_items=20, closed_late=10)),
        "c": facts_for_person(counts(closed_items=20, closed_late=10)),
        "d": facts_for_person(counts(closed_items=20, closed_late=10)),
    }
    base = roster_baseline(all_facts, exclude="star")
    chosen = best_fact(counts(closed_items=30, closed_late=1), base)
    assert chosen["fact"].key == "closed_late"
    assert chosen["direction"] == "better"


def test_the_most_unusual_fact_wins_not_the_first_one():
    all_facts = {
        "x": facts_for_person(counts(closed_items=20, closed_late=10,
                                     open_items=20, quiet_items=19, recent_items=8)),
        "b": facts_for_person(counts(closed_items=20, closed_late=9,
                                     open_items=20, quiet_items=2)),
        "c": facts_for_person(counts(closed_items=20, closed_late=9,
                                     open_items=20, quiet_items=2)),
        "d": facts_for_person(counts(closed_items=20, closed_late=9,
                                     open_items=20, quiet_items=2)),
    }
    base = roster_baseline(all_facts, exclude="x")
    chosen = best_fact(
        counts(closed_items=20, closed_late=10, open_items=20,
               quiet_items=19, recent_items=8),
        base,
    )
    assert chosen["fact"].key == "went_quiet"


def test_a_gap_just_under_the_threshold_does_not_ship():
    all_facts = {
        "a": facts_for_person(counts(closed_items=100, closed_late=20)),
        "b": facts_for_person(counts(closed_items=100, closed_late=20)),
        "c": facts_for_person(counts(closed_items=100, closed_late=20)),
    }
    base = roster_baseline(all_facts)
    near = counts(closed_items=100, closed_late=20 + MIN_GAP_POINTS - 1)
    ranked = rank_facts(facts_for_person(near), base)
    assert "closed_late" not in {r["fact"].key for r in ranked}


def test_ranking_is_deterministic_on_a_tie():
    """A browse-adjacent surface must not reshuffle between polls."""
    all_facts = {n: facts_for_person(counts()) for n in "abcd"}
    base = roster_baseline(all_facts)
    c = counts(closed_items=20, closed_late=20, open_items=20, quiet_items=20)
    first = [r["fact"].key for r in rank_facts(facts_for_person(c), base)]
    second = [r["fact"].key for r in rank_facts(facts_for_person(c), base)]
    assert first == second


# --- what reaches the wire --------------------------------------------

def test_served_facts_carry_integers_not_a_rate():
    """No pre-divided ratio: a served rate is a number whose denominator
    the reader cannot see, and it is the only thing here that could
    reach the gateway serializer as NaN."""
    all_facts = {
        "a": facts_for_person(counts(closed_items=22, closed_late=12)),
        "b": facts_for_person(counts(closed_items=40, closed_late=4)),
        "c": facts_for_person(counts(closed_items=40, closed_late=4)),
        "d": facts_for_person(counts(closed_items=40, closed_late=4)),
    }
    base = roster_baseline(all_facts, exclude="a")
    chosen = best_fact(counts(closed_items=22, closed_late=12), base)
    served = served_facts(chosen, "Pallavi")
    assert served["numerator"] == 12 and served["denominator"] == 22
    assert all(isinstance(v, int) for v in
               (served["numerator"], served["denominator"],
                served["roster_numerator"], served["roster_denominator"]))
    assert not any(isinstance(v, float) for v in served.values())


def test_the_roster_comparison_is_published_not_implied():
    """The contrast is what makes the claim non-obvious, so the reader
    gets to see the other half of it."""
    all_facts = {
        "a": facts_for_person(counts(closed_items=22, closed_late=12)),
        "b": facts_for_person(counts(closed_items=40, closed_late=4)),
        "c": facts_for_person(counts(closed_items=40, closed_late=4)),
        "d": facts_for_person(counts(closed_items=40, closed_late=4)),
    }
    base = roster_baseline(all_facts, exclude="a")
    served = served_facts(best_fact(counts(closed_items=22, closed_late=12), base),
                          "Pallavi")
    assert served["roster_denominator"] == 120
    assert served["roster_people"] == 3


def test_allowed_numbers_covers_both_halves_of_the_contrast():
    """The writer may only use numbers it was given, and the sentence
    needs the roster's pair as well as the person's."""
    facts = {"numerator": 12, "denominator": 22, "roster_numerator": 12,
             "roster_denominator": 120, "roster_people": 3}
    assert allowed_numbers(facts) == {12, 22, 120, 3}


# --- the live shape ----------------------------------------------------

def test_the_measured_production_roster_produces_four_different_stories():
    """Regression on the thing this was built for. These are the real
    2026-08-16 counts. Before this lens all four of these people carried
    the same sentence."""
    people = {
        "Sukumar": counts(recent_items=35, total_items=117, open_items=51, quiet_items=16,
                          closed_items=33, closed_late=1, handed_back=6,
                          restated=9, dated_items=33, re_dated=3),
        "Vijay": counts(recent_items=26, total_items=110, open_items=49, quiet_items=23,
                        closed_items=33, closed_late=7, handed_back=2,
                        restated=2, dated_items=33, re_dated=2),
        "Pallavi": counts(recent_items=30, total_items=81, open_items=41, quiet_items=11,
                          closed_items=22, closed_late=12, handed_back=5,
                          restated=5, dated_items=22, re_dated=3),
        "Srikanth": counts(recent_items=24, total_items=63, open_items=36, quiet_items=12,
                           closed_items=9, closed_late=1, handed_back=4,
                           restated=3, dated_items=9, re_dated=0),
        "Suresh": counts(recent_items=37, total_items=86, open_items=52, quiet_items=15,
                         closed_items=20, closed_late=7, handed_back=6,
                         restated=7, dated_items=20, re_dated=5),
    }
    all_facts = {n: facts_for_person(c) for n, c in people.items()}
    chosen = {}
    for name, c in people.items():
        base = roster_baseline(all_facts, exclude=name)
        pick = best_fact(c, base)
        chosen[name] = (pick["fact"].key, pick["direction"]) if pick else None

    # Four people, four DIFFERENT stories, where all four previously
    # carried one sentence about gating on dependencies.
    assert chosen["Sukumar"] == ("closed_late", "better")
    assert chosen["Vijay"] == ("went_quiet", "worse")
    assert chosen["Pallavi"] == ("closed_late", "worse")
    # Suresh lands on the re-date, which is the object the 2026-08-13
    # expert panel called the most valuable one in the corpus: the item
    # that never fails and never finishes, it just gets a new date. It
    # is invisible from inside any single meeting by construction.
    assert chosen["Suresh"] == ("re_dated", "worse")
    assert len({v[0] for v in chosen.values() if v}) == 3


# --- what the writer is allowed to say --------------------------------

from contextquilt.services.relationship_lenses import (  # noqa: E402
    LENS, build_stands_out_content, parse_stands_out_response,
)

FACTS = {
    "fact_key": "closed_late", "numerator": 12, "denominator": 22,
    "subject": "items that were closed after the date they were due",
    "direction": "worse", "roster_numerator": 20,
    "roster_denominator": 107, "roster_people": 3, "about_person": "Pallavi",
}


def _resp(text, do, skip=False):
    return {"skip": skip, "text": text, "do": do, "reason": "x"}


def test_a_good_claim_survives():
    got = parse_stands_out_response(
        _resp("12 of 22 closed items landed after their due date.",
              "Ask for the date she will actually hit."),
        allowed_numbers(FACTS), person_name="Pallavi",
    )
    assert got["lens"] == LENS


def test_an_invented_number_voids_the_card():
    """The enforcement half of 'never state a number the arithmetic did
    not produce'. The prompt is the hint; this is the invariant."""
    defects = []
    assert parse_stands_out_response(
        _resp("9 of 14 closed items landed late.", "Ask what is moving."),
        allowed_numbers(FACTS), person_name="Pallavi", defects=defects,
    ) is None
    assert defects == ["invented_number"]


def test_the_roster_numbers_are_usable_because_the_contrast_is_the_point():
    got = parse_stands_out_response(
        _resp("Closes late 12 times where others managed 20.",
              "Ask which open items have a real date."),
        allowed_numbers(FACTS), person_name="Pallavi",
    )
    assert got is not None


def test_a_character_verdict_voids_the_card():
    defects = []
    assert parse_stands_out_response(
        _resp("Unreliable on dates compared with everyone else.",
              "Ask for a date she will hit."),
        allowed_numbers(FACTS), person_name="Pallavi", defects=defects,
    ) is None
    assert defects == ["character_word"]


def test_a_dash_used_as_punctuation_voids_the_card():
    """Claims are quoted verbatim into other served surfaces where
    dashes are banned, and the next model copies the punctuation it
    reads, so scrubbing downstream is too late."""
    defects = []
    assert parse_stands_out_response(
        _resp("Closes late more often — far more than others.",
              "Ask for a real date."),
        allowed_numbers(FACTS), person_name="Pallavi", defects=defects,
    ) is None
    assert defects == ["claim_dash_punctuation"]


def test_a_genuine_hyphen_is_not_a_dash():
    got = parse_stands_out_response(
        _resp("Needs a follow-up on most items she closes.",
              "Ask for the on-time date, not the hoped-for one."),
        allowed_numbers(FACTS), person_name="Pallavi",
    )
    assert got is not None


def test_a_claim_opening_with_the_name_voids_the_card():
    assert parse_stands_out_response(
        _resp("Pallavi closes late more often than others.",
              "Ask for a real date."),
        allowed_numbers(FACTS), person_name="Pallavi",
    ) is None


def test_skip_is_honoured():
    assert parse_stands_out_response(_resp("", "", skip=True)) is None


def test_garbage_does_not_crash():
    assert parse_stands_out_response("not json at all") is None
    assert parse_stands_out_response(None) is None


def test_the_prompt_states_both_halves_of_the_comparison():
    """A fact computed but never contrasted still reads as generic, so
    the roster side has to reach the writer."""
    content = build_stands_out_content("Pallavi", FACTS, [{"text": "an item"}])
    assert "12 out of 22" in content
    assert "20 out of 107" in content
    assert "other 3 people" in content


def test_the_prompt_is_byte_stable():
    a = build_stands_out_content("Pallavi", FACTS, [{"text": "an item"}])
    b = build_stands_out_content("Pallavi", FACTS, [{"text": "an item"}])
    assert a == b


# --- ordering on the wire ---------------------------------------------

def test_the_lens_ships_its_own_display_order():
    """SS sorts by whether a lens is NAMED, not against a fixed list, so
    an order that carries meaning has to travel as a field rather than
    be inferred on their side."""
    import pathlib
    worker = pathlib.Path("src/worker.py").read_text()
    main = pathlib.Path("src/main.py").read_text()
    assert 'value["display_order"] = relationship_lenses.DISPLAY_ORDER' in worker
    assert '"display_order": iv.get("display_order")' in main


# --- the contrast cannot be dropped from the sentence ------------------

def test_a_claim_stating_only_this_persons_numbers_is_rejected():
    """A comparison that states one side of itself is an accusation.
    SS renders the claim verbatim and correctly refuses to police its
    contents, so the guarantee lives here."""
    defects = []
    assert parse_stands_out_response(
        _resp("12 of 22 closed items landed late.", "Ask for a real date."),
        allowed_numbers(FACTS), person_name="Pallavi", defects=defects,
        facts=FACTS,
    ) is None
    assert defects == ["contrast_omitted"]


def test_a_claim_carrying_both_halves_survives():
    got = parse_stands_out_response(
        _resp("Closes late 12 times where others managed 20.",
              "Ask for a real date."),
        allowed_numbers(FACTS), person_name="Pallavi", facts=FACTS,
    )
    assert got is not None


def test_a_claim_with_no_numbers_at_all_is_still_allowed():
    """Not every true sentence needs digits, and a claim that states
    neither side is not lopsided."""
    got = parse_stands_out_response(
        _resp("Closes late far more often than others you work with.",
              "Ask for a real date."),
        allowed_numbers(FACTS), person_name="Pallavi", facts=FACTS,
    )
    assert got is not None


# --- an absence needs a window you can see into ------------------------

def test_gone_quiet_needs_items_stated_inside_the_window():
    """Otherwise the fact degrades into 'these items are older than the
    window', which is true of any truncated corpus. Real cause: the
    August app-id split truncates the item scope at the flip while
    person_appearances is not scoped, so every surviving item predates
    the window and the rate goes 47% -> 96% with nothing changing about
    how the work went."""
    truncated = counts(open_items=24, quiet_items=23, recent_items=1)
    assert "went_quiet" not in {f.key for f in facts_for_person(truncated)}


def test_gone_quiet_is_emitted_when_the_window_is_visible():
    healthy = counts(open_items=49, quiet_items=23, recent_items=26)
    assert "went_quiet" in {f.key for f in facts_for_person(healthy)}


def test_a_missing_recent_count_suppresses_the_fact_rather_than_assuming():
    """Absent is 'not measured', and a claim about absence must not be
    built on an unmeasured window."""
    c = counts(open_items=49, quiet_items=23)
    c.pop("recent_items", None)
    assert "went_quiet" not in {f.key for f in facts_for_person(c)}


def test_the_other_facts_are_unaffected_by_the_quiet_floor():
    c = counts(open_items=24, quiet_items=23, recent_items=1)
    assert {"closed_late", "re_dated", "handed_back", "restated"} <= \
        {f.key for f in facts_for_person(c)}


# --- the ceiling and the contrast have to be satisfiable together ------

def test_the_prompt_asks_for_a_claim_with_no_digits():
    """Measured on the first live cycle 2026-08-16: four of four cards were
    rejected `claim_too_long`. Requiring both halves of the contrast INSIDE
    a 62 character claim is not satisfiable, and the counts are rendered
    underneath the sentence anyway, so the claim names the pattern in
    words and the arithmetic stays on the card where it is checkable."""
    from contextquilt.services.relationship_lenses import STANDS_OUT_SYSTEM
    assert "WRITE THE CLAIM WITHOUT DIGITS" in STANDS_OUT_SYSTEM


def test_every_example_claim_in_the_prompt_would_actually_pass():
    """A prompt that models an unshippable claim teaches the model to
    write unshippable claims, which is how this lens shipped three times
    without producing a card. Every quoted example sentence in the prompt
    is checked against the real ceiling.

    Anchored on the shape of an example (a quoted sentence ending in a
    full stop) rather than on a heading, because the heading has already
    been rewritten once and took this test with it.
    """
    from contextquilt.services.insight_cards import MAX_CLAIM_CHARS
    from contextquilt.services.relationship_lenses import STANDS_OUT_SYSTEM
    import re
    examples = re.findall(r'"([A-Z][^"]{15,}?\.)"', STANDS_OUT_SYSTEM)
    assert len(examples) >= 3, f"expected example claims, found {examples}"
    for example in examples:
        assert len(example) <= MAX_CLAIM_CHARS, (example, len(example))
        assert not re.search(r"\d", example), example


def test_a_wordy_claim_with_no_digits_still_passes_the_contrast_rule():
    """The contrast guard must not fire on a claim that states neither
    side, or the only shippable shape becomes unshippable."""
    got = parse_stands_out_response(
        _resp("Closes late far more often than others you work with.",
              "Ask for the date she will actually hit."),
        allowed_numbers(FACTS), person_name="Pallavi", facts=FACTS,
    )
    assert got is not None and got["lens"] == LENS


# --- the writer copies the shape of what it is given -------------------

def test_every_fact_has_a_short_phrase_for_the_writer():
    from contextquilt.services.relationship_lenses import (
        FACT_PHRASES, FACT_SUBJECTS,
    )
    assert set(FACT_PHRASES) == set(FACT_SUBJECTS)


def test_the_writer_phrases_are_short_enough_to_build_a_claim_from():
    """Measured 2026-08-16: handed the 50 character label 'items that were
    closed after the date they were due', the model returned a 75
    character claim against a 62 ceiling, identically on three calls
    because the temperature is pinned. It was not disobeying, it was
    describing the shape of what it was given (doc 19.8). A phrase has to
    leave room for a sentence around it."""
    from contextquilt.services.insight_cards import MAX_CLAIM_CHARS
    from contextquilt.services.relationship_lenses import FACT_PHRASES
    for key, phrase in FACT_PHRASES.items():
        assert len(phrase) <= MAX_CLAIM_CHARS // 2, (key, phrase, len(phrase))


def test_the_prompt_hands_over_the_short_phrase_not_the_long_label():
    from contextquilt.services.relationship_lenses import (
        FACT_PHRASES, build_stands_out_content,
    )
    content = build_stands_out_content("Pallavi", FACTS, [])
    assert FACT_PHRASES["closed_late"] in content


def test_the_precise_label_still_reaches_the_card():
    """The short phrase is for phrasing only. The counts still have to be
    labelled with exactly what was measured."""
    content = build_stands_out_content("Pallavi", FACTS, [])
    assert FACTS["subject"] in content


def test_an_unknown_fact_key_falls_back_to_the_long_subject():
    """A new fact must never lose its description just because nobody
    added a short phrase for it yet."""
    from contextquilt.services.relationship_lenses import build_stands_out_content
    facts = dict(FACTS, fact_key="something_new")
    assert FACTS["subject"] in build_stands_out_content("X", facts, [])


def test_the_prompt_forbids_the_preamble_it_used_to_invite():
    """Measured live: 'The do line says what the user should do
    differently IN THE NEXT MEETING' produced 'In your next meeting, ask
    what blocks Pallavi...' at 125 characters against a 90 ceiling. The
    model copied the phrase out of the instruction, which is the same
    failure as the long subject label one paragraph earlier."""
    from contextquilt.services.relationship_lenses import STANDS_OUT_SYSTEM
    assert "STARTS WITH A VERB" in STANDS_OUT_SYSTEM
    assert "In your next meeting," in STANDS_OUT_SYSTEM  # named as forbidden


def test_every_example_do_line_in_the_prompt_would_actually_pass():
    from contextquilt.services.insight_cards import MAX_DO_CHARS
    from contextquilt.services.relationship_lenses import STANDS_OUT_SYSTEM
    import re
    block = STANDS_OUT_SYSTEM.split("STARTS WITH A VERB")[1]
    examples = re.findall(r'"(Ask [^"]+?\.)"', block)
    assert len(examples) >= 3, examples
    for example in examples:
        assert len(example) <= MAX_DO_CHARS, (example, len(example))
