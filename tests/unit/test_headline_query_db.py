"""The headline lane's query, run as SQL against a real Postgres.

The tests in test_headline_lane.py assert things about the TEXT of this
lane, because worker.py cannot be imported without asyncpg. That is the
constraint every worker test here works under, and it has a blind spot
this file exists to cover.

The blind spot is not hypothetical. The first version of the query
filtered on `cp.user_id`, a column context_patches has not had since
migration 26 dropped it in favour of `patch_subjects`. The string was
present, so every source-reading test passed. The lane never raises by
design, so production would have swallowed an UndefinedColumnError and
written zero headlines forever, behind a warning nobody reads: a total
failure that presents as a feature quietly not helping.

It was found by a diagnostic query happening to use the same wrong
column, which is luck rather than an instrument. This is the instrument.

Same harness rules as test_cue_leg_scope_db.py: needs a live Postgres
via TEST_DATABASE_URL (CI / docker), skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from contextquilt.services.headlines import build_pending_fetch
from contextquilt.services.woven_digest import why_not_a_tile

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DB = REPO_ROOT / "init-db"

TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

if TEST_DB:
    import asyncpg  # noqa: E402 — guarded, absent in the bare local venv

    def _load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    run_migrations = _load("run_migrations_headlines",
                           REPO_ROOT / "scripts" / "run_migrations.py")

_READY = {"done": False}


async def _ensure_schema() -> None:
    if _READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0, "migrations failed to apply against TEST_DATABASE_URL"
    _READY["done"] = True


async def _patch(conn, subject_key, text, ptype="commitment", origin=None,
                 headline=None, status="active"):
    """One patch, subject-linked. The worker's own insert is the shape.

    No subject_key column: migration 26 dropped it, which is the very
    fact the query under test got wrong.
    """
    pid = uuid.uuid4()
    value = {"text": text}
    if headline is not None:
        value["headline"] = headline
    await conn.execute(
        """
        INSERT INTO context_patches
            (patch_id, patch_name, patch_type, value, origin_id, status)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6)
        """,
        pid, text[:40], ptype, json.dumps(value), origin, status,
    )
    await conn.execute(
        "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
        pid, subject_key,
    )
    return pid


async def _run(conn, **kw):
    sql, args = build_pending_fetch(**kw)
    return {r["patch_id"] for r in await conn.fetch(sql, *args)}


@pytest.mark.asyncio
async def test_the_query_executes_at_all():
    """The whole point of this file.

    A wrong column name raises UndefinedColumnError here, where it is a
    red test, rather than in a lane that catches everything.
    """
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        sql, args = build_pending_fetch(subject_key="user:nobody",
                                        origin_id=str(uuid.uuid4()))
        await conn.fetch(sql, *args)          # must not raise
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_it_finds_only_this_users_unheadlined_patches():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        async with conn.transaction():
            me = f"user:{uuid.uuid4()}"
            them = f"user:{uuid.uuid4()}"
            origin = str(uuid.uuid4())

            want = await _patch(conn, me, "ship the gateway", origin=origin)
            done = await _patch(conn, me, "already labelled", origin=origin,
                                headline="Already labelled")
            other = await _patch(conn, them, "not my meeting", origin=origin)
            archived = await _patch(conn, me, "archived one", origin=origin,
                                    status="archived")

            found = await _run(conn, subject_key=me, origin_id=origin)
            assert want in found
            assert done not in found, "a patch with a headline was re-fetched"
            assert other not in found, "another subject's patch was fetched"
            assert archived not in found
            raise _Rollback()
    except _Rollback:
        pass
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_backfill_form_spans_meetings_and_the_lane_form_does_not():
    # The backfill passes no origin, so it must reach patches from every
    # meeting; the worker passes one and must not touch its neighbours.
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        async with conn.transaction():
            me = f"user:{uuid.uuid4()}"
            a, b = str(uuid.uuid4()), str(uuid.uuid4())
            pa = await _patch(conn, me, "from meeting a", origin=a)
            pb = await _patch(conn, me, "from meeting b", origin=b)

            assert await _run(conn, subject_key=me, origin_id=a) == {pa}
            assert {pa, pb} <= await _run(conn, subject_key=me)
            raise _Rollback()
    except _Rollback:
        pass
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_rows_it_returns_are_shaped_for_the_live_tile_predicate():
    """The fetch and the filter have to agree about columns.

    `why_not_a_tile` reads patch_type, value, origin_id, completed_at and
    sensitivity. A SELECT that omits one would make every patch look
    like it passed, or raise inside the filter, and neither shows up in
    a test that only reads the SQL string.
    """
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        async with conn.transaction():
            me = f"user:{uuid.uuid4()}"
            origin = str(uuid.uuid4())
            await _patch(conn, me, "ship the gateway", origin=origin)
            sql, args = build_pending_fetch(subject_key=me, origin_id=origin)
            rows = await conn.fetch(sql, *args)
            assert rows
            for r in rows:
                assert why_not_a_tile(dict(r)) is None
            raise _Rollback()
    except _Rollback:
        pass
    finally:
        await conn.close()


class _Rollback(Exception):
    """Leave the test database as it was found."""
