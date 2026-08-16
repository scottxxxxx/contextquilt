"""The prose lenses converged harder than the one I built, and they are
the ones Scott actually complained about.

Measured 2026-08-16 across every live card:

  what_moves_them   6 claims, TWO distinct opening words between them
  "Responds to"     opens 5 cards across 5 different people
  "Gates forward"   opens 3
  Sukumar / Vijay   byte-identical claims

His words: "more than one person mentions concrete, our vocab seems
limited." Four of the six what_moves_them claims contain "concrete".
"""

import pathlib

from contextquilt.services.consolidation import (
    MODEL_CHOSEN_LENSES,
    build_profile_content,
    parse_profile_response,
)

WORKER = pathlib.Path("src/worker.py").read_text()

DATED = [("2026-07-01", "shipped the thing"), ("2026-07-08", "asked for data")]
USED = ["Responds to concrete blockers and dependency chains."]


def _resp(text, do, lens="how_they_decide"):
    return {"skip": False, "lens": lens, "text": text, "do": do, "reason": "r"}


def test_the_writer_is_shown_what_was_said_about_the_others():
    content = build_profile_content("Priya", DATED, used_claims=USED)
    assert "ALREADY SAID" in content
    assert USED[0] in content


def test_a_claim_reusing_another_persons_opening_is_rejected():
    defects = []
    assert parse_profile_response(
        _resp("Responds to concrete evidence over assumptions.",
              "Ask for the data before the meeting."),
        person_name="Priya", defects=defects, used_claims=USED,
    ) is None
    assert defects == ["claim_repeats_another"]


def test_a_genuinely_different_claim_survives():
    got = parse_profile_response(
        _resp("Waits for a second opinion before committing.",
              "Bring the other reviewer's take with you."),
        person_name="Priya", used_claims=USED,
    )
    assert got is not None


def test_the_guard_spans_both_prose_lenses():
    """This pass does not know which lens it will produce until the
    model answers, and the convergence crossed the lens boundary anyway."""
    assert "cp.value->>'lens' = ANY($2::text[])" in WORKER
    assert "sorted(MODEL_CHOSEN_LENSES)" in WORKER
    assert len(MODEL_CHOSEN_LENSES) == 2


def test_the_persons_own_earlier_claims_are_not_treated_as_collisions():
    """A second lens for the SAME person is the stack working as
    designed. Only other people's claims constrain the wording."""
    assert "AND NOT (cp.value->>'source_person' = ANY($3::text[]))" in WORKER


def test_the_prose_pass_gets_the_same_bounded_retry():
    """Forcing novelty inside a hard ceiling made the contrastive lens
    fail 2 of 4 until a corrective retry was added. Same dynamics here."""
    assert WORKER.count("relationship_lenses.retry_note(") == 2


def test_no_used_claims_means_no_collision_check():
    got = parse_profile_response(
        _resp("Responds to concrete blockers and dependency chains.",
              "Bring the blocker list."),
        person_name="Priya",
    )
    assert got is not None
