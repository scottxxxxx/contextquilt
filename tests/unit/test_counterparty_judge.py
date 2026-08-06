"""The counterparty judge's contract, mostly its refusals.

The parser is the last gate before a backfill writes an `owed_to` edge,
and the failure it has to prevent is the confident one: telling a user
they owe something to somebody they do not. Every ambiguous path here
resolves to None on purpose.
"""

from contextquilt.services.counterparty import (
    COUNTERPARTY_JUDGE_SCHEMA,
    COUNTERPARTY_JUDGE_SYSTEM,
    build_counterparty_content,
    parse_counterparty_verdicts,
)

PEOPLE = ["Lockridge Chen", "Marcus Webb", "Kinsley Raman"]


def test_a_clean_verdict_maps_through():
    content = {"verdicts": [{"item": 0, "owed_to": "Lockridge Chen"}]}
    assert parse_counterparty_verdicts(content, 1, PEOPLE) == ["Lockridge Chen"]


def test_null_is_a_real_answer_not_a_failure():
    content = {"verdicts": [{"item": 0, "owed_to": None}]}
    assert parse_counterparty_verdicts(content, 1, PEOPLE) == [None]


def test_returned_value_is_the_exact_surface_form():
    """Matching is case-insensitive because the model re-cases names, but
    the caller looks the result up against stored person patches, so what
    comes back must be the list's own spelling."""
    content = {"verdicts": [{"item": 0, "owed_to": "lockridge chen"}]}
    assert parse_counterparty_verdicts(content, 1, PEOPLE) == ["Lockridge Chen"]


def test_a_name_outside_the_closed_list_is_discarded():
    """The judge cannot invent a counterparty. A backfill has no business
    creating person patches."""
    content = {"verdicts": [{"item": 0, "owed_to": "Dana Brooks"}]}
    assert parse_counterparty_verdicts(content, 1, PEOPLE) == [None]


def test_partial_name_does_not_match():
    content = {"verdicts": [{"item": 0, "owed_to": "Lockridge"}]}
    assert parse_counterparty_verdicts(content, 1, PEOPLE) == [None]


def test_missing_and_extra_items_are_handled():
    content = {"verdicts": [
        {"item": 2, "owed_to": "Marcus Webb"},
        {"item": 9, "owed_to": "Lockridge Chen"},   # out of range
        {"item": -1, "owed_to": "Lockridge Chen"},  # out of range
    ]}
    assert parse_counterparty_verdicts(content, 3, PEOPLE) == [None, None, "Marcus Webb"]


def test_malformed_output_resolves_to_all_none():
    for junk in (None, [], "", {"verdicts": None}, {"verdicts": "x"},
                 {"verdicts": [None, 3]}, {"nope": []},
                 {"verdicts": [{"item": "0", "owed_to": "Lockridge Chen"}]},
                 {"verdicts": [{"item": 0, "owed_to": 42}]},
                 {"verdicts": [{"item": 0, "owed_to": True}]}):
        assert parse_counterparty_verdicts(junk, 2, PEOPLE) == [None, None], junk


def test_blank_candidates_cannot_swallow_a_verdict():
    content = {"verdicts": [{"item": 0, "owed_to": "  "}]}
    assert parse_counterparty_verdicts(content, 1, ["", "  ", "Lockridge Chen"]) == [None]


def test_last_verdict_for_an_item_wins_rather_than_crashing():
    content = {"verdicts": [
        {"item": 0, "owed_to": "Lockridge Chen"},
        {"item": 0, "owed_to": "Marcus Webb"},
    ]}
    assert parse_counterparty_verdicts(content, 1, PEOPLE) == ["Marcus Webb"]


def test_content_renders_both_blocks():
    out = build_counterparty_content(
        ["Send the routing diagram", "Finish the plan"], PEOPLE
    )
    assert "PEOPLE:" in out and "- Lockridge Chen" in out
    assert "ITEM 0: Send the routing diagram" in out
    assert "ITEM 1: Finish the plan" in out


def test_content_flattens_newlines_and_caps_length():
    out = build_counterparty_content(["a\nb", "x" * 500], PEOPLE)
    assert "ITEM 0: a b" in out
    assert "x" * 300 in out and "x" * 301 not in out


def test_prompt_embeds_the_output_shape():
    """AnthropicLLMClient.extract accepts json_schema for interface parity
    but does NOT enforce it on the wire, so the shape has to be in the
    prompt text or the model answers in prose."""
    assert '{"verdicts":' in COUNTERPARTY_JUDGE_SYSTEM
    assert '"owed_to"' in COUNTERPARTY_JUDGE_SYSTEM
    assert COUNTERPARTY_JUDGE_SCHEMA["required"] == ["verdicts"]


def test_prompt_carries_no_dashes():
    """Models copy the punctuation they see, and this prompt's output ends
    up nowhere near a user, but the house rule is the house rule."""
    assert "—" not in COUNTERPARTY_JUDGE_SYSTEM
    assert "–" not in COUNTERPARTY_JUDGE_SYSTEM
