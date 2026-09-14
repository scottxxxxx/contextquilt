"""Do not ask again: a muted person's descriptions stop coming back.

EXECUTED, because the whole design is that the mute changes what the
WRITE path stores so every existing reader is already correct. A
source-reading test can see the CASE expression and cannot see whether
the row that lands is dismissed or the frozen column moved.

The two statements under test are lifted from the worker rather than
retyped. Needs TEST_DATABASE_URL; skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DB = REPO_ROOT / "init-db"
WORKER = pathlib.Path("src/worker.py").read_text()
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
        "run_migrations_description_mute", REPO_ROOT / "scripts" / "run_migrations.py"
    )

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0
    _SCHEMA_READY["done"] = True


def _lift(source: str, marker: str) -> str:
    """The triple-quoted block CONTAINING `marker`, taken from the file
    rather than retyped: a copy agrees with itself instead of with
    production.

    Anchored on a line INSIDE the statement, not on the code before it.
    The first version of this helper took "the next block after X" and
    got the tail of the SQL for one lift and a DOCSTRING for another,
    which is the same trap the dismissal DB test documents: an end
    marker that also matches something else. A marker inside the block
    cannot land outside it.
    """
    idx = source.index(marker)
    start = source.rindex('"""', 0, idx) + 3
    return source[start:source.index('"""', idx)]


def _insert_sql() -> str:
    return _lift(WORKER, "INSERT INTO entity_descriptions (")


def _reobserve_sql() -> str:
    return _lift(WORKER, "mention_count = mention_count + 1,")


def _mute_sql() -> str:
    return _lift(MAIN, "SET descriptions_muted_at = NOW(),")


def _unmute_sql() -> str:
    return _lift(MAIN, "SET descriptions_muted_at = NULL,")


async def _person(conn, user_id, name="Jillian", description="HR lead at Mitomi"):
    eid = uuid.uuid4()
    await conn.execute(
        """INSERT INTO entities (entity_id, user_id, name, entity_type, description)
           VALUES ($1, $2, $3, 'person', $4)""",
        eid, user_id, name, description)
    return eid


async def _observe(conn, user_id, eid, text, origin="m-1"):
    """The worker's described-as insert, with the mute flag it reads."""
    muted = await conn.fetchval(
        "SELECT descriptions_muted_at IS NOT NULL FROM entities WHERE entity_id = $1", eid)
    await conn.execute(_insert_sql(), user_id, eid, text, origin, "meeting",
                       "meeting_summary", bool(muted))
    return bool(muted)


@pytest.mark.asyncio
async def test_the_statements_parse_and_run():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        eid = await _person(conn, u)
        await _observe(conn, u, eid, "a description")
        await conn.execute(_reobserve_sql(), "new text", "{}", eid)
        await conn.execute(_mute_sql(), u, str(eid), "user_card")
        await conn.execute(_unmute_sql(), u, str(eid))
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_unmuted_is_exactly_what_it_was():
    """The default path must not move: a live row, and the frozen column
    overwritten by the new observation."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        eid = await _person(conn, u)
        assert await _observe(conn, u, eid, "an immigration attorney") is False
        row = await conn.fetchrow(
            "SELECT dismissed_at, dismissed_source FROM entity_descriptions WHERE entity_id=$1", eid)
        assert row["dismissed_at"] is None and row["dismissed_source"] is None

        await conn.execute(_reobserve_sql(), "a fresh sentence", "{}", eid)
        assert await conn.fetchval(
            "SELECT description FROM entities WHERE entity_id=$1", eid) == "a fresh sentence"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_muted_persons_next_observation_arrives_already_dismissed():
    """THE POINT. Every reader filters dismissed_at IS NULL, so the new
    perception is hidden without a second suppression rule anywhere."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        eid = await _person(conn, u)
        await conn.execute(_mute_sql(), u, str(eid), "user_card")

        assert await _observe(conn, u, eid, "a completely different perception") is True
        row = await conn.fetchrow(
            """SELECT dismissed_at, dismissed_source, description
                 FROM entity_descriptions WHERE entity_id=$1""", eid)
        assert row["dismissed_at"] is not None
        assert row["dismissed_source"] == "mute", (
            "the cause must stay distinguishable from a user dismissal")
        assert row["description"] == "a completely different perception", (
            "the observation is RECORDED; a mute hides, it does not blind")
        assert await conn.fetchval(
            """SELECT count(*) FROM entity_descriptions
                WHERE entity_id=$1 AND dismissed_at IS NULL""", eid) == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_muted_persons_frozen_description_stops_being_overwritten():
    """The other half. Without this the card shows a new inferred
    sentence the user never rejected, which is what "do not ask again"
    means to stop."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        eid = await _person(conn, u, description="HR lead at Mitomi")
        await conn.execute(_mute_sql(), u, str(eid), "user_card")

        before = await conn.fetchval(
            "SELECT mention_count FROM entities WHERE entity_id=$1", eid)
        await conn.execute(_reobserve_sql(), "an immigration attorney", "{}", eid)
        assert await conn.fetchval(
            "SELECT description FROM entities WHERE entity_id=$1", eid) == "HR lead at Mitomi"
        # And the REST of the re-observation still happened: the mute
        # freezes one column, it does not stop the person being seen.
        assert await conn.fetchval(
            "SELECT mention_count FROM entities WHERE entity_id=$1", eid) == before + 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_mute_is_per_person():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        muted = await _person(conn, u, name="Muted")
        other = await _person(conn, u, name="Other")
        await conn.execute(_mute_sql(), u, str(muted), "user_card")
        assert await _observe(conn, u, muted, "x") is True
        assert await _observe(conn, u, other, "y") is False
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_unmuting_restores_both_halves():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        eid = await _person(conn, u, description="frozen")
        await conn.execute(_mute_sql(), u, str(eid), "user_card")
        await conn.execute(_unmute_sql(), u, str(eid))

        assert await conn.fetchval(
            "SELECT descriptions_muted_at FROM entities WHERE entity_id=$1", eid) is None
        assert await _observe(conn, u, eid, "a new perception") is False
        await conn.execute(_reobserve_sql(), "a new sentence", "{}", eid)
        assert await conn.fetchval(
            "SELECT description FROM entities WHERE entity_id=$1", eid) == "a new sentence"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_history_observed_while_muted_survives_the_unmute():
    """A mute hides. Everything said while it was on is still there, and
    the undo is what brings it back."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        eid = await _person(conn, u)
        await conn.execute(_mute_sql(), u, str(eid), "user_card")
        await _observe(conn, u, eid, "said while muted")
        await conn.execute(_unmute_sql(), u, str(eid))
        # The undismiss statement on this route clears every stamp.
        await conn.execute(
            """UPDATE entity_descriptions SET dismissed_at = NULL, dismissed_source = NULL
                WHERE user_id = $1 AND entity_id = $2::uuid AND dismissed_at IS NOT NULL""",
            u, str(eid))
        rows = [r["description"] for r in await conn.fetch(
            "SELECT description FROM entity_descriptions WHERE entity_id=$1 AND dismissed_at IS NULL", eid)]
        assert rows == ["said while muted"]
    finally:
        await conn.close()
