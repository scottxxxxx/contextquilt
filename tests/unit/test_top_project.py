"""`top_project` is the first element of the projects rollup, not a
separate maximum.

That distinction is the whole design. The list row and the detail route
describe the same fact, so they have to agree about which project leads,
and the cheapest way to guarantee that is to reuse one ordering rather
than compute a maximum twice. These tests pin the ordering contract that
makes `projects[0]` a safe definition of "top".
"""

from contextquilt.services.people_identity import merge_project_rollups


def _observed(name, n, pid=None):
    return {"project_id": pid, "project": name, "meeting_count": n}


def _stated(name, pid=None):
    return {"project_id": pid, "project": name}


def top(observed=(), stated=()):
    rows = merge_project_rollups(observed, stated)
    return rows[0] if rows else None


def test_most_co_attended_project_leads():
    t = top([_observed("Atlas Migration", 5), _observed("Vendor Eval", 1)])
    assert t["project"] == "Atlas Migration"
    assert t["meeting_count"] == 5


def test_ties_break_on_name_so_the_row_never_reshuffles():
    """A browse surface polled twice must not swap its subtitle. The tie
    break is alphabetical rather than arbitrary for exactly that reason."""
    a = top([_observed("Zebra", 3), _observed("Alpha", 3)])
    b = top([_observed("Alpha", 3), _observed("Zebra", 3)])
    assert a["project"] == "Alpha" and b["project"] == "Alpha"


def test_case_does_not_affect_the_tie_break():
    a = top([_observed("beta", 2), _observed("Alpha", 2)])
    assert a["project"] == "Alpha"


def test_an_observed_project_outranks_a_stated_one():
    """A `works_on` edge is somebody SAYING they are on a project and may
    have no co-attended meeting at all. It carries meeting_count 0, so a
    project they were actually in the room for leads."""
    t = top([_observed("Atlas Migration", 1)], [_stated("Aspirational Project")])
    assert t["project"] == "Atlas Migration"
    assert t["observed"] is True


def test_stated_only_still_produces_a_subtitle():
    """Better to say "Atlas Migration" than nothing when that is genuinely
    all CQ knows. The flags let the client tell the two apart."""
    t = top([], [_stated("Atlas Migration")])
    assert t["project"] == "Atlas Migration"
    assert t["meeting_count"] == 0
    assert t["stated"] is True and t["observed"] is False


def test_no_projects_means_no_subtitle():
    """Null, not an invented placeholder. A row with nothing behind it
    says so; "0 projects" is not a sentence."""
    assert top([], []) is None


def test_top_is_literally_the_first_row_of_the_rollup():
    """The invariant the endpoint depends on. If this ever fails, the list
    row and the detail route have started disagreeing."""
    observed = [_observed("B", 2), _observed("A", 9), _observed("C", 2)]
    stated = [_stated("D")]
    rows = merge_project_rollups(observed, stated)
    # Equality, not identity: each call builds a fresh list. What has to
    # hold is that the endpoint's `projects[0]` and this rollup's leader
    # are the same ROW, not the same object.
    assert top(observed, stated) == rows[0]
    assert rows[0]["project"] == "A"


def test_same_project_seen_both_ways_is_one_row_carrying_both_flags():
    """Otherwise a person on a project they also talked about would show
    it twice, and the subtitle would be picking between duplicates."""
    rows = merge_project_rollups(
        [_observed("Atlas Migration", 4, pid="p1")], [_stated("Atlas Migration", pid="p1")]
    )
    assert len(rows) == 1
    assert rows[0]["observed"] is True and rows[0]["stated"] is True
    assert rows[0]["meeting_count"] == 4


def test_counts_for_one_project_accumulate_before_ranking():
    """Appearances arrive per meeting, so the rollup sums them. Ranking on
    an unsummed row would let a fragmented project lose to a smaller one."""
    rows = merge_project_rollups(
        [_observed("Atlas", 2, pid="p1"), _observed("Atlas", 3, pid="p1"),
         _observed("Other", 4, pid="p2")],
        [],
    )
    assert rows[0]["project"] == "Atlas" and rows[0]["meeting_count"] == 5
