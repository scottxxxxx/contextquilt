"""Deleting a project deletes what its meetings produced.

Scott reversed the old ruling on 2026-09-06. The unscope docstring said
"deleting a project container must never delete what was learned in its
meetings"; his requirement is now that deleting a project be "as if
those events never occurred and we don't leave any remnants".

He chose both open questions against real numbers rather than in the
abstract:

  SCOPE   everything the project's MEETINGS produced, resolved the way
          recall resolves a project, EXCEPT the self-typed set. Narrow
          scope left 35 of 37 rows on his "Twit" project, which is the
          remnant problem itself. Full scope without the carve-out took
          durable facts about HIM ("Mixtral model cannot be deployed at
          Florida Blue due to excessive resource consumption") that
          merely happened to be learned there.
  DEPTH   archive, not hard delete, because the delta's `deleted[]` is
          computed FROM archived rows and a hard delete would stop other
          devices ever learning.

The DB tests need TEST_DATABASE_URL; CI provides one. They exist because
a source-reading test cannot see a query that does not parse, which is
how I served null `stated_roles` to every person for nine minutes
earlier the same evening.
"""

import os
import pathlib
import re

import pytest

TEST_DB = os.getenv("TEST_DATABASE_URL")
MAIN = pathlib.Path("src/main.py").read_text()


def _scope_sql() -> str:
    """Lifted from the source rather than imported, because importing
    `main` needs fastapi and the unit suite runs without it. Lifted
    rather than retyped for the usual reason: a retyped copy agrees with
    itself instead of with production."""
    return MAIN.split('PROJECT_DELETE_SCOPE_SQL = """')[1].split('"""')[0]


def _route_body() -> str:
    return MAIN.split("async def unscope_project")[1].split("\n@app.")[0]


def _impl_body() -> str:
    return MAIN.split("async def _project_delete")[1].split("\nclass ")[0]


# --- the rulings, pinned ----------------------------------------------

def test_self_typed_patches_are_spared_EXECUTED():
    """THE guarantee Scott chose, exercised rather than grepped.

    The first version of this test asserted `"FRESHNESS_TRACKED_TYPES"
    in _impl_body()`, and a sabotage that deleted the carve-out outright
    PASSED IT, because the constant was still sitting in a comment two
    lines away. Third loose-string match to fail that way in one
    evening. The partition moved into its own module so it can be run.
    """
    from contextquilt.services.project_delete import partition_for_delete
    rows = [{"patch_type": t} for t in
            ("moment", "trait", "decision", "preference", "goal",
             "constraint", "commitment", "takeaway", "blocker")]
    doomed, spared = partition_for_delete(rows)
    assert sorted(r["patch_type"] for r in spared) == [
        "constraint", "goal", "preference", "trait"]
    assert sorted(r["patch_type"] for r in doomed) == [
        "blocker", "commitment", "decision", "moment", "takeaway"]


def test_the_spared_set_is_the_recall_scorer_set_not_a_copy():
    """Two lists of the same four names drift. Identity, not equality,
    so a copy with the same contents today still fails."""
    from contextquilt.services import project_delete
    from contextquilt.services.recall_scorer import FRESHNESS_TRACKED_TYPES
    assert project_delete.SPARED_TYPES is FRESHNESS_TRACKED_TYPES


def test_the_counts_a_warning_is_written_from_are_the_doomed_ones():
    """A user warned about 1,422 must not have 1,511 deleted, and must
    not be warned about rows that are spared."""
    from contextquilt.services.project_delete import (
        counts_by_type, partition_for_delete,
    )
    rows = [{"patch_type": t} for t in
            ("moment", "moment", "trait", "decision")]
    doomed, spared = partition_for_delete(rows)
    assert counts_by_type(doomed) == {"moment": 2, "decision": 1}
    assert len(doomed) + len(spared) == len(rows)


def test_the_scope_resolves_a_project_the_way_recall_does():
    """Narrow scope (stamped rows only) left 35 of 37 on a real project,
    which is the remnant problem rather than a fix for it."""
    sql = _scope_sql()
    assert "origin_project_assignments" in sql
    assert "cp.project_id = $2" in sql


def test_preview_and_delete_share_one_resolution():
    """The number a user is warned with must be the number archived. Two
    queries would be two sources of truth about one destructive act."""
    body = _impl_body()
    assert body.count("PROJECT_DELETE_SCOPE_SQL") == 1


def test_it_archives_rather_than_hard_deleting():
    body = _impl_body()
    assert "status = 'archived'" in body
    assert '"project_deleted"' in body
    assert "DELETE FROM context_patches" not in body


# --- the safe side, which is the whole reason it is a body field -------

def test_an_absent_flag_keeps_todays_behaviour():
    """The flag crosses GhostPour, and that hop has eaten optional
    fields before. If a future middlebox eats this one the failure must
    be "nothing was deleted", never the reverse."""
    body = _route_body()
    assert "req and req.delete_patches" in body
    assert "req and req.preview" in body
    assert "Optional[ProjectUnscopeRequest] = None" in MAIN


def test_the_flag_is_a_body_field_not_a_query_param():
    """SS read GP's handler: the body is forwarded as an untyped dict,
    and that same call site never passes `query` through, so a query
    parameter is discarded before CQ sees it."""
    assert "class ProjectUnscopeRequest(BaseModel)" in MAIN
    body = _route_body()
    assert "Query(" not in body.split("async def _project_delete")[0]


def test_preview_writes_nothing():
    body = _impl_body()
    preview_branch = body.split('if preview:')[1].split('archived = 0')[0]
    for destructive in ("UPDATE", "DELETE", "xadd"):
        assert destructive not in preview_branch, destructive


def test_the_survivors_are_reported_rather_than_left_to_be_discovered():
    """"No remnants" is the requirement, so what this does NOT clear is
    named on the wire: appearances, the origin record, and the transcript
    bodies on the ingest stream, which is the largest remnant and which
    only an account purge clears."""
    body = _impl_body()
    for key in ("self_typed_patches", "person_appearances",
                "origin_records", "transcripts_on_the_ingest_stream"):
        assert key in body, key


# --- executing, against a real database --------------------------------

@pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")
@pytest.mark.asyncio
async def test_the_scope_query_actually_runs():
    """It must PARSE and EXECUTE, not merely contain the right words."""
    import asyncpg
    conn = await asyncpg.connect(TEST_DB)
    try:
        rows = await conn.fetch(
            _scope_sql(),
            "user:00000000-0000-0000-0000-000000000000",
            "no-such-project")
        assert rows == []
    finally:
        await conn.close()


@pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")
@pytest.mark.asyncio
async def test_every_bound_parameter_is_referenced():
    """Postgres refuses a parameter it cannot type, and it cannot type
    one the query never mentions. That is exactly how the stated_roles
    query broke earlier tonight."""
    refs = {int(n) for n in re.findall(r"\$(\d+)", _scope_sql())}
    assert refs == set(range(1, max(refs) + 1)), sorted(refs)
