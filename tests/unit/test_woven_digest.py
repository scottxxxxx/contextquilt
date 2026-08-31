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
    DROP_NO_HEADLINE,
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
    layout,
    row_spans_exact,
    row_is_exact,
    row_pairs,
    salience,
    stitch_label,
    STITCH_LABEL_MAX,
    why_not_a_tile,
)

TODAY = date(2026, 8, 31)


def patch(pid="p1", ptype="takeaway", text="Ship the gateway", origin="m1", **value):
    # A tile without a headline is not a tile, so the helper supplies
    # one. A test about its ABSENCE passes headline=None explicitly.
    value.setdefault("headline", text[:40])
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


def test_tile_size_is_decorative_and_that_is_recorded():
    """In the prototype, size does NOT encode importance.

    This test asserted the opposite until the prototype was read. The
    spans are a fixed pattern applied by POSITION, so the fourth tile is
    larger than the first, and any client reading tile size as a ranking
    signal will be wrong. Section 5 still holds: it says index 0 is the
    strongest and takes the FIRST tile, not the biggest.

    Kept rather than deleted, inverted, because "this used to say the
    opposite and here is why it changed" is what a future reader needs
    before they change it back.
    """
    weights = assign_weights(6)
    assert weights != sorted(weights, reverse=True)
    assert weights[3] > weights[0]


def test_a_single_tile_fills_the_row():
    # Thin weeks reach the client because the spec forbids padding, and
    # a lone tile at any narrower span leaves the rest of the row empty
    # beside it, which reads as a missing tile rather than a thin week.
    assert layout(1)["spans"] == [6]


def test_index_zero_is_the_first_tile():
    # Section 5's actual promise, which survives the prototype's layout.
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
    """Rule 7, committed by this test's own author, in this test.

    The name said the score is kept OFF the public shape. The comment
    said GhostPour had declined to take it. The assertion required it to
    be PRESENT, and so the field shipped on the wire for an evening
    while a green test with the right name sat over it.

    Nobody opened it, because the name already said the reassuring
    thing. GP found the field by forwarding it verbatim and noticing
    that two teams had agreed it would not be there, which is the only
    vantage point from which the contradiction was visible at all.

    A comment cannot be wrong out loud. A name can, and this one was.
    """
    out = build_digest([patch("a")], limit=6)
    assert "_salience" not in out["patches"][0]
    assert "salience" not in out["patches"][0]


# --------------------------------------------------------------------
# Stitch labels (section 6.4)
# --------------------------------------------------------------------

def test_a_label_keeps_the_concrete_number():
    """The bug this test exists for: a bare hyphen is not a boundary.

    An early version broke on any "-" and turned "60-67% small firms"
    into "Target market of 60", destroying the figure that both 6.3 and
    6.4 say to keep. Only an em dash or a SPACED hyphen separates
    clauses; an unspaced one is inside a number or a compound word.
    """
    assert stitch_label("Target market of 60-67% small firms is ideal") \
        == "Target market of 60-67%"


def test_a_label_stops_at_a_clause_boundary_rather_than_a_character_count():
    assert stitch_label("Data security and privacy: zero data retention") \
        == "Data security"
    assert stitch_label("Zero data retention - no exceptions") \
        == "Zero data retention"


def test_a_label_never_ends_on_a_dangling_word():
    """Cutting mid-phrase is unavoidable without a model. Ending on a
    conjunction is not, and it is the difference between a short label
    and a broken one, which is the same objection as a trailing
    ellipsis."""
    for text in ("Competitors are shipping insecure implementations of it",
                 "Data security and privacy concerns raised by the client",
                 "A plan for the migration and the rollout and the rest"):
        label = stitch_label(text)
        assert label.split()[-1].lower() not in {
            "and", "or", "of", "the", "a", "an", "to", "for", "with"}, label


def test_a_label_never_carries_an_ellipsis():
    long_text = "Achieve three million in annual recurring revenue by scaling"
    assert "..." not in stitch_label(long_text)
    assert "…" not in stitch_label(long_text)


def test_labels_respect_the_length_cap():
    for text in ("x" * 200, "Camino Caseworks business plan documenting scope",
                 "Short one"):
        assert len(stitch_label(text)) <= STITCH_LABEL_MAX


def test_an_empty_patch_yields_an_empty_label_rather_than_raising():
    assert stitch_label("") == ""
    assert stitch_label(None) == ""


# --------------------------------------------------------------------
# JSONB arrives as a STRING. This emptied the quilt for every user.
# --------------------------------------------------------------------

def test_a_json_string_value_is_parsed_not_dropped():
    """The bug that rendered an empty quilt for everyone.

    `value` is JSONB and asyncpg returns it as a JSON STRING unless a
    codec is registered, which this pool does not do. An earlier version
    checked `isinstance(value, dict)` and returned empty for anything
    else, so EVERY patch dropped as `no_text`.

    Caught on real data in one run, and only because `dropped` reports
    which rule fired: 351 candidates, 351 `no_text`. Without that map it
    would have been an empty screen with no explanation.
    """
    import json as _json
    raw = {"patch_id": "a", "patch_type": "takeaway", "origin_id": "m1",
           "value": _json.dumps({"text": "Zero data retention",
                                 "headline": "Zero retention"})}
    assert why_not_a_tile(raw) is None
    out = build_digest([raw], limit=6)
    assert len(out["patches"]) == 1
    assert out["patches"][0]["fact"] == "Zero data retention"


def test_every_value_field_survives_the_string_form():
    # Not just `text`: the shelve stamp, the sensitivity flag and the
    # recurrence counter all live in the same blob, and reading them
    # through a different path is how one of them gets missed.
    import json as _json
    shelved = {"patch_id": "s", "patch_type": "takeaway", "origin_id": "m",
               "value": _json.dumps({"text": "x", "shelved_at": "2026-08-30"})}
    assert why_not_a_tile(shelved) == DROP_SHELVED

    recurring = {"patch_id": "r", "patch_type": "takeaway", "origin_id": "m",
                 "value": _json.dumps({"text": "x", "restatement_count": 2})}
    plain = {"patch_id": "p", "patch_type": "takeaway", "origin_id": "m",
             "value": _json.dumps({"text": "x"})}
    assert salience(recurring) > salience(plain)


@pytest.mark.parametrize("bad", ["not json at all", "[1,2,3]", "null", ""])
def test_an_unparseable_value_drops_cleanly_rather_than_raising(bad):
    # A malformed blob must cost that patch a tile, never the request.
    assert why_not_a_tile({"patch_id": "x", "patch_type": "takeaway",
                           "origin_id": "m", "value": bad}) == "no_text"


def test_one_type_does_not_take_the_whole_quilt():
    """Type rhythm, and it is a judgment call rather than a spec rule.

    Section 6.1 caps weight-3 tiles at two "so the quilt has rhythm",
    which establishes visual rhythm as a legitimate selection concern.
    Colour is the same argument: type drives the fabric hue, so an
    unconstrained ranking on a commitment-heavy week returns five
    commitments and the quilt renders as one purple block. Measured on
    real data the top six were four commitments and two blockers.
    """
    cands = [patch(f"c{i}", ptype="commitment", text=f"Ship thing {i}")
             for i in range(6)]
    cands += [patch(f"d{i}", ptype="decision", text=f"Decide thing {i}")
              for i in range(3)]
    types = [p["patch_type"] for p in build_digest(cands, limit=6)["patches"]]
    # Two types and six tiles: the honest spread is three and three.
    assert types.count("commitment") == 3 and types.count("decision") == 3


def test_the_cap_is_the_even_share_rather_than_a_flat_two():
    """The bug the first version of this shipped with.

    A flat cap of two deferred four commitments on a two-type week and
    then backfilled two of them, so the output was four commitments and
    the cap had done nothing but shuffle. A cap the material cannot meet
    is not a cap. With three types the even share IS two.
    """
    cands = [patch(f"c{i}", ptype="commitment", text=f"Ship {i}") for i in range(6)]
    cands += [patch(f"d{i}", ptype="decision", text=f"Decide {i}") for i in range(3)]
    cands += [patch(f"b{i}", ptype="blocker", text=f"Blocked on {i}") for i in range(3)]
    counts = {}
    for p in build_digest(cands, limit=6)["patches"]:
        counts[p["patch_type"]] = counts.get(p["patch_type"], 0) + 1
    assert max(counts.values()) == 2 and len(counts) == 3


def test_a_single_type_week_still_fills_rather_than_padding():
    """The cap yields rather than reaching for weaker material.

    An honest monochrome quilt beats a decorative one built from patches
    that did not earn a tile, so if the week genuinely holds one type,
    the tiles are that type in rank order.
    """
    cands = [patch(f"c{i}", ptype="commitment", text=f"Ship thing {i}")
             for i in range(6)]
    out = build_digest(cands, limit=6)
    assert len(out["patches"]) == 6
    assert {p["patch_type"] for p in out["patches"]} == {"commitment"}


def test_the_cap_never_promotes_an_unqualified_patch():
    # Strictly a tie-break among patches that already passed pruning.
    cands = [patch(f"c{i}", ptype="commitment", text=f"Ship {i}") for i in range(4)]
    cands.append(patch("bad", ptype="decision", text=""))     # no text
    types = [p["patch_type"] for p in build_digest(cands, limit=6)["patches"]]
    assert "decision" not in types


def test_the_quilt_still_fills_when_the_variety_runs_out():
    """The backfill, which sabotage found had NO test at all.

    Removing the backfill entirely left the suite green, and per our own
    rule that surprise is the finding rather than a compliment. The
    one-type test never reaches this code: with a single type the cap
    becomes the limit, so nothing is ever deferred.

    The case that DOES reach it is uneven material. Five commitments and
    one decision, six slots, cap three: the loop takes three commitments
    and the one decision and runs out of variety at four. Without the
    backfill the user gets a four-tile quilt while two qualified patches
    sit unused, which is the cap causing exactly the padding-in-reverse
    it was supposed to prevent.
    """
    cands = [patch(f"c{i}", ptype="commitment", text=f"Ship {i}") for i in range(5)]
    cands += [patch("d0", ptype="decision", text="Decide the pricing")]
    out = build_digest(cands, limit=6)
    assert len(out["patches"]) == 6, "the quilt went short when variety ran out"
    types = [p["patch_type"] for p in out["patches"]]
    assert types.count("commitment") == 5 and types.count("decision") == 1


def test_a_patch_with_no_headline_never_becomes_a_tile():
    """The ruling that settled a real disagreement with ShoulderSurf.

    Section 6.3 refuses an invalid headline rather than repairing it,
    because every repair is a truncation. That rule was about the
    WRITER, and it left the reader's case unstated, so CQ and SS filled
    the gap in opposite directions: CQ said render `fact`, SS's
    renderer skipped the patch. At six tiles that was a one-tile
    disagreement. At sixty with one in five null it is twelve.

    SS's objection decides it: `fact` is unbounded and a tile is
    stamp-sized, so rendering it makes the RENDERER cut the sentence,
    which is the forbidden truncation through a different door. And
    skipping client-side is wrong too, because `total_available` would
    promise tiles that never appear.

    So the selection refuses instead, the contract is one sentence long
    (every tile served has a headline), and the loss is COUNTED in
    `dropped` where it creates pressure to improve the writer rather
    than silently thinning somebody's quilt.
    """
    out = build_digest([
        patch("a", text="Target market is small firms", headline="Small firms win"),
        patch("b", text="Privacy will be the differentiator", headline=None),
    ], limit=6)
    assert [p["patch_id"] for p in out["patches"]] == ["a"]
    assert out["patches"][0]["headline"] == "Small firms win"
    assert out["dropped"] == {DROP_NO_HEADLINE: 1}


def test_every_served_tile_carries_a_headline():
    # The contract, asserted as a sweep rather than case by case.
    out = build_digest(
        [patch(f"p{i}", text=f"Thing number {i}") for i in range(6)]
        + [patch("bare", text="No line for this one", headline=None)], limit=6)
    assert all(p["headline"] for p in out["patches"])


def test_a_headline_arriving_as_a_json_string_value_still_serves():
    # The JSONB trap again, on the newest field rather than the oldest.
    import json as _json
    p = patch("a", text="Zero data retention", headline="Zero retention")
    p["value"] = _json.dumps(p["value"])
    assert build_digest([p], limit=6)["patches"][0]["headline"] == "Zero retention"


def test_no_internal_field_travels_at_all():
    """The sweep, rather than one field by name.

    `_salience` was caught because someone remembered it. A list of
    known internals is a list somebody must maintain, and the entry that
    gets forgotten is the one whose presence is silent, so this asserts
    the CONVENTION instead: nothing underscore-prefixed goes out.
    """
    out = build_digest([patch("a", text="Ship the gateway")], limit=6)
    served = out["patches"][0]
    assert not any(k.startswith("_") for k in served), (
        f"internal fields on the wire: "
        f"{[k for k in served if k.startswith('_')]}"
    )
