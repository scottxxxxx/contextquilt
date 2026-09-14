"""The recall access bump reaches a patch that has no metrics row, EXECUTED.

Doc 25 finding 3. Nine worker lanes pair a patch insert with a
patch_usage_metrics insert; POST /v1/quilt/{u}/patches and the person
create behind POST /v1/people do not. The old bump was an UPDATE, so for
those patches it matched nothing, silently, inside a fire-and-forget
task, and the decay loop's access exemption could never see them.

The statement is LIFTED from main.py rather than retyped, and executed,
because the defect was a statement that ran clean and changed nothing.

Same harness as test_description_dismissal_db.py: needs
TEST_DATABASE_URL, skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DB = REPO_ROOT / "init-db"
MAIN = pathlib.Path("src/main.py").read_text()


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
        "run_migrations_access_upsert", REPO_ROOT / "scripts" / "run_migrations.py"
    )

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0, "migrations failed to apply against TEST_DATABASE_URL"
    _SCHEMA_READY["done"] = True


def _bump_sql() -> str:
    body = MAIN.split("async def _bump_patch_access(", 1)[1]
    after = body.split("await db_pool.execute(", 1)[1]
    return after.split('"""', 2)[1]


async def _patch(conn, user_id: str, ptype: str = "commitment") -> uuid.UUID:
    """A patch the way the API lanes make one: NO metrics row."""
    pid = uuid.uuid4()
    await conn.execute(
        """INSERT INTO context_patches (patch_id, patch_name, patch_type, value, status)
           VALUES ($1, 'app-created row', $2, '{"text": "app-created row"}'::jsonb, 'active')""",
        pid, ptype)
    await conn.execute(
        "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
        pid, f"user:{user_id}")
    return pid


@pytest.mark.asyncio
async def test_the_statement_parses_and_runs_on_an_empty_list():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        await conn.execute(_bump_sql(), [])
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_patch_with_no_metrics_row_gets_one_on_first_recall():
    """THE DEFECT: the old UPDATE left this row's count at nothing at all."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        pid = await _patch(conn, str(uuid.uuid4()))
        assert await conn.fetchval(
            "SELECT count(*) FROM patch_usage_metrics WHERE patch_id=$1", pid) == 0

        await conn.execute(_bump_sql(), [pid])

        row = await conn.fetchrow(
            "SELECT access_count, last_accessed_at FROM patch_usage_metrics WHERE patch_id=$1", pid)
        assert row is not None, "the bump still cannot reach an API-created patch"
        assert row["access_count"] == 1
        assert row["last_accessed_at"] is not None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_patch_with_a_row_is_incremented_not_reset():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        pid = await _patch(conn, str(uuid.uuid4()))
        await conn.execute(
            """INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
               VALUES ($1, 7, NOW() - INTERVAL '10 days', 0.5)""", pid)

        await conn.execute(_bump_sql(), [pid])

        row = await conn.fetchrow(
            """SELECT access_count, current_decay_score,
                      last_accessed_at > NOW() - INTERVAL '1 minute' AS fresh
                 FROM patch_usage_metrics WHERE patch_id=$1""", pid)
        assert row["access_count"] == 8
        assert row["fresh"] is True
        # The upsert must not clobber a column it was not asked about.
        assert row["current_decay_score"] == 0.5
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_an_id_that_is_not_a_patch_is_skipped_not_fatal():
    """The bump is fire-and-forget and its failure is swallowed, so a FK
    violation here would silently drop EVERY id in the batch."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        real = await _patch(conn, str(uuid.uuid4()))
        ghost = uuid.uuid4()

        await conn.execute(_bump_sql(), [ghost, real])

        assert await conn.fetchval(
            "SELECT access_count FROM patch_usage_metrics WHERE patch_id=$1", real) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM patch_usage_metrics WHERE patch_id=$1", ghost) == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_batch_mixing_both_shapes_lands_both():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        fresh = await _patch(conn, u)
        seen = await _patch(conn, u)
        await conn.execute(
            """INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
               VALUES ($1, 2, NOW(), 1.0)""", seen)

        await conn.execute(_bump_sql(), [fresh, seen])

        counts = {r["patch_id"]: r["access_count"] for r in await conn.fetch(
            "SELECT patch_id, access_count FROM patch_usage_metrics WHERE patch_id = ANY($1::uuid[])",
            [fresh, seen])}
        assert counts == {fresh: 1, seen: 3}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_every_bound_parameter_is_referenced():
    """#464: Postgres cannot type a parameter the query never mentions."""
    refs = {int(x) for x in re.findall(r"\$(\d+)", _bump_sql())}
    assert refs == {1}, f"expected exactly $1, got {sorted(refs)}"
