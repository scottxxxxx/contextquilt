"""Deleting one meeting, EXECUTED against Postgres.

The scope query, the archive, the presence delete and the assignment
delete are the four statements the route runs, lifted from the service
constants (the same objects the route uses, not a retyped copy) and run
against real tables. Same harness as the other *_db tests: needs
TEST_DATABASE_URL, skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DB = REPO_ROOT / "init-db"


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
        "run_migrations_origin_delete", REPO_ROOT / "scripts" / "run_migrations.py"
    )
    from src.contextquilt.services import origin_delete as od  # noqa: E402
    from src.contextquilt.services.project_delete import partition_for_delete  # noqa: E402

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0
    _SCHEMA_READY["done"] = True


async def _patch(conn, user_id, ptype, origin_id, origin_type="meeting"):
    pid = uuid.uuid4()
    await conn.execute(
        """INSERT INTO context_patches
               (patch_id, patch_name, patch_type, value, origin_id, origin_type, status)
           VALUES ($1, 'row', $2, '{"text": "row"}'::jsonb, $3, $4, 'active')""",
        pid, ptype, origin_id, origin_type)
    await conn.execute(
        "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
        pid, f"user:{user_id}")
    return pid


async def _person_row(conn, user_id, origin_id):
    eid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO entities (entity_id, user_id, name, entity_type) VALUES ($1, $2, $3, 'person')",
        eid, user_id, f"Person {eid.hex[:6]}")
    await conn.execute(
        """INSERT INTO person_appearances (user_id, entity_id, origin_id, origin_type)
           VALUES ($1, $2, $3, 'meeting')""",
        user_id, eid, origin_id)
    return eid


async def _run_delete(conn, user_id, origin_id, origin_type="meeting"):
    """The route's write path, statement for statement, minus Redis."""
    rows = await conn.fetch(od.SCOPE_SQL, f"user:{user_id}", origin_type, origin_id)
    doomed, spared = partition_for_delete(rows)
    archived = 0
    if doomed:
        res = await conn.execute(od.ARCHIVE_SQL, [r["patch_id"] for r in doomed])
        archived = int(res.split()[-1])
    res = await conn.execute(od.APPEARANCES_DELETE_SQL, user_id, origin_id)
    appearances = int(res.split()[-1])
    await conn.execute(od.ASSIGNMENT_DELETE_SQL, user_id, origin_id, origin_type)
    return archived, len(spared), appearances


@pytest.mark.asyncio
async def test_every_statement_parses_and_runs_on_an_unknown_meeting():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        assert await _run_delete(conn, u, "no-such-meeting") == (0, 0, 0)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_meetings_patches_are_archived_with_cause_and_the_self_typed_survive():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u, m = str(uuid.uuid4()), f"m-{uuid.uuid4().hex[:8]}"
        c = await _patch(conn, u, "commitment", m)
        d = await _patch(conn, u, "decision", m)
        t = await _patch(conn, u, "trait", m)

        archived, spared, _ = await _run_delete(conn, u, m)
        assert (archived, spared) == (2, 1)

        rows = {r["patch_id"]: (r["status"], r["value"].get("archive_cause") if isinstance(r["value"], dict) else None)
                for r in await conn.fetch(
                    "SELECT patch_id, status, value FROM context_patches WHERE patch_id = ANY($1::uuid[])",
                    [c, d, t])}
        import json
        assert rows[c][0] == "archived"
        assert rows[d][0] == "archived"
        assert rows[t][0] == "active", "a trait learned in the meeting must survive it"
        raw = await conn.fetchval("SELECT value::text FROM context_patches WHERE patch_id=$1", c)
        assert json.loads(raw)["archive_cause"] == "meeting_deleted"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_another_meetings_patches_are_untouched():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        mine = await _patch(conn, u, "commitment", "m-mine")
        other = await _patch(conn, u, "commitment", "m-other")
        await _run_delete(conn, u, "m-mine")
        assert await conn.fetchval("SELECT status FROM context_patches WHERE patch_id=$1", mine) == "archived"
        assert await conn.fetchval("SELECT status FROM context_patches WHERE patch_id=$1", other) == "active"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_origin_type_is_part_of_the_key():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        doc = await _patch(conn, u, "decision", "shared-id", origin_type="document")
        await _run_delete(conn, u, "shared-id", origin_type="meeting")
        assert await conn.fetchval("SELECT status FROM context_patches WHERE patch_id=$1", doc) == "active"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_presence_rows_for_the_meeting_are_deleted_and_others_kept():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u, m = str(uuid.uuid4()), f"m-{uuid.uuid4().hex[:8]}"
        await _person_row(conn, u, m)
        await _person_row(conn, u, m)
        keep = await _person_row(conn, u, "m-other")
        _, _, appearances = await _run_delete(conn, u, m)
        assert appearances == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM person_appearances WHERE user_id=$1 AND origin_id=$2", u, m) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM person_appearances WHERE user_id=$1 AND entity_id=$2", u, keep) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_repeat_delete_reports_zeros_not_an_error():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u, m = str(uuid.uuid4()), f"m-{uuid.uuid4().hex[:8]}"
        await _patch(conn, u, "commitment", m)
        await _person_row(conn, u, m)
        assert await _run_delete(conn, u, m) == (1, 0, 1)
        assert await _run_delete(conn, u, m) == (0, 0, 0)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_another_users_meeting_with_the_same_id_is_untouched():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u1, u2, m = str(uuid.uuid4()), str(uuid.uuid4()), f"m-{uuid.uuid4().hex[:8]}"
        theirs = await _patch(conn, u2, "commitment", m)
        await _person_row(conn, u2, m)
        assert await _run_delete(conn, u1, m) == (0, 0, 0)
        assert await conn.fetchval("SELECT status FROM context_patches WHERE patch_id=$1", theirs) == "active"
        assert await conn.fetchval(
            "SELECT count(*) FROM person_appearances WHERE user_id=$1 AND origin_id=$2", u2, m) == 1
    finally:
        await conn.close()
