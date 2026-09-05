"""Unit tests for recall_formatter."""

import json
from datetime import date, datetime

from src.contextquilt.services.recall_formatter import (
    CHARS_PER_TOKEN,
    DEFAULT_RECALL_TOKEN_BUDGET,
    MAX_RECALL_TOKEN_BUDGET,
    MIN_RECALL_TOKEN_BUDGET,
    format_flat_ranked,
    format_category_grouped,
    resolve_token_budget,
)


def _patch(patch_id, patch_type, text, owner=None, deadline=None, deadline_date=None):
    value = {"text": text, "owner": owner, "deadline": deadline}
    if deadline_date is not None:
        value["deadline_date"] = deadline_date
    return {
        "patch_id": patch_id,
        "patch_type": patch_type,
        "value": json.dumps(value),
        "created_at": datetime.utcnow(),
    }


def _entity(name, etype, description=None):
    return {"name": name, "entity_type": etype, "description": description}


def _relationship(from_name, to_name, rel_type, context=None):
    return {
        "from_name": from_name,
        "to_name": to_name,
        "relationship_type": rel_type,
        "context": context,
    }


# ============================================================
# Flat formatter
# ============================================================


def test_flat_formatter_produces_single_line_per_patch():
    scored = [
        (100.0, _patch("1", "commitment", "Ship the feature by Friday", owner="Alex", deadline="2026-04-25")),
        (80.0, _patch("2", "blocker", "API not deployable in prod")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[])
    assert "[todo] Ship the feature by Friday" in out
    assert "owner: Alex" in out
    assert "by 2026-04-25" in out
    assert "[blocker] API not deployable in prod" in out


def test_flat_formatter_respects_score_ordering():
    """First patch in input should appear first in output."""
    scored = [
        (100.0, _patch("high", "commitment", "FIRST ITEM")),
        (50.0, _patch("low", "takeaway", "SECOND ITEM")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[])
    assert out.index("FIRST ITEM") < out.index("SECOND ITEM")


def test_flat_formatter_includes_entity_header():
    scored = []
    entities = [
        _entity("Alex", "person", description="MVP backend lead"),
        _entity("Atlas App", "project", description="Q3 launch"),
    ]
    out = format_flat_ranked(scored, entity_rows=entities, relationship_rows=[])
    assert "Alex" in out
    assert "Atlas App" in out
    assert "MVP backend lead" in out


def test_flat_formatter_respects_max_chars_budget():
    # Create many patches with long text; verify truncation
    scored = [
        (100.0 - i, _patch(str(i), "takeaway", "x" * 200)) for i in range(50)
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[], max_chars=500)
    assert len(out) <= 700  # some slop for newlines/headers


def test_flat_formatter_surfaces_relationships():
    rels = [_relationship("Alex", "Atlas App", "works_on", context="backend lead")]
    out = format_flat_ranked([], entity_rows=[], relationship_rows=rels)
    assert "Alex works_on Atlas App" in out


def test_flat_formatter_handles_new_types():
    """Goals, constraints, events should render with sensible prefixes."""
    scored = [
        (60.0, _patch("g", "goal", "Ship MVP by July")),
        (55.0, _patch("c", "constraint", "Cannot exceed budget")),
        (50.0, _patch("e", "event", "Board meeting occurred")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[])
    assert "[goal]" in out
    assert "[rule]" in out
    assert "[event]" in out


# ============================================================
# Grouped formatter
# ============================================================


def test_grouped_formatter_groups_by_type():
    scored = [
        (100.0, _patch("1", "trait", "Direct communicator")),
        (90.0, _patch("2", "commitment", "Finish docs", owner="Alex")),
        (80.0, _patch("3", "blocker", "Waiting on API")),
    ]
    out = format_category_grouped(scored, entity_rows=[], relationship_rows=[])
    assert "About you" in out
    assert "Open commitments" in out
    assert "Blockers" in out
    # Ordering: About you before commitments
    assert out.index("About you") < out.index("Open commitments")


def test_grouped_formatter_renders_new_facet_sections():
    """Goals, constraints, events should appear in the grouped output."""
    scored = [
        (60.0, _patch("g", "goal", "Reach 1M MAU by Q4")),
        (55.0, _patch("c", "constraint", "PII cannot leave EU")),
        (50.0, _patch("e", "event", "Board approved expansion 2026-04-10")),
    ]
    out = format_category_grouped(scored, entity_rows=[], relationship_rows=[])
    assert "Goals" in out
    assert "Constraints" in out
    assert "Recent events" in out


def test_grouped_formatter_includes_owner_and_deadline_for_commitments():
    scored = [
        (100.0, _patch("c", "commitment", "Ship feature", owner="Alex", deadline="2026-04-25")),
    ]
    out = format_category_grouped(scored, entity_rows=[], relationship_rows=[])
    assert "Alex: Ship feature (by 2026-04-25)" in out


# ============================================================
# Deadline status (structured deadline_date)
# ============================================================

TODAY = date(2026, 6, 10)


def test_flat_formatter_marks_overdue_deadline():
    scored = [
        (100.0, _patch("c", "commitment", "Send the report", deadline="Monday", deadline_date="2026-06-08")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[], today=TODAY)
    assert "by 2026-06-08 (OVERDUE, not confirmed done)" in out


def test_flat_formatter_marks_due_today():
    scored = [
        (100.0, _patch("c", "commitment", "Send the report", deadline="today", deadline_date="2026-06-10")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[], today=TODAY)
    assert "by 2026-06-10 (due today)" in out


def test_flat_formatter_marks_due_soon_within_week():
    scored = [
        (100.0, _patch("c", "commitment", "Send the report", deadline="Friday", deadline_date="2026-06-12")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[], today=TODAY)
    assert "by 2026-06-12 (due soon)" in out


def test_flat_formatter_far_future_deadline_has_no_marker():
    scored = [
        (100.0, _patch("c", "commitment", "Send the report", deadline="in August", deadline_date="2026-08-15")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[], today=TODAY)
    assert "by 2026-08-15" in out
    assert "OVERDUE" not in out
    assert "due" not in out


def test_flat_formatter_prefers_structured_date_over_free_text():
    scored = [
        (100.0, _patch("c", "commitment", "Send the report", deadline="end of week", deadline_date="2026-06-12")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[], today=TODAY)
    assert "by 2026-06-12" in out
    assert "end of week" not in out


def test_flat_formatter_falls_back_to_free_text_deadline():
    scored = [
        (100.0, _patch("c", "commitment", "Send the report", deadline="after the board meeting")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[], today=TODAY)
    assert "by after the board meeting" in out


def test_flat_formatter_ignores_unparseable_deadline_date():
    scored = [
        (100.0, _patch("c", "commitment", "Send the report", deadline="soon", deadline_date="garbage")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[], today=TODAY)
    # Unparseable structured date renders bare, never raises
    assert "by garbage" in out
    assert "OVERDUE" not in out


def test_flat_formatter_marks_overdue_goal():
    # Goals carry deadlines too (audit 2026-07-23: a "production by July
    # 15" goal rendered dateless a week past its date). The flat path
    # renders the fragment for any type; this pins that behavior.
    scored = [
        (100.0, _patch("g", "goal", "Deliver IT Assist to production", deadline="July 15", deadline_date="2026-06-08")),
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[], today=TODAY)
    assert "[goal] Deliver IT Assist to production [by 2026-06-08 (OVERDUE, not confirmed done)]" in out


def test_grouped_formatter_marks_overdue_goal():
    scored = [
        (100.0, _patch("g", "goal", "Deliver IT Assist to production", deadline="July 15", deadline_date="2026-06-08")),
        (90.0, _patch("g2", "goal", "Grow to 1500 users")),
    ]
    out = format_category_grouped(scored, entity_rows=[], relationship_rows=[], today=TODAY)
    assert "- Deliver IT Assist to production (by 2026-06-08 (OVERDUE, not confirmed done))" in out
    # Undated goals render bare, unchanged
    assert "- Grow to 1500 users" in out


def test_grouped_formatter_marks_overdue_commitments():
    scored = [
        (100.0, _patch("c", "commitment", "Ship feature", owner="Alex", deadline="last Friday", deadline_date="2026-06-05")),
    ]
    out = format_category_grouped(scored, entity_rows=[], relationship_rows=[], today=TODAY)
    assert "Alex: Ship feature (by 2026-06-05 (OVERDUE, not confirmed done))" in out


# ============================================================
# Token budget resolution (GP contract, 2026-06-11)
# ============================================================


def test_token_budget_default_in_gp_band():
    # GP asked for a default in the 600-800 band inside their 8k scaffold
    assert 600 <= DEFAULT_RECALL_TOKEN_BUDGET <= 800
    assert resolve_token_budget(None) == DEFAULT_RECALL_TOKEN_BUDGET
    assert resolve_token_budget({}) == DEFAULT_RECALL_TOKEN_BUDGET


def test_token_budget_passthrough_and_clamp():
    assert resolve_token_budget({"token_budget": 400}) == 400
    assert resolve_token_budget({"token_budget": 5}) == MIN_RECALL_TOKEN_BUDGET
    assert resolve_token_budget({"token_budget": 99999}) == MAX_RECALL_TOKEN_BUDGET
    assert resolve_token_budget({"token_budget": "800"}) == 800  # stringly-typed JSON survives


def test_token_budget_never_raises_on_garbage():
    assert resolve_token_budget({"token_budget": "lots"}) == DEFAULT_RECALL_TOKEN_BUDGET
    assert resolve_token_budget({"token_budget": None}) == DEFAULT_RECALL_TOKEN_BUDGET
    assert resolve_token_budget({"token_budget": [1]}) == DEFAULT_RECALL_TOKEN_BUDGET


def test_budget_shapes_flat_output_size():
    scored = [(100.0 - i, _patch(str(i), "takeaway", f"unique fact number {i} " + "pad " * 20)) for i in range(40)]
    small = format_flat_ranked(scored, [], [], max_chars=MIN_RECALL_TOKEN_BUDGET * CHARS_PER_TOKEN)
    large = format_flat_ranked(scored, [], [], max_chars=MAX_RECALL_TOKEN_BUDGET * CHARS_PER_TOKEN)
    assert len(small) < len(large)
    assert len(small) <= MIN_RECALL_TOKEN_BUDGET * CHARS_PER_TOKEN + 100  # patch-boundary slop


# ------------------------------------------------------------------
# format_flat_ranked_with_stats — rendered-line count (commitment E)
# ------------------------------------------------------------------

from src.contextquilt.services.recall_formatter import format_flat_ranked_with_stats


def test_stats_count_matches_rendered_patch_lines():
    patches = [(10.0 - i, _patch(str(i), "takeaway", f"observation number {i} about topic")) for i in range(8)]
    ctx, n = format_flat_ranked_with_stats(patches, [], [], max_chars=5000)
    assert n == 8
    assert ctx.count("[note]") == 8


def test_stats_count_reflects_budget_truncation():
    patches = [(10.0 - i, _patch(str(i), "takeaway", f"observation number {i} about a fairly long topic sentence")) for i in range(20)]
    ctx, n = format_flat_ranked_with_stats(patches, [], [], max_chars=300)
    assert 0 < n < 20
    assert ctx.count("[note]") == n


def test_wrapper_back_compat_same_string():
    patches = [(1.0, _patch("1", "takeaway", "just one thing"))]
    ctx, _ = format_flat_ranked_with_stats(patches, [], [], max_chars=1600)
    assert format_flat_ranked(patches, [], [], max_chars=1600) == ctx


# --- Recall age window (metadata.max_age_days) ---------------------------

def test_max_age_days_absent_means_no_window():
    from contextquilt.services.recall_formatter import resolve_max_age_days
    assert resolve_max_age_days(None) is None
    assert resolve_max_age_days({}) is None
    assert resolve_max_age_days({"max_age_days": None}) is None


def test_max_age_days_passthrough_and_clamp():
    from contextquilt.services.recall_formatter import (
        MAX_RECALL_AGE_DAYS, resolve_max_age_days,
    )
    assert resolve_max_age_days({"max_age_days": 30}) == 30
    assert resolve_max_age_days({"max_age_days": "30"}) == 30
    assert resolve_max_age_days({"max_age_days": 1}) == 1
    assert resolve_max_age_days({"max_age_days": 10 ** 6}) == MAX_RECALL_AGE_DAYS


def test_max_age_days_malformed_or_nonpositive_is_no_window_not_4xx():
    from contextquilt.services.recall_formatter import resolve_max_age_days
    for bad in ("thirty", "", 0, -5, [], {}, 2.5 * 0 - 1):
        assert resolve_max_age_days({"max_age_days": bad}) is None, bad


def test_max_age_days_boolean_is_rejected():
    # int(True) == 1 would otherwise turn a stray JSON `true` into a
    # one-day window, which is the worst possible silent default.
    from contextquilt.services.recall_formatter import resolve_max_age_days
    assert resolve_max_age_days({"max_age_days": True}) is None
    assert resolve_max_age_days({"max_age_days": False}) is None
