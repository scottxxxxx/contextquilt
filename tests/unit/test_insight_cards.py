"""The insight card collapse: at most one card per lens, and which one.

A person holds several `person` patches (one per surface form the
extractor used) and an insight stamps whichever was current when it was
derived, so the read widened to every form and surfaced every form's
card. This collapses them.

WHICH one it keeps is the part that had a defect. See
test_a_model_lens_collapses_on_evidence_rows_not_recency for the
production case: three cards derived from 32, 21 and 2 meetings, and it
kept the 2.
"""

from contextquilt.services import insight_cards


# --------------------------------------------------------------------
# The collapse must not keep the thinnest card
# --------------------------------------------------------------------

def _card(lens, ident, denominator=None, evidence_rows=0):
    return {"lens": lens, "id": ident,
            "facts": {"denominator": denominator} if denominator is not None else None,
            "evidence": list(range(evidence_rows))}


def test_a_model_lens_collapses_on_evidence_rows_not_recency():
    """The starvation this fixes, measured on production 2026-08-31.

    `facts.denominator` is null for a lens a model reasoned its way to:
    it counted nothing, so it has no counts. `how_they_decide` and
    `what_moves_them` are that family. So the old comparison saw 0 on
    every card, never fired, and fell back to newest-first.

    Suresh held three `how_they_decide` cards derived from 32, 21 and 2
    meetings and the collapse kept the 2. ShoulderSurf requires three
    evidence rows before rendering a lens, so both of his model lenses
    dropped and the person we hold 140 meetings on rendered ONE card,
    while Pallavi with one card per lens rendered all of them.
    """
    kept = insight_cards.one_card_per_lens([
        _card("how_they_decide", "newest", evidence_rows=2),
        _card("how_they_decide", "richest", evidence_rows=32),
        _card("how_they_decide", "middle", evidence_rows=21),
    ])
    assert [c["id"] for c in kept] == ["richest"]


def test_a_real_denominator_still_wins_outright():
    # The original rule is unchanged where a computed lens carries its
    # own arithmetic base: that number answers the question directly and
    # the evidence rows are only a fallback for lenses without one.
    kept = insight_cards.one_card_per_lens([
        _card("what_stands_out", "small_base", denominator=5, evidence_rows=40),
        _card("what_stands_out", "large_base", denominator=58, evidence_rows=2),
    ])
    assert [c["id"] for c in kept] == ["large_base"]


def test_evidence_rows_break_a_tie_between_equal_denominators():
    kept = insight_cards.one_card_per_lens([
        _card("what_moves_them", "thin", denominator=8, evidence_rows=2),
        _card("what_moves_them", "thick", denominator=8, evidence_rows=19),
    ])
    assert [c["id"] for c in kept] == ["thick"]


def test_recency_remains_the_last_resort_when_nothing_distinguishes_them():
    # Cards arrive newest-first, so with no counts and no evidence on
    # either side the first one seen still wins. That is the only case
    # the old behaviour was ever right for.
    kept = insight_cards.one_card_per_lens([
        _card("how_they_decide", "newest"),
        _card("how_they_decide", "older"),
    ])
    assert [c["id"] for c in kept] == ["newest"]


def test_a_malformed_evidence_field_does_not_crash_the_collapse():
    # Serving must never fail the detail route, and a card is a dict
    # from a JSONB blob rather than a validated object.
    for bad in (None, "three", 7, {"a": 1}):
        kept = insight_cards.one_card_per_lens([
            {"lens": "x", "id": "a", "facts": None, "evidence": bad},
            _card("x", "b", evidence_rows=5),
        ])
        assert [c["id"] for c in kept] == ["b"], bad
