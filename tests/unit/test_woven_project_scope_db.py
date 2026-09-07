"""The woven grid's project scope, EXECUTED against a real Postgres.

Why this file exists, measured on prod 2026-09-07. Scott's "Twit"
project card said 39 and the grid under it showed 3. The candidate query
scoped on `cp.project_id = $3`, the stamp alone, and a meeting-bound row
carries its meeting and no project of its own, so 36 of the 39 rows the
project holds were never candidates. The served log line read
`candidates=3, dropped={}`: nothing was pruned, so no test of the tile
gate could ever have seen this. The loss was upstream, in one predicate.

Recall got the resolution in #436/#450 and this leg did not, which is
the drift these tests exist to stop. They run the SQL LIFTED FROM THE
ROUTE rather than a retyped copy, because a copy agrees with itself.

Same harness as test_recall_project_scope_db.py: needs
TEST_DATABASE_URL, skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contextquilt.services.origin_project import RECORD_INGEST_PROJECT_SQL
from contextquilt.services.recall_scope import in_project_clause, origins_cte

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DB = REPO_ROOT / "init-db"
MAIN = pathlib.Path("src/main.py").read_text()

TWIT = "8DEBE602-0000-0000-0000-000000000001"
OTHER = "10437AFE-0000-0000-0000-000000000002"
MEETING_TWIT = "BCF304AB-0000-0000-0000-00000000000A"
MEETING_OTHER = "B4E4FC7D-0000-0000-0000-00000000000B"


def _load_by_path(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

if TEST_DB:
    import asyncpg  # noqa: E402

    run_migrations = _load_by_path(
        "run_migrations_woven_scope", REPO_ROOT / "scripts" / "run_migrations.py"
    )

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0, "migrations failed to apply against TEST_DATABASE_URL"
    _SCHEMA_READY["done"] = True


def _candidate_sql_template() -> str:
    """WOVEN_CANDIDATE_SQL as shipped, lifted from main.py."""
    return re.search(r'WOVEN_CANDIDATE_SQL = """(.*?)"""', MAIN, re.S).group(1)


def _woven_sql(scoped: bool = True, col: str = "project_id") -> str:
    """The statement the route builds, assembled the way the route
    assembles it: same two helpers, same parameter positions."""
    tmpl = _candidate_sql_template()
    if not scoped:
        return tmpl.replace("{CTE}", "").replace("{PROJECT}", "")
    return (tmpl
            .replace("{CTE}", origins_cte(col, "$1", "$3", True))
            .replace("{PROJECT}", "AND " + in_project_clause(col, "$3")))


async def _patch(conn, subject_key, text, patch_type, *, project_id=None,
                 origin_id=None, created_at=None):
    patch_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO context_patches
            (patch_id, patch_name, patch_type, value, project_id, origin_id,
             origin_type, status, created_at)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, 'active', $8)
        """,
        patch_id, text[:40], patch_type, f'{{"text": "{text}", "headline": "{text[:30]}"}}',
        project_id, origin_id, "meeting" if origin_id else None,
        created_at or datetime.now(timezone.utc),
    )
    await conn.execute(
        "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
        patch_id, subject_key,
    )
    return patch_id


async def _fixture(conn, subject_key, user_id):
    """The prod shape: three stamped rows and a pile of meeting-bound ones."""
    t0 = datetime.now(timezone.utc) - timedelta(hours=3)
    ids = {
        "stamped_decision": await _patch(
            conn, subject_key, "we ship the rundown behind the flag", "decision",
            project_id=TWIT, origin_id=MEETING_TWIT, created_at=t0),
        # The 36. A moment is project_scoped:false by manifest design, so
        # it stores with a null project and resolves through its meeting.
        "twit_moment": await _patch(
            conn, subject_key, "asked whether the gateway forwards offset", "moment",
            origin_id=MEETING_TWIT, created_at=t0 + timedelta(minutes=1)),
        "twit_preference": await _patch(
            conn, subject_key, "prefers the diff before the summary", "preference",
            origin_id=MEETING_TWIT, created_at=t0 + timedelta(minutes=2)),
        # Another project's meeting-bound row: must NOT arrive.
        "other_moment": await _patch(
            conn, subject_key, "asked who owns the caption placement", "moment",
            origin_id=MEETING_OTHER, created_at=t0 + timedelta(minutes=3)),
        "other_stamped": await _patch(
            conn, subject_key, "bikes ship in march", "decision",
            project_id=OTHER, origin_id=MEETING_OTHER, created_at=t0 + timedelta(minutes=4)),
    }
    # Twit's meeting is resolvable ONLY through the ingest's own record,
    # which is the case that has no stamped sibling to resolve through.
    await conn.execute(RECORD_INGEST_PROJECT_SQL, user_id, MEETING_TWIT,
                       "meeting", TWIT, "Twit")
    await conn.execute(RECORD_INGEST_PROJECT_SQL, user_id, MEETING_OTHER,
                       "meeting", OTHER, "Austin Bike Mechanics")
    return ids


async def _candidates(conn, subject_key, project_id, days=90):
    rows = await conn.fetch(_woven_sql(), subject_key, days, project_id)
    return {r["patch_id"] for r in rows}


@pytest.mark.asyncio
async def test_the_candidate_query_parses_and_runs():
    """A source-reading test cannot see a query that does not parse, which
    is how `stated_roles` served null to every person for nine minutes on
    2026-09-07 with 3025 tests green. Empty is a fine result; not raising
    is the assertion."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        rows = await conn.fetch(
            _woven_sql(), f"user:{uuid.uuid4()}", 90, TWIT)
        assert rows == []
        # The unscoped form is a different statement and gets its own run.
        rows = await conn.fetch(_woven_sql(scoped=False), f"user:{uuid.uuid4()}", 90)
        assert rows == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_meeting_bound_row_reaches_the_grid():
    """THE BUG. 36 of Twit's 39 rows carry no project of their own and
    were invisible to the grid while the card counted them."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        user_id = str(uuid.uuid4())
        subject = f"user:{user_id}"
        ids = await _fixture(conn, subject, user_id)
        got = await _candidates(conn, subject, TWIT)
        assert ids["twit_moment"] in got
        assert ids["twit_preference"] in got
        assert ids["stamped_decision"] in got
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_another_projects_meeting_bound_row_does_not():
    """The scoping fix must not become a leak. This is the 2026-09-04
    defect in the other direction, and the reason the route reuses
    recall's clause rather than a looser one of its own."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        user_id = str(uuid.uuid4())
        subject = f"user:{user_id}"
        ids = await _fixture(conn, subject, user_id)
        got = await _candidates(conn, subject, TWIT)
        assert ids["other_moment"] not in got
        assert ids["other_stamped"] not in got
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_window_still_bounds_the_grid():
    """A case the change should leave alone. The 90-day window is not what
    thinned Twit, and it must still thin a genuinely old row."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        user_id = str(uuid.uuid4())
        subject = f"user:{user_id}"
        ids = await _fixture(conn, subject, user_id)
        old = await _patch(
            conn, subject, "settled long ago", "decision", project_id=TWIT,
            origin_id=MEETING_TWIT,
            created_at=datetime.now(timezone.utc) - timedelta(days=200))
        got = await _candidates(conn, subject, TWIT, days=90)
        assert old not in got
        assert ids["twit_moment"] in got
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_project_known_holds_for_a_project_with_nothing_stamped():
    """`project_known: false` means "wrong project" on the wire and the
    client says so. A project whose every row is meeting-bound has none
    carrying the stamp: "Emids" holds 7 rows, 0 stamped, and the narrow
    check called it non-existent."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        user_id = str(uuid.uuid4())
        subject = f"user:{user_id}"
        await _patch(conn, subject, "nothing here carries a project", "moment",
                     origin_id=MEETING_TWIT)
        await conn.execute(RECORD_INGEST_PROJECT_SQL, user_id, MEETING_TWIT,
                           "meeting", TWIT, "Twit")
        sql = (f"{origins_cte('project_id', '$1', '$2', True)}"
               "SELECT 1 FROM context_patches cp "
               "JOIN patch_subjects ps ON ps.patch_id = cp.patch_id "
               f"WHERE ps.subject_key = $1 AND {in_project_clause('project_id', '$2')} "
               "LIMIT 1")
        assert await conn.fetchval(sql, subject, TWIT) == 1
        # And a project this user genuinely does not have still reads
        # false, which is the case the change must leave alone.
        assert await conn.fetchval(sql, subject, OTHER) is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_every_bound_parameter_is_referenced():
    """Postgres refuses a parameter it cannot type, and it cannot type one
    the query never mentions (#464). Both forms of the statement."""
    for scoped in (True, False):
        refs = {int(n) for n in re.findall(r"\$(\d+)", _woven_sql(scoped=scoped))}
        assert refs == set(range(1, max(refs) + 1)), (
            f"scoped={scoped}: parameters must be contiguous from $1; got {sorted(refs)}")
        assert max(refs) == (3 if scoped else 2)
