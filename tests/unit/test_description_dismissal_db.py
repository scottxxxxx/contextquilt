"""A dismissed description leaves the card, EXECUTED against Postgres.

Scott corrected Jillian Cunningham's company name in the app on
2026-09-12, the correction worked exactly as designed, and the card did
not change. `entities.description` is one meeting's frozen sentence on
the entity row and it is what the person card renders; dismissal writes
to `entity_descriptions`. Nothing joined them.

Measured the same day: 345 of 412 person entities carry a frozen
description and only 34 have ANY series row, so 312 people could be
"dismissed" with zero rows updated and nothing changing on screen. Zero
descriptions had ever been dismissed by anyone.

Both halves are SQL that has to run against real tables, which a
source-reading test cannot check: #464 shipped a query that left `$3`
unbound and served null to every person for nine minutes with 3025
tests green.

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
        "run_migrations_desc_dismiss", REPO_ROOT / "scripts" / "run_migrations.py"
    )

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0, "migrations failed to apply against TEST_DATABASE_URL"
    _SCHEMA_READY["done"] = True


def _lift(start: str) -> str:
    """The first triple-quoted block after `start`, LIFTED from the route
    rather than retyped. A copy here agrees with itself instead of with
    production.

    Taking the first quoted block rather than splitting on an end marker,
    because the obvious end marker bites: "user_id, entity_id," is both
    the argument line AND the INSERT's column list, so an end-marker
    version silently returned SQL truncated before any parameter. Caught
    by the contiguous-parameter test below, which is the whole reason it
    exists.
    """
    after = MAIN.split(start, 1)[1]
    return after.split('"""', 2)[1]


def _materialise_sql() -> str:
    return _lift("materialised = await db_pool.fetchval(")


def _suppress_sql() -> str:
    return _lift("if await db_pool.fetchval(")


async def _person(conn, user_id, name, description):
    entity_id = uuid.uuid4()
    await conn.execute(
        """INSERT INTO entities (entity_id, user_id, name, entity_type, description)
           VALUES ($1, $2, $3, 'person', $4)""",
        entity_id, user_id, name, description)
    return entity_id


@pytest.mark.asyncio
async def test_both_queries_parse_and_run():
    """A source-reading test cannot see a query that does not parse."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        assert await conn.fetchval(_materialise_sql(), u, str(uuid.uuid4())) is None
        assert await conn.fetchval(
            _suppress_sql(), u, str(uuid.uuid4()), "anything") is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_frozen_description_is_materialised_into_the_series():
    """THE BUG. 312 of 412 people have a frozen sentence and no series
    row, so a dismissal marked nothing and changed nothing on screen."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        text = "HR lead for North America at Mitomi, overseeing ~70 people"
        e = await _person(conn, u, "Jillian Cunningham", text)
        assert await conn.fetchval(
            "SELECT count(*) FROM entity_descriptions WHERE entity_id=$1", e) == 0

        made = await conn.fetchval(_materialise_sql(), u, str(e))
        assert made is not None
        row = await conn.fetchrow(
            "SELECT description, source FROM entity_descriptions WHERE entity_id=$1", e)
        assert row["description"] == text
        assert row["source"] == "entities.description"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_materialising_twice_does_not_duplicate():
    """A second dismissal must be idempotent, not a second row."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        e = await _person(conn, u, "Jillian", "HR lead at Mitomi")
        await conn.fetchval(_materialise_sql(), u, str(e))
        again = await conn.fetchval(_materialise_sql(), u, str(e))
        assert again is None
        assert await conn.fetchval(
            "SELECT count(*) FROM entity_descriptions WHERE entity_id=$1", e) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_person_with_no_frozen_description_materialises_nothing():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        # Distinct names per case: `entities` carries a unique constraint
        # on (user_id, name, entity_type), so reusing one name made the
        # SECOND iteration fail on the INSERT rather than on the thing
        # under test. Worth more than the fix: it also constrains the
        # deferred org rename, since renaming onto a name that already
        # exists for that type collides and has to become a merge.
        for i, desc in enumerate((None, "", "   ")):
            e = await _person(conn, u, f"Nobody {i}", desc)
            assert await conn.fetchval(_materialise_sql(), u, str(e)) is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_dismissed_sentence_is_suppressed_and_others_are_not():
    """Matched on the exact text, so rejecting an OLD perception does not
    hide a NEWER sentence nobody has objected to."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        old_text = "HR lead at Mitomi"
        e = await _person(conn, u, "Jillian", old_text)
        await conn.fetchval(_materialise_sql(), u, str(e))
        await conn.execute(
            """UPDATE entity_descriptions SET dismissed_at = NOW(),
                      dismissed_source = 'user_card'
                WHERE entity_id = $1""", e)

        assert await conn.fetchval(_suppress_sql(), u, str(e), old_text) == 1
        # A different sentence on the same person is untouched.
        assert await conn.fetchval(
            _suppress_sql(), u, str(e), "HR lead at Netomi") is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_live_description_is_never_suppressed():
    """The case the change must leave alone: materialised but NOT
    dismissed still shows."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        text = "HR lead at Mitomi"
        e = await _person(conn, u, "Jillian", text)
        await conn.fetchval(_materialise_sql(), u, str(e))
        assert await conn.fetchval(_suppress_sql(), u, str(e), text) is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_one_persons_dismissal_does_not_hide_anothers():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u = str(uuid.uuid4())
        text = "HR lead at Mitomi"
        a = await _person(conn, u, "Jillian", text)
        b = await _person(conn, u, "Someone Else", text)
        await conn.fetchval(_materialise_sql(), u, str(a))
        await conn.execute(
            "UPDATE entity_descriptions SET dismissed_at=NOW() WHERE entity_id=$1", a)
        assert await conn.fetchval(_suppress_sql(), u, str(a), text) == 1
        assert await conn.fetchval(_suppress_sql(), u, str(b), text) is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_every_bound_parameter_is_referenced():
    """Postgres refuses a parameter it cannot type, and it cannot type
    one the query never mentions (#464)."""
    for sql, n in ((_materialise_sql(), 2), (_suppress_sql(), 3)):
        refs = {int(x) for x in re.findall(r"\$(\d+)", sql)}
        assert refs == set(range(1, n + 1)), f"expected $1..${n}, got {sorted(refs)}"
