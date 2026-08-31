"""Sixty tiles, paged, and arranged so it reads as a quilt.

Scott raised the ceiling from 6 on 2026-08-31 after it was measured that
a real week holds 322 eligible tiles for the heaviest user and 125 for
the next, and that serving more costs NO model spend: nothing on the
read path calls an LLM and headlines are written once at ingest. He
then asked for the part this file mostly covers: "prioritize them
somehow, but show a mix so it is actually reminiscent of a quilt".

The answer is that those are TWO JOBS.

  WHICH patches  -> priority. Rank decides, always.
  WHERE they sit -> the quilt. Arrangement decides nothing about
                    whether a patch is shown, only where.

Doing both in one pass is what produced a block of one colour.
"""

import collections

import pytest

from contextquilt.services.woven_digest import (
    LAYOUTS,
    MAX_TILES,
    PROTOTYPE_MAX,
    build_digest,
    layout,
)


def patch(pid, ptype="takeaway", text="Ship the gateway", origin="m1"):
    return {"patch_id": pid, "patch_type": ptype, "origin_id": origin,
            "value": {"text": text}, "created_at": None}


def mixed(counts):
    out = []
    for ptype, n in counts.items():
        out += [patch(f"{ptype}{i:03d}", ptype, f"{ptype} number {i} here")
                for i in range(n)]
    return out


def neighbours(out):
    """Same-type pairs that actually TOUCH in a rendered row.

    Rows are runs of consecutive positions, so this is the only measure
    that matches what the eye sees. Counting per-page type totals would
    call a page well mixed while it rendered as two blocks.
    """
    return sum(1 for row in out["row_pairs"] for a, b in zip(row, row[1:])
               if out["patches"][a]["patch_type"] == out["patches"][b]["patch_type"])


# --------------------------------------------------------------------
# The layout, now generated above six
# --------------------------------------------------------------------

@pytest.mark.parametrize("n", range(1, 61))
def test_every_row_fills_exactly_six_columns(n):
    """The invariant the whole grid rests on.

    A row summing to 5 or 7 is a ragged quilt, and it is the failure the
    original table was hand-verified against. Generated rows get the
    same check rather than being trusted because a loop wrote them.
    """
    plan = layout(n)
    for row in plan["rows"]:
        assert sum(plan["spans"][i] for i in row) == 6, f"{n}: row {row}"


@pytest.mark.parametrize("n", range(1, 61))
def test_rows_partition_every_tile_exactly_once_in_order(n):
    plan = layout(n)
    assert [i for row in plan["rows"] for i in row] == list(range(n))
    assert len(plan["spans"]) == len(plan["heights"]) == n


@pytest.mark.parametrize("n", range(1, 61))
def test_a_row_has_one_height(n):
    # Mixed heights inside a row is a visibly broken grid rather than a
    # subtle one, so it is worth pinning at every size.
    plan = layout(n)
    for row in plan["rows"]:
        assert len({plan["heights"][i] for i in row}) == 1, f"{n}: row {row}"


@pytest.mark.parametrize("n", range(1, 7))
def test_the_prototype_sizes_are_untouched(n):
    """1 through 6 are the design's, not generated.

    The prototype is authoritative about what the screen looks like, and
    upstream prompt caching depends on recall output being byte-stable,
    so the shape that ships today must not move because a generator was
    added above it.
    """
    assert layout(n) == LAYOUTS[n]
    assert PROTOTYPE_MAX == 6


def test_the_ceiling_is_sixty_and_holds():
    assert MAX_TILES == 60
    assert len(layout(60)["spans"]) == 60
    assert len(layout(999)["spans"]) == 60


def test_no_row_shape_repeats_three_times_running():
    # Section 6.1's rhythm argument applied down the scroll rather than
    # across one screen. Three identical rows in a row is a ladder.
    shapes = [tuple(layout(60)["spans"][i] for i in row)
              for row in layout(60)["rows"]]
    for i in range(len(shapes) - 2):
        assert not (shapes[i] == shapes[i + 1] == shapes[i + 2]), shapes[i:i + 3]


# --------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------

def test_paging_is_a_partition_with_no_repeats_and_no_gaps():
    cands = mixed({"commitment": 40, "blocker": 12, "decision": 8, "takeaway": 6})
    seen, offset = [], 0
    while True:
        out = build_digest(cands, limit=6, offset=offset)
        if not out["patches"]:
            break
        seen += [p["patch_id"] for p in out["patches"]]
        offset += 6
        if not out["has_more"]:
            break
    assert len(seen) == len(set(seen)) == 66


def test_total_available_counts_what_earned_a_tile_not_what_was_fetched():
    """The honest denominator for "showing 6 of 322".

    Counting raw candidates would include rows the quilt can never show
    and would promise a scroll that ends early.
    """
    cands = mixed({"commitment": 10})
    cands += [patch("person1", "person"), patch("orphan", "event", origin=None)]
    out = build_digest(cands, limit=6)
    assert out["total_available"] == 10
    assert out["dropped"]


def test_has_more_is_false_on_the_last_page():
    out = build_digest(mixed({"commitment": 10}), limit=6, offset=6)
    assert len(out["patches"]) == 4
    assert out["has_more"] is False
    assert out["offset"] == 6


def test_an_offset_past_the_end_is_empty_rather_than_an_error():
    out = build_digest(mixed({"commitment": 3}), limit=6, offset=99)
    assert out["patches"] == [] and out["has_more"] is False


# --------------------------------------------------------------------
# The quilt: arrangement
# --------------------------------------------------------------------

@pytest.mark.parametrize("limit", [6, 12, 24, 30, 60])
def test_touching_tiles_differ_in_type_whenever_the_material_allows(limit):
    """The whole of Scott's ask, measured the way the eye sees it.

    Counts alone do not make a quilt: a page can be well mixed by the
    numbers and still render as blocks, because what shows is
    neighbours.
    """
    cands = mixed({"commitment": 40, "blocker": 20, "decision": 15,
                   "takeaway": 12, "goal": 8, "constraint": 5})
    assert neighbours(build_digest(cands, limit=limit)) == 0


def test_a_single_type_week_is_not_shuffled_for_the_sake_of_it():
    # Nothing to contrast with, so rank order stands. A quilt that
    # rearranged identical tiles would be losing information to gain
    # nothing.
    out = build_digest(mixed({"commitment": 6}), limit=6)
    assert [p["patch_id"] for p in out["patches"]] == [f"commitment{i:03d}"
                                                       for i in range(6)]


def test_arrangement_never_changes_which_patches_are_shown():
    """Priority owns membership; arrangement owns position only.

    This is the property that makes it safe to permute a page at all.
    """
    cands = mixed({"commitment": 20, "blocker": 10, "decision": 6})
    out = build_digest(cands, limit=12)
    ids = {p["patch_id"] for p in out["patches"]}
    assert len(ids) == 12
    # Every shown patch is one that qualified, none invented, none dropped.
    assert ids <= {c["patch_id"] for c in cands}


def test_the_mix_survives_all_the_way_down_the_scroll():
    """The cap is per PAGE, which is what makes it work at 60 as at 6.

    A cap on the whole selection is met trivially by a long list and
    does nothing.
    """
    cands = mixed({"commitment": 60, "blocker": 20, "decision": 10})
    for offset in (0, 6, 12, 18, 24):
        out = build_digest(cands, limit=6, offset=offset)
        counts = collections.Counter(p["patch_type"] for p in out["patches"])
        assert max(counts.values()) <= 3, (offset, counts)


def test_the_arrangement_is_deterministic():
    # Recall output must stay byte-stable within a UTC day because
    # upstream prompt caching depends on it, and a tile that moved
    # between two identical requests is that bug arriving by the back
    # door.
    cands = mixed({"commitment": 20, "blocker": 12, "decision": 9, "goal": 4})
    first = [p["patch_id"] for p in build_digest(cands, limit=30)["patches"]]
    for _ in range(5):
        assert [p["patch_id"] for p in build_digest(cands, limit=30)["patches"]] == first


def test_ties_break_on_salience_rather_than_the_alphabet():
    """The stripe bug, caught on real data rather than in this file.

    With fifteen types each contributing a couple of tiles, every group
    count tied, and sorting the type NAME cycled blocker, commitment,
    constraint, decision, deliverable, event in alphabetical order
    forever. Regular, and the opposite of a quilt.
    """
    cands = mixed({"aaa": 3, "zzz": 3})
    # zzz carries the more consequential text, so it should not always
    # lose to a name that sorts earlier.
    for c in cands:
        if c["patch_type"] == "zzz":
            c["patch_type"] = "commitment"      # scores higher than a bare type
    order = [p["patch_type"] for p in build_digest(cands, limit=6)["patches"]]
    assert order[0] == "commitment", order


def test_the_service_clamps_the_limit_rather_than_trusting_its_caller():
    """The route is not the only caller, and the failure would be silent.

    `zip` truncates the tiles to the layout's length while `row_pairs`
    would still describe the longer list, so the grid would reference
    positions that were never served: a layout that is wrong rather than
    absent, which is the shape SS built a tripwire for.
    """
    out = build_digest(mixed({"commitment": 80, "blocker": 40}), limit=500)
    assert len(out["patches"]) == MAX_TILES
    assert [i for row in out["row_pairs"] for i in row] == list(range(MAX_TILES))
