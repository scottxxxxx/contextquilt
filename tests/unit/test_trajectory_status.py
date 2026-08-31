"""Why there is no trajectory card, which is not the same question as whether.

2026-08-31: Scott asked why "how they're changing" had disappeared. It
had not broken. Suresh's card was archived that afternoon with cause
`lapsed`, because his speaking turns went from 316 against 201, a real
shift, to 271 against 258, a 5% move. The card requires a 20 point gap
AND a 40% relative change before it claims anything, so it correctly
withdrew a claim that had stopped being true, and the screen showed a
hole.

An absence with no reason is indistinguishable from a bug, and the
reader resolves that ambiguity against us every time. This is the fourth
instance of one rule: `dropped` on the woven route, `project_known`'s
three states, `reconciling` on this same object, and now this.

Read as source, the constraint every route test here works under.
"""

from pathlib import Path

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def _block() -> str:
    start = MAIN.index("trajectory_status = None")
    return MAIN[start:MAIN.index("    reconciling = None")]


# --------------------------------------------------------------------
# The distinction ShoulderSurf asked for, which is the whole design
# --------------------------------------------------------------------

def test_lapsed_and_never_qualified_are_different_states():
    """They earn different sentences and must never collapse.

    "This was a trend and it flattened" earns a line, because steady is
    a finding and it is the one a reader would not have guessed.
    "There has never been enough here to measure" earns silence, and
    dressing that up as steadiness would be inventing a finding.
    """
    block = _block()
    assert '"state": "never_qualified"' in block
    assert '"lapsed" if cause == "lapsed" else "withdrawn"' in block


def test_an_archive_for_any_other_reason_is_not_reported_as_lapsed():
    # A card removed by a correction did not flatten. Folding every
    # archive cause into `lapsed` would let the client say "no
    # significant change" about a card that was withdrawn for being
    # wrong.
    assert '"withdrawn"' in _block()


def test_an_active_card_reports_active_rather_than_a_reason():
    # The reason field exists to explain an absence. With a card
    # present there is nothing to explain, and inventing a cause here
    # would be a second source of truth about the card itself.
    block = _block()
    assert '"state": "active"' in block


def test_the_withdrawn_claim_is_carried_and_named_for_what_it_is():
    """So the client can be specific instead of generic.

    "Turns were up sharply through late August, the last 8 meetings are
    level" beats "no significant change", and the key is named
    `withdrawn_claim` precisely so nobody renders it as a live one.
    """
    block = _block()
    assert '"withdrawn_claim": cv.get("text")' in block
    assert '"withdrawn_claim": None' in block


def test_a_failed_lookup_serves_null_rather_than_never_qualified():
    """Null means CQ cannot tell.

    Answering "never qualified" because a query failed would be the
    quietest possible lie: it renders as silence, which is exactly what
    a healthy never-qualified renders as, so nothing would ever show it
    was wrong.
    """
    block = _block()
    assert "except Exception" in block
    assert 'logger.warning("trajectory_status_unavailable"' in block
    assert "trajectory_status = None" in MAIN[:MAIN.index("except Exception as exc:\n        # Null means CQ cannot tell")]


def test_it_is_served_on_the_person_detail():
    assert '"trajectory_status": trajectory_status,' in MAIN


def test_the_card_is_matched_by_entity_AND_by_person_patch_ids():
    """Both, because which identity a card is keyed on depends on the pass.

    The model lenses stamp `source_person` with a person PATCH id; the
    contrastive pass keys on the ENTITY, which is the identity that does
    not move when the extractor rephrases somebody. Matching only one
    left live cards unreachable before, which is a bug this object has
    already had once.
    """
    block = _block()
    assert "source_entity_id" in block and "source_person" in block


def test_the_lens_name_comes_from_the_service_not_a_literal():
    # A literal here drifts the moment the lens is renamed, and the
    # failure is silent: no card matches, so every person reports
    # never_qualified.
    block = _block()
    assert "trajectory_svc.LENS" in block
    assert "how_theyre_changing" not in block
