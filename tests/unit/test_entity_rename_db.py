"""The rename, and the merge it becomes, EXECUTED against Postgres.

The unique index on (user_id, name, entity_type) is the whole reason
this operation is not what its name says, and an index is not a thing a
source-reading test can see. Needs TEST_DATABASE_URL; skipped otherwise.
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
        "run_migrations_entity_rename", REPO_ROOT / "scripts" / "run_migrations.py"
    )
    from src.contextquilt.services import entity_rename as er  # noqa: E402

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0
    _SCHEMA_READY["done"] = True


async def _entity(conn, user_id, name, etype="org"):
    eid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO entities (entity_id, user_id, name, entity_type) VALUES ($1,$2,$3,$4)",
        eid, user_id, name, etype)
    return eid


async def _apply(conn, user_id, entity_id, new_name, source="admin"):
    """The route's write path, statement for statement, minus Redis."""
    subject = await conn.fetchrow(er.SUBJECT_SQL, user_id, str(entity_id))
    target = await conn.fetchrow(
        er.TARGET_SQL, user_id, subject["entity_type"], new_name, str(entity_id))
    plan = er.plan(subject, target, new_name)
    if plan["action"] == er.NOOP:
        return plan, 0, 0
    aliases = rels = 0
    async with conn.transaction():
        if plan["action"] == er.RENAME:
            await conn.execute(er.RENAME_SQL, user_id, str(entity_id), new_name)
            await conn.execute(er.ALIAS_SQL, user_id, str(entity_id), plan["from"], source)
        else:
            survivor = plan["survivor_entity_id"]
            await conn.execute(er.ALIAS_SQL, user_id, survivor, plan["from"], source)
            moved = await conn.execute(er.ALIASES_REPOINT_SQL, survivor, user_id, str(entity_id))
            aliases = int(moved.split()[-1])
            for column in ("from_entity_id", "to_entity_id"):
                other = "to_entity_id" if column == "from_entity_id" else "from_entity_id"
                moved = await conn.execute(
                    er.RELATIONSHIP_REPOINT_SQL.format(column=column, other=other),
                    survivor, user_id, str(entity_id))
                rels += int(moved.split()[-1])
            await conn.execute(er.RELATIONSHIP_CLEANUP_SQL, user_id, str(entity_id))
            await conn.execute(er.SELF_LOOP_CLEANUP_SQL, user_id, survivor)
            await conn.execute(er.MARK_MERGED_SQL, survivor, user_id, str(entity_id))
    return plan, aliases, rels


@pytest.mark.asyncio
async def test_every_statement_parses_and_runs():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        e = await _entity(conn, u, "Mitomi")
        plan, _, _ = await _apply(conn, u, e, "Netomi")
        assert plan["action"] == er.RENAME
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_plain_rename_keeps_the_old_spelling_as_an_alias():
    """Five months of transcripts say Mitomi and they are not wrong about
    what was said; recall matches names UNION aliases."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        e = await _entity(conn, u, "Mitomi")
        await _apply(conn, u, e, "Netomi")
        assert await conn.fetchval(
            "SELECT name FROM entities WHERE entity_id=$1", e) == "Netomi"
        rows = [r["alias"] for r in await conn.fetch(
            "SELECT alias FROM entity_aliases WHERE entity_id=$1", e)]
        assert rows == ["Mitomi"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_renaming_onto_an_existing_name_merges_instead_of_violating_the_index():
    """THE TRAP, executed. A blind UPDATE raises UniqueViolation here."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        old = await _entity(conn, u, "Mitomi")
        survivor = await _entity(conn, u, "Netomi")

        with pytest.raises(asyncpg.UniqueViolationError):
            async with conn.transaction():
                await conn.execute(er.RENAME_SQL, u, str(old), "Netomi")

        plan, _, _ = await _apply(conn, u, old, "Netomi")
        assert plan["action"] == er.MERGE
        assert plan["survivor_entity_id"] == str(survivor)
        assert await conn.fetchval(
            "SELECT merged_into FROM entities WHERE entity_id=$1", old) == survivor
        assert await conn.fetchval(
            "SELECT name FROM entities WHERE entity_id=$1", old) == "Mitomi", \
            "the loser keeps its own name; the pointer is what resolves it"
        assert await conn.fetchval(
            "SELECT entity_id FROM entity_aliases WHERE user_id=$1 AND LOWER(alias)='mitomi'",
            u) == survivor
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_losers_own_aliases_follow_it_to_the_survivor():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        old = await _entity(conn, u, "Mitomi")
        survivor = await _entity(conn, u, "Netomi")
        await conn.execute(
            "INSERT INTO entity_aliases (user_id, entity_id, alias, source) VALUES ($1,$2,$3,'test')",
            u, old, "Mitomy")
        _, aliases, _ = await _apply(conn, u, old, "Netomi")
        assert aliases == 1
        assert await conn.fetchval(
            "SELECT entity_id FROM entity_aliases WHERE user_id=$1 AND LOWER(alias)='mitomy'",
            u) == survivor
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_relationships_repoint_without_colliding_on_an_edge_the_survivor_has():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        old = await _entity(conn, u, "Mitomi")
        survivor = await _entity(conn, u, "Netomi")
        person = await _entity(conn, u, "Jillian", etype="person")
        # The same edge on both sides: the merge must drop one, not throw.
        for src in (old, survivor):
            await conn.execute(
                """INSERT INTO relationships (user_id, from_entity_id, to_entity_id, relationship_type)
                   VALUES ($1,$2,$3,'works_at')""", u, person, src)
        # And one the survivor does not have.
        other = await _entity(conn, u, "Acme", etype="org")
        await conn.execute(
            """INSERT INTO relationships (user_id, from_entity_id, to_entity_id, relationship_type)
               VALUES ($1,$2,$3,'partners_with')""", u, old, other)

        _, _, rels = await _apply(conn, u, old, "Netomi")
        assert rels >= 1
        assert await conn.fetchval(
            """SELECT count(*) FROM relationships
                WHERE user_id=$1 AND from_entity_id=$2 AND to_entity_id=$3""",
            u, person, survivor) == 1
        assert await conn.fetchval(
            """SELECT count(*) FROM relationships
                WHERE user_id=$1 AND (from_entity_id=$2 OR to_entity_id=$2)""",
            u, old) == 0
        assert await conn.fetchval(
            """SELECT count(*) FROM relationships
                WHERE user_id=$1 AND from_entity_id=$2 AND to_entity_id=$3""",
            u, survivor, other) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_another_users_entity_of_the_same_name_is_not_the_target():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())
        theirs = await _entity(conn, u2, "Netomi")
        mine = await _entity(conn, u1, "Mitomi")
        plan, _, _ = await _apply(conn, u1, mine, "Netomi")
        assert plan["action"] == er.RENAME
        assert await conn.fetchval(
            "SELECT merged_into FROM entities WHERE entity_id=$1", theirs) is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_person_of_the_same_name_is_not_the_target():
    """The unique index is per TYPE: a person called Netomi does not
    block an org rename, and merging across types would be a category
    error."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        person = await _entity(conn, u, "Netomi", etype="person")
        org = await _entity(conn, u, "Mitomi")
        plan, _, _ = await _apply(conn, u, org, "Netomi")
        assert plan["action"] == er.RENAME
        assert await conn.fetchval(
            "SELECT merged_into FROM entities WHERE entity_id=$1", person) is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_second_run_of_the_same_rename_is_a_noop():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        e = await _entity(conn, u, "Mitomi")
        await _apply(conn, u, e, "Netomi")
        plan, _, _ = await _apply(conn, u, e, "Netomi")
        assert plan["action"] == er.NOOP
    finally:
        await conn.close()
