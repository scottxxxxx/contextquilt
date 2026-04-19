"""Unit tests for recall_formatter."""

import json
from datetime import datetime

from src.contextquilt.services.recall_formatter import (
    format_flat_ranked,
    format_category_grouped,
)


def _patch(patch_id, patch_type, text, owner=None, deadline=None):
    return {
        "patch_id": patch_id,
        "patch_type": patch_type,
        "value": json.dumps({"text": text, "owner": owner, "deadline": deadline}),
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
        _entity("Benefits App", "project", description="Q3 launch"),
    ]
    out = format_flat_ranked(scored, entity_rows=entities, relationship_rows=[])
    assert "Alex" in out
    assert "Benefits App" in out
    assert "MVP backend lead" in out


def test_flat_formatter_respects_max_chars_budget():
    # Create many patches with long text; verify truncation
    scored = [
        (100.0 - i, _patch(str(i), "takeaway", "x" * 200)) for i in range(50)
    ]
    out = format_flat_ranked(scored, entity_rows=[], relationship_rows=[], max_chars=500)
    assert len(out) <= 700  # some slop for newlines/headers


def test_flat_formatter_surfaces_relationships():
    rels = [_relationship("Alex", "Benefits App", "works_on", context="backend lead")]
    out = format_flat_ranked([], entity_rows=[], relationship_rows=rels)
    assert "Alex works_on Benefits App" in out


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
