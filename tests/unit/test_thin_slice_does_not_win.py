"""The card stands on the most evidence available, not on scheduling.

The consolidation pass runs once per app id, and this user's items are
split across two of them by the August flip, so the same person is
measured twice over different slices of their own record.

Measured 2026-08-16: the pass that sees 28 of Sukumar's items wrote his
card at 08:59 (`restated 7/28`), and the pass that sees 86 items was then
blocked by the durable-no, so the thin slice beat the rich one purely by
finishing first. He lost `closed_late 1/30`, a gap of -29 and the
strongest card on the roster, to a +19.
"""

import pathlib

WORKER = pathlib.Path("src/worker.py").read_text()


def test_the_check_takes_the_candidates_evidence():
    assert "candidate_denominator: int = 0," in WORKER


def test_a_users_no_blocks_whatever_the_candidate_stands_on():
    """An archived card is a decision, never a measurement. No amount of
    fresh evidence overrides somebody saying no."""
    assert 'if any(r["status"] != "active" for r in rows):' in WORKER
    assert "return True" in WORKER


def test_a_thinner_live_card_does_not_block_a_richer_one():
    assert 'if not any(r["den"] < int(candidate_denominator or 0) for r in rows):' \
        in WORKER


def test_the_thin_card_is_retracted_rather_than_left_beside_the_new_one():
    """Two live cards for one lens would put the collapse back on the
    read; and it must retract rather than delete, so it never reads as a
    user's no later."""
    assert "stands_out_thin_card_retracted" in WORKER
    assert """'{archive_cause}',
                                      '"retracted"'::jsonb)""" in WORKER


def test_both_call_sites_pass_the_denominator():
    """A gate that defaults to zero evidence would silently restore the
    old behaviour at any call site that forgets it."""
    assert WORKER.count('chosen["fact"].denominator,') == 2
