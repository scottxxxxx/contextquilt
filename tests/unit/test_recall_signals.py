"""Unit tests for recall_signals (metamemory gap lines)."""

from src.contextquilt.services.recall_signals import (
    MAX_UNMATCHED_MENTIONS,
    build_signal_lines,
    extract_unmatched_mentions,
    memory_signals_enabled,
)

KNOWN = {"Sarah Abrams", "Axiom Industries", "Falcon Redesign", "S. Abrams"}


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
    text = "Where are we on the Falcon Redesign with Axiom Industries?"
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


# ------------------------------------------------------------------
# build_coverage_line — contract commitment E
# ------------------------------------------------------------------

from src.contextquilt.services.recall_signals import build_coverage_line


def test_coverage_line_when_truncated():
    assert build_coverage_line(15, 98) == "(showing 15 of 98 stored patches for this project)"
    assert build_coverage_line(0, 7) == "(showing 0 of 7 stored patches for this project)"


def test_no_coverage_line_when_complete_or_unscoped():
    assert build_coverage_line(98, 98) is None
    assert build_coverage_line(100, 98) is None
    assert build_coverage_line(0, 0) is None
    assert build_coverage_line(5, 0) is None


# ------------------------------------------------------------------
# Wire-text fixes (2026-07-18 build-749 test findings): CamelCase
# fragments, markdown bullet casing, and conversation-history focus.
# ------------------------------------------------------------------

WIRE_KNOWN = {"HubSpot", "Brightbeam Academy", "Artemis", "CBE"}


def test_camelcase_word_is_swallowed_whole_and_suppressed():
    # "HubSpot" must not shed a "Hub" fragment that dodges suppression.
    assert extract_unmatched_mentions("Check the HubSpot pipeline today.", WIRE_KNOWN) == []


def test_fragment_prefix_of_known_word_is_suppressed():
    # Even when a fragment arrives on its own, precision-first: "Hub"
    # prefixes known "hubspot", so no gap claim.
    assert extract_unmatched_mentions("Did we discuss Hub with anyone?", WIRE_KNOWN) == []


def test_single_word_opening_a_markdown_bullet_is_ignored():
    text = "Priorities:\n- Engage with relevant opportunities\n- Complete the course\n* Review docs"
    assert extract_unmatched_mentions(text, WIRE_KNOWN) == []


def test_history_framing_scopes_gap_claims_to_current_question():
    # The production fingerprint from the 749 test: artifacts in the
    # prior answer must not starve the real gap in the live question.
    text = (
        "User question: What are the current priorities on this project? "
        "Assistant: Priorities are:\n"
        "- Engage with opportunities via the HubSpot pipeline\n"
        "- Complete the Brightbeam Academy course\n"
        "Alpha Omega Consulting and Bravo Dynamics were also mentioned.\n"
        "Current question: What are the current priorities on this project, "
        "and where did we land with Zephyrline?"
    )
    assert extract_unmatched_mentions(text, WIRE_KNOWN) == ["Zephyrline"]


def test_no_framing_scans_whole_text():
    out = extract_unmatched_mentions("Where did we land with Zephyrline?", WIRE_KNOWN)
    assert out == ["Zephyrline"]


def test_unknown_names_in_current_question_still_reported_and_capped():
    text = (
        "User question: Earlier stuff. Assistant: An answer.\n"
        "Current question: What's the status of Zephyrline, Kestrelmark, Ospreygate and Falconworks?"
    )
    out = extract_unmatched_mentions(text, WIRE_KNOWN)
    assert out == ["Zephyrline", "Kestrelmark", "Ospreygate"]


def test_qa_history_resend_shape_scopes_gap_to_live_question():
    # Anonymized structural twin of the genuine Zephyrline recall resend GP
    # captured off their socket (the 2586-char wire text; verified locally that
    # the shipped extractor yields ["Zephyrline"] on the real bytes). Real
    # names and meeting content are deliberately NOT committed to this public
    # repo — only the fingerprint that made the resend a distinct regression:
    # the "Previous conversation in this chat:" + inline Q:/A: framing (a
    # different lead-in than the User/Assistant twin above but the same trailing
    # "Current question:" marker), markdown headers/bullets, CamelCase known
    # words, prior-answer names that must stay scoped out, and the one genuinely
    # unknown name (Zephyrline) that lives in the live question while also
    # echoing inside the prior answer. It must surface exactly once.
    text = (
        "Previous conversation in this chat:\n"
        "Q: What are the current priorities on this project?\n"
        "A: Based on the meetings in this project, here are the priorities:\n\n"
        "---\n\n"
        "## Current Priorities\n\n"
        "### 1. **Pipeline Engagement**\n"
        "- Review the HubSpot pipeline for deals at 60%+ stage\n"
        "- Engage with relevant opportunities to build expertise\n"
        "- Complete the Brightbeam Academy course and review Artemis docs\n\n"
        "### 2. **CBE Support**\n"
        "- Devlin is experiencing response-time issues post-release\n"
        "- Sarita asked about filling the gap left by Bram\n\n"
        "Alpha Omega Consulting and Bravo Dynamics were also mentioned.\n"
        "Q: What are the current priorities, and where did we land with Zephyrline?\n"
        "A: I don't have any context about Zephyrline in the meetings available "
        "to me. Could you clarify whether Zephyrline is a customer or a project?\n"
        "Current question: What are the current priorities on this project, "
        "and where did we land with Zephyrline?"
    )
    assert extract_unmatched_mentions(text, WIRE_KNOWN) == ["Zephyrline"]
