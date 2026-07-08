"""Unit tests for recall_signals (metamemory gap lines)."""

from src.contextquilt.services.recall_signals import (
    MAX_UNMATCHED_MENTIONS,
    build_signal_lines,
    extract_unmatched_mentions,
    memory_signals_enabled,
)

KNOWN = {"Sarah Abrams", "ABM Industries", "Falcon Redesign", "S. Abrams"}


# ------------------------------------------------------------------
# memory_signals_enabled — lenient truthy parsing, never raises
# ------------------------------------------------------------------

def test_enabled_accepts_bool_int_and_string_truthy():
    for raw in (True, 1, "true", "TRUE", " 1 ", "yes"):
        assert memory_signals_enabled({"memory_signals": raw}) is True


def test_enabled_rejects_falsy_missing_and_junk():
    for md in (None, {}, {"memory_signals": False}, {"memory_signals": 0},
               {"memory_signals": "false"}, {"memory_signals": "banana"},
               {"memory_signals": ["true"]}, {"memory_signals": 2}):
        assert memory_signals_enabled(md) is False


# ------------------------------------------------------------------
# extract_unmatched_mentions — precision over recall
# ------------------------------------------------------------------

def test_unknown_name_mid_sentence_is_reported():
    out = extract_unmatched_mentions("Can you ask Priya about the launch?", KNOWN)
    assert out == ["Priya"]


def test_multi_word_unknown_name_is_reported_even_at_sentence_start():
    out = extract_unmatched_mentions("Marcus Webb wants the deck early.", KNOWN)
    assert out == ["Marcus Webb"]


def test_single_word_at_sentence_start_is_ignored():
    # Ordinary sentence casing, not a name signal.
    assert extract_unmatched_mentions("Deadlines keep slipping on this.", KNOWN) == []
    assert extract_unmatched_mentions("First sentence. Deadlines slipped.", KNOWN) == []


def test_partial_name_overlap_with_index_is_suppressed():
    # The index knows "Sarah Abrams"; the text says "Sarah". Claiming
    # a gap here would be a false absence — worse than silence.
    assert extract_unmatched_mentions("What did Sarah say yesterday?", KNOWN) == []


def test_word_level_overlap_suppresses_multi_word_candidates():
    # "Sarah Chen" shares "Sarah" with a known entity — stay quiet.
    assert extract_unmatched_mentions("Loop in Sarah Chen please.", KNOWN) == []


def test_alias_words_also_suppress():
    assert extract_unmatched_mentions("Ping Abrams about it.", KNOWN) == []


def test_known_entity_exact_mention_not_reported():
    text = "Where are we on the Falcon Redesign with ABM Industries?"
    assert extract_unmatched_mentions(text, KNOWN) == []


def test_leading_stopword_is_stripped_from_run():
    out = extract_unmatched_mentions("Did The Orion Initiative get approved?", KNOWN)
    assert out == ["Orion Initiative"]


def test_stopword_only_runs_and_short_words_dropped():
    assert extract_unmatched_mentions("Ok I think We should. Hi there.", KNOWN) == []
    # < 3 chars single word mid-sentence
    assert extract_unmatched_mentions("send it to Al today", KNOWN) == []


def test_allcaps_and_acronyms_do_not_trigger():
    assert extract_unmatched_mentions("URGENT: fix the API and the SLA now", KNOWN) == []


def test_dedup_order_and_cap():
    # Sentence-start "Priya" is ambiguous (dropped), but her mid-sentence
    # recurrence recovers her — order follows first confident occurrence.
    text = ("Priya met Deshawn, then Priya called Kwame, "
            "then Zorina and Thandiwe joined.")
    out = extract_unmatched_mentions(text, KNOWN)
    assert out == ["Deshawn", "Priya", "Kwame"]
    assert len(out) == MAX_UNMATCHED_MENTIONS


def test_deterministic_for_identical_inputs():
    text = "Ask Priya and Marcus Webb about the Orion Initiative."
    a = extract_unmatched_mentions(text, set(KNOWN))
    b = extract_unmatched_mentions(text, set(KNOWN))
    assert a == b


def test_empty_index_reports_unknown_names():
    out = extract_unmatched_mentions("Ask Priya about it.", set())
    assert out == ["Priya"]


def test_days_and_months_are_stopwords():
    assert extract_unmatched_mentions("Move it to Friday or early June.", KNOWN) == []


# ------------------------------------------------------------------
# build_signal_lines — fixed order, exact wording
# ------------------------------------------------------------------

def test_all_three_lines_in_fixed_order():
    lines = build_signal_lines(
        ["Priya", "Orion Initiative"],
        project_scope_label="Falcon",
        project_scope_missing=True,
        nothing_matched=True,
    )
    assert lines == [
        "(no stored memory about: Priya, Orion Initiative)",
        '(no stored project memory for "Falcon")',
        "(memory checked: nothing stored matched this request)",
    ]


def test_no_lines_when_nothing_to_say():
    assert build_signal_lines([]) == []


def test_scope_line_requires_label():
    assert build_signal_lines([], project_scope_label=None, project_scope_missing=True) == []
