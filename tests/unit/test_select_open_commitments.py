"""Who gets the scarce injection slots, and why a sort key could not do it.

2026-08-19, live: a colleague asked in a meeting to close an item from
the previous day's call. The ingest did not detect it. Not an extraction
miss, the item was never in the prompt. The fetch ordered overdue first
then newest under a cap of 20, against 124 overdue commitments, so the
cap was spent before any non-overdue item was reached.

The measurements that shaped the fix, all from prod that day:

  376  open commitments for the user
  207  of them with NO deadline_date, so they can never become overdue
       and could never enter the injected set by either arm of the window
  124  overdue, against a cap of 20
  279  open commitments on ABM alone, 103 of them overdue

That last number is why this is a reserved share and not another ORDER BY
key: scoping to the meeting's project and keeping overdue-first still
fills every slot from the same project's overdue backlog.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.extraction_prompts import select_open_commitments


def c(pid):
    return {"patch_id": pid, "text": f"item {pid}", "created_at": None,
            "deadline_date": None}


def ids(rows):
    return [r["patch_id"] for r in rows]


def test_the_case_that_failed_in_the_room():
    """Yesterday's item on this project, against a backlog of overdue
    items that outnumbers the cap on its own. Before the fix it was
    unreachable at any cap the prompt could afford."""
    overdue_backlog = [c(f"old{i}") for i in range(124)]
    yesterday = c("rate-limit")
    got = select_open_commitments([yesterday], overdue_backlog, 20)
    assert "rate-limit" in ids(got)
    assert len(got) == 20


def test_the_project_share_is_reserved_not_preferred():
    """Half the cap, held for this meeting's project even when the
    general list could fill every slot by itself."""
    got = select_open_commitments([c(f"p{i}") for i in range(50)],
                                  [c(f"g{i}") for i in range(50)], 20)
    assert ids(got)[:10] == [f"p{i}" for i in range(10)]
    assert ids(got)[10:] == [f"g{i}" for i in range(10)]


def test_the_project_never_takes_more_than_its_share():
    """The inverse failure: a busy project starving the overdue list is
    the same bug pointed the other way."""
    got = select_open_commitments([c(f"p{i}") for i in range(50)], [], 20)
    assert len(got) == 10


def test_an_unused_project_share_goes_back_to_the_general_list():
    """A quiet project must not cost slots. Three project items and a
    cap of 20 still returns 20."""
    got = select_open_commitments([c("p0"), c("p1"), c("p2")],
                                  [c(f"g{i}") for i in range(50)], 20)
    assert len(got) == 20
    assert ids(got)[:3] == ["p0", "p1", "p2"]


def test_no_project_degrades_to_exactly_the_old_behaviour():
    """A meeting with no project (or a lane that passes none) must be
    unchanged, not merely similar."""
    general = [c(f"g{i}") for i in range(50)]
    assert ids(select_open_commitments([], general, 20)) == [f"g{i}" for i in range(20)]
    assert ids(select_open_commitments(None, general, 20)) == [f"g{i}" for i in range(20)]


def test_an_item_in_both_lists_is_paid_for_once():
    """Recent AND overdue is the common case for a live project. Counting
    it twice would silently shrink the cap."""
    dup = c("both")
    got = select_open_commitments([dup], [dup] + [c(f"g{i}") for i in range(50)], 20)
    assert len(got) == 20
    assert ids(got).count("both") == 1


def test_each_list_keeps_its_own_order():
    """The orders are the callers' rules (project newest-first, general
    overdue-first) and this function must not reorder either."""
    got = select_open_commitments([c("p2"), c("p0"), c("p1")],
                                  [c("g9"), c("g1")], 20)
    assert ids(got) == ["p2", "p0", "p1", "g9", "g1"]


def test_junk_and_edges_never_raise_on_the_ingest_path():
    """A raise here takes down extraction for the whole meeting."""
    assert select_open_commitments([], [], 20) == []
    assert select_open_commitments(None, None, 20) == []
    assert select_open_commitments([c("p")], [c("g")], 0) == []
    assert select_open_commitments([c("p")], [c("g")], -1) == []
    # A row with no patch_id cannot be deduped, so it is dropped rather
    # than admitted as an untrackable duplicate.
    assert select_open_commitments([{"text": "no id"}], [c("g")], 20) == [c("g")]


def test_an_odd_cap_favours_the_general_list():
    """cap // 2 on purpose: when the cap is odd the spare slot goes to
    the overdue side, which is the side with the older claim."""
    got = select_open_commitments([c(f"p{i}") for i in range(9)],
                                  [c(f"g{i}") for i in range(9)], 5)
    assert ids(got) == ["p0", "p1", "g0", "g1", "g2"]
