"""The project-header contract: three triage fields, and opt-in ranking.

Measured on ABM the day this shipped: 472 open items carrying 99
overdue, 55 high salience and 23 restated, and NONE of those three
reached a client. A capped rundown also returned an arbitrary N rather
than the important N, so a client asking for the top 40 by recency
would have surfaced almost none of the 99 overdue ones.

main.py is not importable here (no fastapi locally, see CLAUDE.md), so
the endpoint guards are source-level assertions, same style as
test_people_network.py.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

MAIN = (ROOT / "src" / "main.py").read_text()


def test_the_three_triage_fields_are_declared():
    for field in ("overdue_since", "salience", "restatement_count"):
        assert f"{field}: Optional[" in MAIN, f"{field} missing from the model"


def test_the_three_triage_fields_are_actually_populated():
    """Declared but never assigned is the silent half of this bug: the
    key ships as null forever and reads as 'none of these exist', which
    is exactly how the 99 overdue items stayed invisible."""
    assert 'overdue_since=value.get("overdue_since")' in MAIN
    assert 'salience=value.get("salience")' in MAIN
    assert "restatement_count=_as_optional_int(" in MAIN


def test_attention_order_is_opt_in_and_recency_stays_default():
    """GP reads this endpoint into a prompt. Reordering underneath a
    caller who never asked would change their bytes for free."""
    assert 'elif order == "attention":' in MAIN
    assert 'query += " ORDER BY cp.created_at DESC, cp.patch_id ASC"' in MAIN


def test_meeting_view_keeps_capture_order():
    """origin_id is a browse surface under the SS contract, and branch
    order is what guarantees it wins over any ranking."""
    i_origin = MAIN.index('if origin_id:\n        query += " ORDER BY cp.created_at ASC')
    i_attn = MAIN.index('elif order == "attention":')
    assert i_origin < i_attn, "attention must not preempt the meeting view"


def _attention_block():
    return MAIN.split('elif order == "attention":')[1].split("else:")[0]


def test_attention_ranks_overdue_then_salience_then_recurrence_then_due():
    block = _attention_block()
    clauses = [
        "overdue_since' IS NOT NULL",
        "salience' = 'high'",
        "restatement_count' ~ '^[1-9]'",
        "deadline_date'",
    ]
    for c in clauses:
        assert c in block, f"missing rank clause: {c}"
    pos = [block.index(c) for c in clauses]
    assert pos == sorted(pos), "rank clauses are out of priority order"


def test_attention_order_casts_nothing():
    """A ::int on restatement_count raises on a hand written value and
    takes down a read route for whoever holds one bad row. The regex
    answers 'has this come back' without trusting the contents, and ISO
    dates sort correctly as text."""
    block = _attention_block()
    assert "::int" not in block
    assert "::date" not in block


def test_attention_order_is_deterministic():
    """Microsecond-equal batch inserts gave undefined order before the
    patch_id tiebreak. A ranked list needs it more, not less."""
    assert "cp.patch_id ASC" in _attention_block()


def test_unknown_order_is_rejected_not_silently_ignored():
    """`order=priority` would otherwise return a plausible list in the
    wrong order, with a 200, and nothing to notice."""
    assert "UNKNOWN_ORDER" in MAIN
    assert 'if order is not None and order not in ("attention",):' in MAIN


def test_as_optional_int_handles_every_shape_the_wire_delivers():
    src = MAIN.split("def _as_optional_int(value):")[1].split("\nasync def ")[0]
    ns = {}
    exec("def _as_optional_int(value):" + src, ns)
    f = ns["_as_optional_int"]
    assert f(3) == 3
    assert f("3") == 3           # a ->> select delivers text
    assert f("  3 ") == 3
    assert f(0) == 0
    assert f(-1) is None         # a count is never negative
    assert f("-1") is None
    assert f(None) is None
    assert f("") is None
    assert f("many") is None
    assert f(True) is None       # bool is an int in python; not a count
    assert f([3]) is None
