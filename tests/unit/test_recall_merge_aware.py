"""Guard: recall's entity fetch resolves merged entities forward.

A merge marks the folded entity with a forward pointer instead of
deleting it, and the dead row keeps its own name AND its own description.
Recall matched that row by name, so after merging four spellings of one
person the header rendered:

    People: Vijay Rayudu (Participant...); Vijay R (Platform and product
    coordination); Vijay Rayud

and the model was told one human was three. That is exactly the split
brain a merge exists to resolve, surviving in the recall lane because
only the WRITE path hopped the pointer
(`worker._resolve_merged_forward`). Two predicates for one concept, and
only one of them maintained.

Source-level rather than behavioural, following
`test_connection_status_filter.py`: the logic is entirely SQL and there
is no local Postgres in this suite. It cannot prove the resolution is
correct, only that the recall read is not quietly reverted to a plain
name match, which is the regression worth catching. Correctness was
verified against production and against fabricated chain, cycle and
missing-alias rows on 2026-08-07.
"""

import pathlib
import re

MAIN = pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py"


def _recall_entity_query() -> str:
    """The SQL literal that turns matched names into entity rows.

    Anchored on `entity_rows = await db_pool.fetch(` inside recall, and
    bounded generously, because the statement spans a CTE.
    """
    text = MAIN.read_text()
    m = re.search(r"entity_rows = await db_pool\.fetch\(", text)
    assert m, "recall's entity fetch moved or was renamed; update this guard"
    return text[m.start(): m.start() + 2600]


def test_recall_entity_fetch_hops_the_merge_pointer():
    """The regression: a plain name match returns folded rows."""
    q = _recall_entity_query()
    assert "merged_into" in q, (
        "recall's entity fetch no longer references merged_into. A merged-away "
        "entity keeps its name and description, so recall will render one "
        "person as several."
    )


def test_resolution_is_recursive_not_one_hop():
    """A into B into C. One hop lands on B, which is itself merged away,
    so the header would still name a dead identity."""
    q = _recall_entity_query()
    assert "RECURSIVE" in q.upper()


def test_recursion_is_depth_capped():
    """Merge cycles are corrupt data the write path already logs
    (`entity_merge_cycle`). A read on the hot path must terminate on them
    rather than spin, and degrade to something rather than fail recall."""
    q = _recall_entity_query()
    assert re.search(r"depth\s*<\s*\d+", q), "no depth cap on the merge walk"


def test_output_ordering_is_deterministic():
    """All recall output must stay byte-stable within a UTC day because
    upstream prompt caching depends on it. An unordered entity set would
    reshuffle the People header between identical calls."""
    q = _recall_entity_query()
    assert "ORDER BY e.entity_id" in q


def test_it_resolves_rather_than_merely_excluding_folded_rows():
    """Excluding folded rows would usually work, because a merge records
    the loser's name as an alias on the survivor. Usually is not a
    guarantee: if that alias is missing, excluding DROPS the match
    entirely while resolving substitutes the survivor.

    Pinned because "just add `WHERE merged_into IS NULL`" is the obvious
    simplification and it is the wrong one.
    """
    q = _recall_entity_query()
    assert "survivor" in q.lower(), "expected a forward-resolution step"
    # The naive filter, applied to the OUTER select over matched names,
    # is what this test exists to prevent.
    assert not re.search(r"WHERE\s+e\.merged_into\s+IS\s+NULL", q, re.I)


def test_the_write_path_helper_still_exists():
    """The read and write sides now make the same claim in two places.
    If the write-side helper is ever removed or renamed, this guard's
    reasoning needs revisiting rather than silently rotting."""
    worker = (pathlib.Path(__file__).resolve().parents[2] / "src" / "worker.py").read_text()
    assert "_resolve_merged_forward" in worker
