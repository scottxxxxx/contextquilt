"""Renaming an entity whose name may already be taken (doc 24 item 2).

The decision is executed here; the four statements are executed against
Postgres in test_entity_rename_db.py. The trap doc 24 named for whoever
built this is that `entities` is unique on (user_id, name, entity_type),
so "rename" is sometimes a merge, and only the rows can say which.
"""

import pytest

from src.contextquilt.services import entity_rename as er


def _entity(name="Mitomi", etype="org", merged_into=None, eid="e-1", mentions=3):
    return {"entity_id": eid, "name": name, "entity_type": etype,
            "merged_into": merged_into, "mention_count": mentions}


# --------------------------------------------------------------------
# Which of the three things this is
# --------------------------------------------------------------------

def test_rename_when_nobody_holds_the_name():
    plan = er.plan(_entity(), None, "Netomi")
    assert plan["action"] == er.RENAME
    assert plan["from"] == "Mitomi"
    assert plan["to"] == "Netomi"


def test_merge_when_the_name_is_already_taken():
    """THE TRAP. A blind UPDATE here violates the unique index."""
    target = _entity(name="Netomi", eid="e-2", mentions=9)
    plan = er.plan(_entity(), target, "Netomi")
    assert plan["action"] == er.MERGE
    assert plan["survivor_entity_id"] == "e-2"
    assert "unique on (user_id, name, entity_type)" in plan["reason"]


def test_noop_when_the_name_is_already_right():
    plan = er.plan(_entity(name="Netomi"), None, "Netomi")
    assert plan["action"] == er.NOOP


def test_noop_is_case_insensitive_about_the_current_name():
    plan = er.plan(_entity(name="Netomi"), None, "netomi")
    assert plan["action"] == er.NOOP


def test_a_case_only_difference_with_a_holder_is_still_a_merge():
    """"netomi" and "Netomi" are one company. The unique index would not
    catch it, which is exactly why the target lookup is LOWER()-matched:
    an insensitive duplicate is still a duplicate a human means to
    collapse."""
    plan = er.plan(_entity(), _entity(name="netomi", eid="e-2"), "Netomi")
    assert plan["action"] == er.MERGE


def test_whitespace_is_trimmed_not_treated_as_a_new_name():
    plan = er.plan(_entity(name="Netomi"), None, "  Netomi  ")
    assert plan["action"] == er.NOOP


# --------------------------------------------------------------------
# Refusals, which are not the same as "would do nothing"
# --------------------------------------------------------------------

def test_a_missing_entity_is_refused_not_reported_as_a_noop():
    with pytest.raises(ValueError, match="not found"):
        er.plan(None, None, "Netomi")


def test_an_already_merged_row_is_refused():
    """Renaming a forward pointer moves a name nothing resolves to."""
    with pytest.raises(ValueError, match="already merged"):
        er.plan(_entity(merged_into="e-9"), None, "Netomi")


def test_an_empty_name_is_refused():
    for bad in ("", "   ", None, 7):
        with pytest.raises(ValueError, match="new_name is required"):
            er.plan(_entity(), None, bad)


# --------------------------------------------------------------------
# The body
# --------------------------------------------------------------------

def test_preview_and_apply_have_the_same_shape():
    plan = er.plan(_entity(), None, "Netomi")
    preview = er.response(plan, applied=False)
    applied = er.response(plan, applied=True, aliases_repointed=2,
                          relationships_repointed=5)
    assert set(preview) == set(applied)
    assert preview["applied"] is False and applied["applied"] is True
    assert applied["aliases_repointed"] == 2
    assert applied["relationships_repointed"] == 5


def test_the_body_says_on_the_wire_that_stored_text_is_untouched():
    body = er.response(er.plan(_entity(), None, "Netomi"), applied=True)
    assert "not rewritten" in body["limits"]
    assert "descriptions" in body["limits"]


def test_the_plan_is_not_mutated_by_building_a_response():
    plan = er.plan(_entity(), None, "Netomi")
    before = dict(plan)
    er.response(plan, applied=True)
    assert plan == before


# --------------------------------------------------------------------
# The statements (shape only; the DB test runs them)
# --------------------------------------------------------------------

def test_the_subject_lookup_is_scoped_to_the_user():
    assert "user_id = $1" in er.SUBJECT_SQL
    assert "merged_into" in er.SUBJECT_SQL


def test_the_target_lookup_excludes_self_and_merged_rows():
    assert "entity_id <> $4::uuid" in er.TARGET_SQL
    assert "merged_into IS NULL" in er.TARGET_SQL
    assert "LOWER(name) = LOWER($3)" in er.TARGET_SQL


def test_the_merge_marks_a_forward_pointer_and_never_deletes_the_entity():
    assert "merged_into = $1::uuid" in er.MARK_MERGED_SQL
    assert "DELETE FROM entities" not in "".join(
        v for k, v in vars(er).items() if isinstance(v, str))


def test_the_relationship_repoint_skips_edges_the_survivor_already_has():
    """relationships is unique on user+from+to+type, so a blind UPDATE
    throws on any overlapping neighbourhood."""
    assert "NOT EXISTS" in er.RELATIONSHIP_REPOINT_SQL
