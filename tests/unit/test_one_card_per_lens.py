"""One lens, one card, however many spellings of the person exist.

Scott's 2026-08-16 screenshots: Sukumar's page rendered two
HOW THEY DECIDE chips and two WHAT MOVES THEM, Vijay two HOW THEY
DECIDE. Cause was #249 widening the insight read to every surface form's
person patch without collapsing what came back. The write path is fixed
separately; these cards are already in the database, so the read has to
collapse them too.
"""

from contextquilt.services.insight_cards import one_card_per_lens


def _card(lens, text, patch_id="p"):
    return {"patch_id": patch_id, "lens": lens, "text": text}


def test_a_repeated_lens_collapses_to_one_card():
    cards = [
        _card("how_they_decide", "Sequences work in dependency order.", "new"),
        _card("how_they_decide", "Gates forward movement on technical work.", "old"),
    ]
    assert [c["patch_id"] for c in one_card_per_lens(cards)] == ["new"]


def test_newest_wins():
    """The read hands them over newest first, and after the cluster merge
    the newest is the one derived from the whole record."""
    cards = [
        _card("what_moves_them", "newest", "n"),
        _card("what_moves_them", "older", "o"),
        _card("what_moves_them", "oldest", "z"),
    ]
    kept = one_card_per_lens(cards)
    assert len(kept) == 1 and kept[0]["text"] == "newest"


def test_distinct_lenses_all_survive():
    cards = [
        _card("how_they_decide", "a"),
        _card("what_moves_them", "b"),
        _card("how_they_follow_through", "c"),
    ]
    assert len(one_card_per_lens(cards)) == 3


def test_the_stack_scott_saw_becomes_the_stack_the_design_draws():
    """Sukumar's live page, verbatim shape: five cards down to three."""
    cards = [
        _card("how_they_decide", "Sequences work in dependency order.", "1"),
        _card("how_they_decide", "Gates forward movement on technical work.", "2"),
        _card("what_moves_them", "Responds to concrete blockers.", "3"),
        _card("what_moves_them", "Responds to evidence of visibility.", "4"),
        _card("how_they_follow_through", "33 of 34 closed on time.", "5"),
    ]
    kept = one_card_per_lens(cards)
    assert [c["lens"] for c in kept] == [
        "how_they_decide", "what_moves_them", "how_they_follow_through",
    ]


def test_order_is_otherwise_preserved():
    cards = [_card("b", "1"), _card("a", "2"), _card("b", "3"), _card("c", "4")]
    assert [c["lens"] for c in one_card_per_lens(cards)] == ["b", "a", "c"]


def test_cards_without_a_lens_are_not_collapsed_together():
    """An unknown shape is not evidence of duplication."""
    cards = [_card(None, "one", "1"), _card(None, "two", "2")]
    assert len(one_card_per_lens(cards)) == 2


def test_empty_and_none_are_safe():
    assert one_card_per_lens([]) == []
    assert one_card_per_lens(None) == []


def test_an_unknown_lens_still_gets_a_card():
    """The vocabulary grows; a lens this build has never heard of is
    served, not dropped."""
    cards = [_card("how_they_hand_work_back", "something new")]
    assert len(one_card_per_lens(cards)) == 1


# --- evidence beats recency -------------------------------------------

def _counted(lens, denominator, patch_id):
    return {"patch_id": patch_id, "lens": lens, "text": "x",
            "facts": {"numerator": 1, "denominator": denominator}}


def test_the_card_computed_from_more_of_the_record_wins():
    """The user's items are split across two app ids by the August flip
    (916 rows one side, 279 the other) and the pass is ACL-scoped, so it
    runs once per app over a different slice of the same person. Newest
    would hand the page whichever pass finished last."""
    cards = [_counted("what_stands_out", 3, "thin-but-newer"),
             _counted("what_stands_out", 30, "rich-but-older")]
    assert [c["patch_id"] for c in one_card_per_lens(cards)] == ["rich-but-older"]


def test_newest_still_wins_when_neither_card_carries_counts():
    cards = [_card("how_they_decide", "newest", "n"),
             _card("how_they_decide", "older", "o")]
    assert [c["patch_id"] for c in one_card_per_lens(cards)] == ["n"]


def test_a_card_with_counts_beats_one_without_only_on_evidence():
    """A missing facts block reads as zero evidence, so a counted card
    outranks it, but two uncounted cards keep the recency rule."""
    cards = [_card("what_stands_out", "no counts", "plain"),
             _counted("what_stands_out", 12, "counted")]
    assert [c["patch_id"] for c in one_card_per_lens(cards)] == ["counted"]


def test_a_malformed_denominator_does_not_crash_the_page():
    cards = [_counted("what_stands_out", 10, "good"),
             {"patch_id": "bad", "lens": "what_stands_out",
              "facts": {"denominator": "not a number"}}]
    assert [c["patch_id"] for c in one_card_per_lens(cards)] == ["good"]


def test_lens_order_is_first_appearance_not_winner_position():
    """Swapping which card wins must not reorder the stack."""
    cards = [_counted("a", 1, "a-thin"), _counted("b", 9, "b"),
             _counted("a", 99, "a-rich")]
    assert [c["lens"] for c in one_card_per_lens(cards)] == ["a", "b"]
    assert [c["patch_id"] for c in one_card_per_lens(cards)] == ["a-rich", "b"]
