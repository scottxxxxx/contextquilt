"""A cap that bites has to say so.

GhostPour, from the far side: they pass a `limit`, build a cross-meeting
topic tracker on the result, and "a silent cap means a busy project
undercounts and the artifact is confidently wrong about the one thing it
exists to measure."

They believed the cap was ours. It is not: `limit` is a caller-supplied
query param, ge=1 le=500, defaulting to NO cap. So they were truncating
their own dossier and then measuring against the truncated set, which is
the worst version of this because nothing on either side was lying.

Recall already solved its own truncation with a coverage line (contract
commitment E, always on). This is the same principle, structured,
because this surface is read by code rather than by a model.
"""

import pathlib

MAIN = pathlib.Path("src/main.py").read_text()


def test_the_response_can_report_truncation():
    assert "truncated: Optional[bool] = None" in MAIN
    assert "total_available: Optional[int] = None" in MAIN


def test_the_total_is_counted_before_the_cap():
    """Counting after would return the capped number and agree with
    itself no matter what was dropped."""
    body = MAIN.split("if limit:")[1][:400]
    assert "count(*) FROM (" in body
    assert body.index("count(*) FROM (") < body.index("LIMIT $")


def test_a_caller_that_passes_no_limit_pays_nothing():
    """The extra COUNT query still runs only when a cap was actually
    passed. That was always the real cost concern, and it is unchanged:
    an uncapped read gets its total from the rows it already fetched."""
    assert "total_available: Optional[int] = None\n    if limit:" in MAIN


def test_the_total_is_present_even_when_nothing_capped():
    """Changed 2026-08-19 on GP's ask, and the reason is better than the
    convenience. The one field that says "you are seeing a partial view"
    used to disappear exactly when a caller stopped asking for a partial
    view. So the day anything SERVER side caps a response, a consumer
    that had been reading total_available sees absence, which is the
    same thing it has always seen, and has no way to tell the two apart.
    Absence cannot carry that news; a number can.

    Free on an uncapped read: the rows fetched ARE the population."""
    body = MAIN.split("rows = await db_pool.fetch(query, *params)")[1][:900]
    assert "if total_available is None:" in body
    assert "total_available = len(rows)" in body


def test_truncated_is_a_comparison_not_an_assumption():
    """Passing limit=500 against 12 patches is not truncation. The flag
    reports whether the cap BIT, not whether one was supplied, and on an
    uncapped read it is a true False rather than an absence."""
    assert "truncated=total_available > len(rows)," in MAIN
