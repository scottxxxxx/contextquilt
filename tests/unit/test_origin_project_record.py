"""A meeting whose only output is a moment still knows its project.

Measured on prod 2026-09-05: a four minute recording arrived stamped
`project: GL Unlimited`, its main extraction stored nothing, and its
only output was seven `moment` rows. `moment` is project_scoped: false
by manifest design, so nothing carried the project and nothing in the
database recorded it. The recall scope rule read the meeting as
unassigned and served those rows into an unrelated project's chat.

The ingest now records what the meeting belonged to, in the table
migration 43 built for the user's own decision, and the recall scope
rule reads both.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from contextquilt.services import origin_project
from contextquilt.services.cue_matching import build_cue_fetch
from contextquilt.services.origin_project import (
    RECORD_INGEST_PROJECT_SQL,
    assignments_available,
    assignments_union_sql,
    reset_probe,
)
from contextquilt.services.recall_scope import (
    build_flat_fetch,
    build_scoped_count,
    origins_cte,
)

ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()
WORKER = (ROOT / "src" / "worker.py").read_text()
MIGRATION = (ROOT / "init-db" / "43_origin_project_assignments.sql").read_text()
AGE = "AND ($4::int IS NULL OR cp.patch_type = ANY($3::text[]))"
UNIVERSAL = ["trait", "preference", "goal", "constraint"]


# ----------------------------------------------------------------------
# The write never argues with a human
# ----------------------------------------------------------------------

def test_the_ingest_write_cannot_overwrite_a_stored_decision():
    """An explicit assignment, and an explicit unassignment (project_id
    NULL, the third state the migration exists for), outrank a re-ingest."""
    assert "ON CONFLICT (user_id, origin_id, origin_type) DO NOTHING" in RECORD_INGEST_PROJECT_SQL
    assert "DO UPDATE" not in RECORD_INGEST_PROJECT_SQL
    assert "NULL means explicitly unassigned" in MIGRATION


def test_the_worker_records_only_when_it_has_both_an_origin_and_a_project():
    i = WORKER.index("AND THE OTHER HALF: record what this meeting belonged to")
    block = WORKER[i:i + 1600]
    assert "if origin_id and project_id:" in block
    assert "origin_project.RECORD_INGEST_PROJECT_SQL" in block
    assert 'logger.warning("origin_project_not_recorded"' in block


def test_the_record_runs_after_the_read_that_honours_the_user():
    """Ordering is load bearing: the adopt block may set project_id to
    NULL from an explicit unassignment, and then there is nothing to
    record."""
    assert WORKER.index("origin_project_adopted") < WORKER.index(
        "AND THE OTHER HALF: record what this meeting belonged to")


# ----------------------------------------------------------------------
# The read is a union, and it is opt in
# ----------------------------------------------------------------------

def test_the_union_leg_reads_the_matching_column_and_splits_the_subject():
    leg = assignments_union_sql("project_id", "$1")
    assert "FROM origin_project_assignments opa" in leg
    assert "opa.user_id = split_part($1, ':', 2)" in leg
    assert "opa.project_id AS scope" in leg and "opa.project_id IS NOT NULL" in leg
    assert "opa.project AS scope" in assignments_union_sql("project", "$1")


def test_without_the_flag_every_leg_is_byte_identical_to_before():
    for sql, _ in (build_flat_fetch("user:u", UNIVERSAL, None, AGE, recall_project_id="P"),
                   build_scoped_count("user:u", UNIVERSAL, None, AGE, recall_project_id="P"),
                   build_cue_fetch("user:u", ["api"], UNIVERSAL, None, AGE, recall_project_id="P")):
        assert "origin_project_assignments" not in sql


def test_with_the_flag_every_leg_carries_the_union_once():
    for sql, _ in (build_flat_fetch("user:u", UNIVERSAL, None, AGE, recall_project_id="P",
                                    include_assignments=True),
                   build_scoped_count("user:u", UNIVERSAL, None, AGE, recall_project_id="P",
                                      include_assignments=True),
                   build_cue_fetch("user:u", ["api"], UNIVERSAL, None, AGE, recall_project_id="P",
                                   include_assignments=True)):
        assert sql.count("origin_project_assignments") == 1
        # It joins the CTE, so the held/foreign rules read it too.
        assert sql.index("origin_project_assignments") < sql.index("held AS (")


def test_the_union_sits_inside_the_materialized_cte():
    cte = origins_cte("project_id", "$1", "$2", include_assignments=True)
    assert cte.startswith("WITH origins AS MATERIALIZED (")
    assert "origin_project_assignments" in cte.split("), ")[0]


# ----------------------------------------------------------------------
# The probe: a lagging database degrades instead of 500ing
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_probe_answers_once_and_is_cached():
    reset_probe()
    calls = {"n": 0}

    async def fetch(_q):
        calls["n"] += 1
        return [{"ok": True}]

    assert await assignments_available(fetch) is True
    assert await assignments_available(fetch) is True
    assert calls["n"] == 1
    reset_probe()


@pytest.mark.asyncio
async def test_a_missing_table_or_a_failed_probe_reads_as_absent():
    reset_probe()

    async def missing(_q):
        return [{"ok": False}]

    assert await assignments_available(missing) is False
    reset_probe()

    async def broken(_q):
        raise RuntimeError("relation does not exist")

    assert await assignments_available(broken) is False
    reset_probe()


def test_recall_probes_once_and_passes_the_answer_to_all_three_legs():
    assert "include_assignments = await origin_project.assignments_available(db_pool.fetch)" in MAIN
    assert MAIN.count("include_assignments=include_assignments,") == 3
