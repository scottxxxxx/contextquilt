"""Slice 3 of the facet-runtime pass: recall speaks facets for new types.

Before this, a registered app's types scored base priority 0 (sorted
below everything SS), took no freshness discipline, got no deadline
boost, and their person entities vanished from the recall header. The
fix is facet-keyed DEFAULTS behind the SS name tables, which stay
verbatim: an SS type can never take a different score, and every
parameter defaults to the old behavior for callers that pass nothing.
"""

from pathlib import Path

from contextquilt.services.recall_scorer import (
    COMPLETABLE_DEFAULT_PRIORITY,
    FACET_PRIORITY,
    TYPE_PRIORITY,
    score_patches,
)
from contextquilt.services.recall_formatter import format_flat_ranked_with_stats

SRC = Path(__file__).resolve().parents[2] / "src"


def row(ptype, text, **kw):
    return {"patch_id": f"{ptype}-{text[:8]}", "patch_type": ptype,
            "value": {"text": text}, **kw}


# Timestamp-free rows all take a flat +5 recency component (newest ==
# oldest); fold it in so assertions read as base + recency.
RECENCY_FLAT = 5.0


def scores(patches, **kw):
    return {(r["patch_type"]): s for s, r in score_patches(patches, "query", [], **kw)}


# --------------------------------------------------------------------
# Scorer
# --------------------------------------------------------------------

def test_facet_default_lifts_an_unknown_type_off_zero():
    got = scores(
        [row("skill_rating", "clarity 7 of 10"), row("takeaway", "a note")],
        facet_by_type={"skill_rating": "Attribute"},
    )
    assert got["skill_rating"] == FACET_PRIORITY["Attribute"] + RECENCY_FLAT
    assert got["takeaway"] == TYPE_PRIORITY["takeaway"] + RECENCY_FLAT


def test_unknown_type_without_facet_still_scores_zero_base():
    got = scores([row("mystery", "unmapped thing")])
    assert got["mystery"] == 0.0 + RECENCY_FLAT


def test_completable_outranks_its_facet():
    """A completable Constraint scores like an open obligation, not like
    a rule: the completable default mirrors commitment/blocker sitting
    at the top of the SS table."""
    got = scores(
        [row("improvement_area", "reduce filler words")],
        facet_by_type={"improvement_area": "Constraint"},
        completable_types=frozenset({"improvement_area"}),
    )
    assert got["improvement_area"] == COMPLETABLE_DEFAULT_PRIORITY + RECENCY_FLAT
    assert COMPLETABLE_DEFAULT_PRIORITY > FACET_PRIORITY["Constraint"]


def test_ss_types_cannot_take_a_facet_score():
    """TYPE_PRIORITY wins first, always: passing facets and completables
    for SS names changes nothing, which is the byte-identity contract."""
    patches = [row(t, f"an {t}") for t in TYPE_PRIORITY]
    plain = scores(patches)
    with_runtime = scores(
        patches,
        facet_by_type={t: "Episode" for t in TYPE_PRIORITY},
        completable_types=frozenset(TYPE_PRIORITY),
        deadline_types=frozenset({"commitment", "blocker"}),
    )
    assert plain == with_runtime


def test_deadline_boost_follows_the_passed_set():
    # Yesterday: overdue, and inside the 30-day window after which the
    # boost deliberately expires.
    import datetime as dt
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    overdue = {"text": "ship it", "deadline_date": yesterday}
    base = score_patches(
        [{"patch_id": "x", "patch_type": "improvement_area", "value": dict(overdue)}],
        "q", [],
        facet_by_type={"improvement_area": "Constraint"},
    )[0][0]
    boosted = score_patches(
        [{"patch_id": "x", "patch_type": "improvement_area", "value": dict(overdue)}],
        "q", [],
        facet_by_type={"improvement_area": "Constraint"},
        deadline_types=frozenset({"improvement_area"}),
    )[0][0]
    assert boosted > base


def test_freshness_discipline_extends_to_passed_types():
    """A 500-day-stale Attribute-facet type takes the same staleness
    multiplier a trait would; without the set it scores full."""
    stale = row("skill_rating", "clarity 7", last_observed_at=None, created_at=None)
    import datetime as dt
    old = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    stale["last_observed_at"] = old
    full = score_patches([dict(stale)], "q", [],
                         facet_by_type={"skill_rating": "Attribute"})[0][0]
    damped = score_patches([dict(stale)], "q", [],
                           facet_by_type={"skill_rating": "Attribute"},
                           freshness_types=frozenset({"skill_rating"}))[0][0]
    assert damped < full


# --------------------------------------------------------------------
# Formatter
# --------------------------------------------------------------------

def test_header_people_line_follows_the_vocabulary():
    entities = [{"entity_type": "contact", "name": "Rowan Achebe", "description": None}]
    default_ctx, _ = format_flat_ranked_with_stats([], entities, [])
    vocab_ctx, _ = format_flat_ranked_with_stats(
        [], entities, [], person_entity_type="contact"
    )
    assert "People:" not in default_ctx
    assert "People: Rowan Achebe" in vocab_ctx


# --------------------------------------------------------------------
# Source guards
# --------------------------------------------------------------------

def test_recall_handler_resolves_the_runtime_once():
    src = (SRC / "main.py").read_text()
    assert "type_runtime = await facet_runtime.get_type_runtime(db_pool.fetch)" in src
    assert "cp.patch_type IN ('commitment', 'blocker')" not in src, (
        "the overdue-guarantee leg literal is back"
    )
    # The render cache must not share a header between two apps whose
    # vocabularies disagree about which entity type is a person.
    assert '"person_entity_type": recall_vocab.person_entity_type,' in src


# --------------------------------------------------------------------
# People-scoped recall (boundary piece 4)
# --------------------------------------------------------------------

def test_people_scope_renders_the_tab_and_nothing_else():
    from contextquilt.services.recall_formatter import format_people_scope
    import datetime as dt
    people = [{
        "entity_id": "e1", "name": "Vijay Rayudu",
        "_they_owe": [
            {"patch_id": f"p{i}", "patch_type": "commitment",
             "text": f"item {i}", "owner": "Vijay", "deadline": None,
             "deadline_date": None, "overdue_since": None}
            for i in range(12)
        ],
        "_you_owe": None,
        "_completed_they_owe": [
            {"patch_id": "d1", "patch_type": "commitment", "text": "done thing",
             "owner": "Vijay", "deadline": None, "deadline_date": None,
             "overdue_since": None,
             "completed_at": dt.datetime(2026, 8, 9, tzinfo=dt.timezone.utc)},
        ],
    }]
    entities = [{"entity_type": "person", "name": "Vijay Rayudu", "description": None}]
    ctx, ids, total = format_people_scope(people, entities, [])
    assert "People: Vijay Rayudu" in ctx
    assert "Vijay Rayudu owes you (12 open):" in ctx
    # The cap self-describes: a truncated ledger never reads as whole.
    assert "(showing 10 of 12 open)" in ctx
    assert "Recently completed by Vijay Rayudu (1 total):" in ctx
    assert "[done 2026-08-09]" in ctx
    assert len(ids) == 11 and total == 13


def test_people_scope_lane_skips_every_memory_leg():
    src = (SRC / "main.py").read_text()
    m = __import__("re").search(
        r'recall_scope"\) == "people":(.*?)return RecallResponse',
        src, __import__("re").DOTALL,
    )
    assert m, "people-scope lane not found"
    lane = m.group(1)
    assert "_people_core" in lane
    assert "communication_style=None" not in lane  # set in the response call below the slice
    for forbidden in ("fact_rows", "cue_rows", "score_patches", "signal_lines"):
        assert forbidden not in lane, f"memory leg {forbidden} leaked into people scope"
