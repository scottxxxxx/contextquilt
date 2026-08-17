"""The `believed_resolved` ledger mode.

A meeting said something that looked like an item being finished, but not
well enough to close on. The item is STILL OPEN and still owed, and this
mode is a question for a human rather than a verdict.
"""

from __future__ import annotations

from datetime import date

from contextquilt.services import item_ledger as il

TODAY = date(2026, 8, 17)


def _row(**over):
    row = {
        "patch_id": "11111111-1111-1111-1111-111111111111",
        "patch_type": "commitment",
        "text": "Send the threshold logic to Pallavi",
        "owner": "Vijay",
        "created_at": date(2026, 8, 1),
        "completed_at": None,
    }
    row.update(over)
    return row


def test_believed_item_is_still_open_and_carries_its_receipts():
    item = il.classify_item(_row(
        believed_complete_at="2026-08-17T09:00:00Z",
        believed_complete_evidence="Vijay confirmed he will send it later today",
        believed_complete_reasons=["future_intent_only"],
        believed_complete_origin_id="MEETING-1",
    ), TODAY)
    assert item["mode"] == il.BELIEVED_RESOLVED
    assert item["completed_at"] is None, "a belief must never look like a completion"
    assert item["believed_complete_reasons"] == ["future_intent_only"]
    assert item["believed_complete_origin_id"] == "MEETING-1"


def test_no_belief_means_plain_open():
    assert il.classify_item(_row(), TODAY)["mode"] == il.OPEN


def test_a_real_completion_outranks_a_belief():
    """If a human confirmed it, `resolved` is the headline. The belief is
    spent, not competing with the answer it asked for."""
    item = il.classify_item(_row(
        completed_at=date(2026, 8, 16),
        believed_complete_at="2026-08-15T09:00:00Z",
    ), TODAY)
    assert item["mode"] == il.RESOLVED
    assert il.BELIEVED_RESOLVED not in item["modes"]


def test_belief_outranks_restated_and_re_dated():
    """Whether the item is still owed at all changes what every other
    mode on it means, so it is the headline."""
    item = il.classify_item(_row(
        believed_complete_at="2026-08-17T09:00:00Z",
        restatements=[{"observed_at": "2026-08-10", "origin_id": "M2"}],
        deadline_history=[{"from": "2026-08-05", "to": "2026-08-20"}],
    ), TODAY)
    assert item["mode"] == il.BELIEVED_RESOLVED
    # and nothing else is hidden by that choice
    assert il.RE_DATED in item["modes"]


def test_reasons_survive_a_json_string_from_the_wire():
    """A `->>` select delivers the array as text. _as_list would have
    returned [] here without raising, stripping the justification off a
    claim that stays on screen."""
    item = il.classify_item(_row(
        believed_complete_at="2026-08-17T09:00:00Z",
        believed_complete_reasons='["owner_not_named_in_evidence", "future_intent_only"]',
    ), TODAY)
    assert item["believed_complete_reasons"] == [
        "owner_not_named_in_evidence", "future_intent_only",
    ]


def test_reasons_degrade_to_empty_rather_than_raising():
    for junk in (None, "", "not json", 42, [1, 2], [{"a": 1}]):
        item = il.classify_item(_row(
            believed_complete_at="2026-08-17T09:00:00Z",
            believed_complete_reasons=junk,
        ), TODAY)
        assert item["believed_complete_reasons"] == []


def test_mode_is_published_in_the_vocabulary_for_commitments():
    """A client must not have to discover this mode by seeing one."""
    vocab = il.vocabulary(["commitment"])
    modes = vocab["modes_by_object_type"]["commitment"]
    assert il.BELIEVED_RESOLVED in modes


def test_mode_is_universal_not_commitment_specific():
    """A question nobody answered can equally look answered. Only the
    date mode is commitment specific."""
    assert il.BELIEVED_RESOLVED in il.UNIVERSAL_MODES
    modes = il.modes_for_object_type("question")
    assert il.BELIEVED_RESOLVED in modes
    assert il.RE_DATED not in modes


def test_every_mode_has_a_precedence_slot():
    """A mode missing from MODE_PRECEDENCE raises on sort, on a read
    route, for whichever user happens to hold one."""
    for mode in il.UNIVERSAL_MODES + (il.RE_DATED,):
        assert mode in il.MODE_PRECEDENCE
