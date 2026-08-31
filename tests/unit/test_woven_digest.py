"""Woven digest selection: the tiling invariant, and the two spec bugs.

Section 6 of the Woven handoff asks the memory layer to pick the 4 to 6
patches a week worth a tile. Two things in that spec are wrong and this
file pins the corrections so nobody quietly restores them.

1. The weight rule and the tiling rule contradict each other. Section 5
   fixes a 6-column grid with spans 1->2, 2->3, 3->4; section 6.1 says
   "top scorer -> 3, next two -> 2, rest -> 1", which is {3,2,2,1,1,1}
   and cannot tile in ANY order.
2. "NULL origin never gets a tile" would delete five types the spec's
   own token table renders, because NULL origin is the deliberate design
   for user-scoped types rather than orphanhood.
"""

from datetime import date
from itertools import permutations

import pytest

from contextquilt.services.woven_digest import (
    COLUMNS,
    DROP_EXHAUST,
    DROP_ORPHAN,
    DROP_PERSON,
    DROP_RESOLVED,
    DROP_SENSITIVE,
    DROP_SHELVED,
    SPAN_FOR_WEIGHT,
    USER_SCOPED_TYPES,
    assign_weights,
    build_digest,
    row_is_exact,
    row_pairs,
    salience,
    why_not_a_tile,
)

TODAY = date(2026, 8, 31)


def patch(pid="p1", ptype="takeaway", text="Ship the gateway", origin="m1", **value):
    return {"patch_id": pid, "patch_type": ptype, "origin_id": origin,
            "value": dict({"text": text}, **value)}


# --------------------------------------------------------------------
# The tiling invariant, which is the bug that would have shipped
# --------------------------------------------------------------------

def test_the_specs_own_weight_multiset_cannot_tile_in_any_order():
    """{3,2,2,1,1,1} from section 6.1 has ZERO valid arrangements.

    Exhaustive over all permutations. This is not a bad arrangement, it
    is an impossible one, which is why following 6.1 literally would
    have produced a ragged quilt rather than an obvious error.
    """
    def tiles(seq):
        def go(i):
            if i == len(seq):
                return True
            return any(
                i + k <= len(seq)
                and row_is_exact(seq[i:i + k])
                and go(i + k)
                for k in (2, 3)
            )
        return go(0)

    assert not any(tiles(list(p)) for p in set(permutations([3, 2, 2, 1, 1, 1])))


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6])
def test_every_digest_size_tiles_exactly(count):
    weights = assign_weights(count)
    assert len(weights) == count
    covered = sorted(i for row in row_pairs(count) for i in row)
    assert covered == list(range(count)), "every tile must be placed exactly once"
    for row in row_pairs(count):
        cells = [weights[i] for i in row]
        assert row_is_exact(cells), (
            f"{count} tiles: row {cells} spans "
            f"{sum(SPAN_FOR_WEIGHT[w] for w in cells)} of {COLUMNS} columns"
        )


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6])
def test_weights_never_increase_with_rank(count):
    # A weaker patch must never be shown larger than a stronger one.
    w = assign_weights(count)
    assert w == sorted(w, reverse=True), w


def test_a_single_tile_is_outside_the_grid_and_that_is_recorded():
    # No span equals 6, so one tile cannot fill a row. The client renders
    # it full width; this pins that we did not pretend otherwise.
    assert not row_is_exact(assign_weights(1))


def test_the_strongest_patch_still_gets_the_largest_tile():
    # Section 5 requires index 0 to be the strongest and take the first
    # tile. The outside-in pairing preserves that while fixing the rows.
    weights = assign_weights(6)
    assert weights[0] == max(weights)
    assert row_pairs(6)[0][0] == 0


def test_no_more_than_two_weight_three_tiles():
    # Section 6.1's rhythm cap, which the corrected distribution keeps.
    for count in range(1, 7):
        assert assign_weights(count).count(3) <= 2


# --------------------------------------------------------------------
# Pruning, section 6.2
# --------------------------------------------------------------------

def test_user_scoped_types_are_not_orphans():
    """The second spec bug. NULL origin is the DESIGN for these types.

    An origin-null filter would drop trait, preference, project and org,
    all of which the spec's own token table colours.
    """
    for ptype in sorted(USER_SCOPED_TYPES - {"person"}):
        p = patch(ptype=ptype, origin=None)
        assert why_not_a_tile(p) != DROP_ORPHAN, (
            f"{ptype} is user-scoped; NULL origin is its design, not orphanhood"
        )


def test_an_episode_type_without_an_origin_is_a_real_orphan():
    # The corrected test: no provenance to show, and provenance is the point.
    assert why_not_a_tile(patch(ptype="commitment", origin=None)) == DROP_ORPHAN


def test_person_patches_never_get_a_tile():
    assert why_not_a_tile(patch(ptype="person")) == DROP_PERSON


def test_sensitive_content_stays_off_the_home_quilt():
    assert why_not_a_tile(dict(patch(), sensitivity="phi")) == DROP_SENSITIVE
    assert why_not_a_tile(patch(sensitivity="private")) == DROP_SENSITIVE


def test_a_shelved_patch_does_not_come_back_as_a_tile():
    assert why_not_a_tile(patch(shelved_at="2026-08-30")) == DROP_SHELVED


def test_acted_on_items_belong_to_the_seam_not_the_week():
    assert why_not_a_tile(dict(patch(), completed_at="2026-08-30")) == DROP_RESOLVED


@pytest.mark.parametrize("text", [
    "Let's take that offline",
    "We should schedule a call about it",
    "Reschedule the sync",
])
def test_meeting_exhaust_is_dropped(text):
    assert why_not_a_tile(patch(text=text)) == DROP_EXHAUST


def test_a_real_fact_survives_pruning():
    assert why_not_a_tile(patch(text="Zero data retention with AI providers")) is None


# --------------------------------------------------------------------
# Scoring, section 6.1
# --------------------------------------------------------------------

def test_consequence_outranks_a_bare_event():
    assert salience(patch(ptype="decision")) > salience(patch(ptype="event"))


def test_recurrence_is_the_strongest_single_signal():
    # The spec calls it that, and it is already kept by the write path.
    base = salience(patch())
    once = salience(patch(restatement_count=1))
    twice = salience(patch(restatement_count=2))
    assert twice > once > base


def test_specificity_beats_vagueness():
    assert salience(patch(text="Achieve $3M ARR by Q4")) > \
           salience(patch(text="We should grow the business"))


def test_connectivity_counts():
    assert salience(patch(), edge_count=2) > salience(patch(), edge_count=0)


def test_a_live_old_commitment_beats_a_stale_fresh_takeaway():
    # Section 6.1's freshness rule, stated as the case it exists for.
    live_old = patch(ptype="commitment", deadline_date="2026-09-30")
    stale_new = dict(patch(ptype="takeaway"), decay_state="stale")
    assert salience(live_old, TODAY) > salience(stale_new, TODAY)


# --------------------------------------------------------------------
# The digest as a whole
# --------------------------------------------------------------------

def test_the_digest_never_pads_to_the_limit():
    # "Empty is a real state ... Do not pad." Only two survive pruning.
    cands = [patch("a"), patch("b", ptype="person"), patch("c", text="")]
    out = build_digest(cands, limit=6)
    assert len(out["patches"]) == 1
    assert out["dropped"] == {DROP_PERSON: 1, "no_text": 1}


def test_an_empty_candidate_set_is_an_empty_digest_not_an_error():
    out = build_digest([], limit=6)
    assert out["patches"] == [] and out["row_pairs"] == []


def test_the_order_is_stable_across_calls():
    # A tile that moved between two opens is the spec's stability defect.
    # Equal scores tiebreak on patch_id, so the sort cannot be unstable.
    cands = [patch(f"p{i}") for i in range(6)]
    first = [p["patch_id"] for p in build_digest(cands)["patches"]]
    again = [p["patch_id"] for p in build_digest(list(reversed(cands)))["patches"]]
    assert first == again


def test_salience_is_computed_but_kept_off_the_public_shape():
    # Underscore-prefixed: internal, for ordering and QA. Serving it
    # alongside an already-ordered array carries nothing the index does
    # not, and GhostPour declined to take it too.
    out = build_digest([patch("a")], limit=6)
    assert "_salience" in out["patches"][0]
    assert "salience" not in out["patches"][0]
