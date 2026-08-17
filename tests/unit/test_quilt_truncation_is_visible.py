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
    """The extra count query runs only when a cap was actually passed:
    a caller that never limits should not fund a question it did not
    ask, and should see no change in the response either."""
    assert "total_available: Optional[int] = None\n    if limit:" in MAIN
    assert "truncated=(None if total_available is None" in MAIN


def test_truncated_is_a_comparison_not_an_assumption():
    """Passing limit=500 against 12 patches is not truncation. The flag
    reports whether the cap BIT, not whether one was supplied."""
    assert "else total_available > len(rows))" in MAIN
