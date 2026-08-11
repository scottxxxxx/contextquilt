"""Source-level guards for the person-detail completion history legs.

"What did this person actually deliver" is answerable ONLY here: the
app's sync keeps no tombstone memory (ids in `deleted` are removed and
nothing remembers them), so the client cannot reconstruct history, and
recall filters to active, so chat cannot see it either. The claims these
guards pin are the ones that would fail silently.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
MAIN = (SRC / "main.py").read_text()


def test_history_is_completed_only():
    """Decayed / archived-without-completion rows must never appear:
    "expired" is not a claim anyone completed anything. The population
    query gates on completed_at, the one column all three close lanes
    share and decay never sets."""
    m = re.search(r"completed_rows = await conn\.fetch\(\s*\"\"\"(.*?)\"\"\"", MAIN, re.DOTALL)
    assert m, "completed history query not found"
    # The OUTER where (the last one; the owns-edge subquery has its own,
    # which legitimately filters connection status).
    outer_where = m.group(0).split("WHERE")[-1]
    assert "completed_at IS NOT NULL" in outer_where
    assert "cp.status" not in outer_where, (
        "gate on completed_at, not status: archived-without-completion is decay"
    )


def test_only_entity_scoped_callers_pay_for_history():
    """The list assembles every person; fetching the full completed
    population there would cost every list call for data no row
    renders. The opt-in sites are both ENTITY-SCOPED: the person detail
    route and the people-scoped recall lane (each passes explicit
    entity ids into _people_core). The list route must never opt in."""
    assert MAIN.count("include_completed=True") == 2
    list_body = re.search(
        r"async def list_people\(.*?\nasync def ", MAIN, re.DOTALL
    )
    assert list_body and "include_completed" not in list_body.group(0)


def test_the_cap_self_describes():
    """{"total": N, "items": [...]}: a bare capped array reads as "this
    is everything", the silent truncation the quilt coverage line
    exists to prevent."""
    m = re.search(r"def _history\(rows\):.*?\n        \}", MAIN, re.DOTALL)
    assert m, "_history() not found"
    assert '"total": len(rows)' in m.group(0)
    assert "COMPLETED_HISTORY_CAP" in m.group(0)


def test_done_items_carry_the_closure_facts():
    m = re.search(r"def _done_item\(r\):.*?\n        \}", MAIN, re.DOTALL)
    assert m, "_done_item() not found"
    for field in ('"completed_at"', '"completion_source"', '"completion_evidence"'):
        assert field in m.group(0), f"{field} missing from history items"


def test_completed_items_report_no_decay_band():
    """Decay does not apply to a completed row; null means not tracked,
    and serving a band would be a claim the decay loop will never act
    on."""
    assert '{"decay_state": None}' in MAIN


def test_completed_you_owe_keeps_the_null_semantics():
    """Same rules as the open you_owe: null when the caller's manifest
    lacks owed_to or the person has no patch, a real (possibly empty)
    object once both hold. 0 delivered and cannot-tell are different
    claims."""
    m = re.search(r'"completed_you_owe": \((.*?)\),\s*\n\s*\},', MAIN, re.DOTALL)
    assert m, "completed_you_owe rendering not found"
    assert "None if" in m.group(0)
