"""BARE_NAME_CANDIDATES_SQL, executed against a real Postgres.

The pure tests pin the chooser. This runs the query that feeds it,
because the 2026-09-04 defect was a row coming back (Steven Levy, alias
"Steven", no presence in the Immigration project) and the row that
should have (Steven Williams, present, no alias) not being asked for.

Same harness as test_cue_leg_scope_db.py: TEST_DATABASE_URL or skip.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

from contextquilt.services.entity_match import BARE_NAME_CANDIDATES_SQL

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DB = REPO_ROOT / "init-db"
IMMIGRATION = "10FF20F9-0000-0000-0000-000000000001"
OTHER = "3FEE79F3-0000-0000-0000-000000000002"


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
        "run_migrations_bare_names", REPO_ROOT / "scripts" / "run_migrations.py"
    )

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0, "migrations failed to apply against TEST_DATABASE_URL"
    _SCHEMA_READY["done"] = True


async def _person(conn, user_id, name, *, alias=None, project=None, merged_into=None,
                  suppressed=False, entity_type="person"):
    eid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO entities (entity_id, user_id, name, entity_type, merged_into, suppressed_at) "
        "VALUES ($1, $2, $3, $4, $5, CASE WHEN $6 THEN NOW() ELSE NULL END)",
        eid, user_id, name, entity_type, merged_into, suppressed,
    )
    if alias:
        await conn.execute(
            "INSERT INTO entity_aliases (user_id, entity_id, alias, source) VALUES ($1, $2, $3, 'heuristic')",
            user_id, eid, alias,
        )
    if project:
        await conn.execute(
            "INSERT INTO person_appearances (user_id, entity_id, origin_id, origin_type, project_id) "
            "VALUES ($1, $2, $3, 'meeting', $4)",
            user_id, eid, str(uuid.uuid4()), project,
        )
    return eid


@pytest.mark.asyncio
async def test_the_present_namesake_is_a_candidate_and_the_alias_holder_is_not_present():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        user = f"u-{uuid.uuid4()}"
        levy = await _person(conn, user, "Steven Levy", alias="Steven", project=OTHER)
        williams = await _person(conn, user, "Steven Williams", project=IMMIGRATION)
        nguyen = await _person(conn, user, "Steven Nguyen")
        survivor = await _person(conn, user, "Stevie Survivor")
        await _person(conn, user, "Steven Folded", merged_into=survivor)
        await _person(conn, user, "Steven Disowned", suppressed=True)
        await _person(conn, user, "Steven Street", entity_type="org")

        rows = await conn.fetch(BARE_NAME_CANDIDATES_SQL, user, ["steven"], IMMIGRATION, "person")
        by = {r["name"]: r for r in rows}

        assert set(by) == {"Steven Levy", "Steven Williams", "Steven Nguyen"}
        assert by["Steven Williams"]["present"] is True
        assert by["Steven Levy"]["present"] is False       # present elsewhere is not present here
        assert by["Steven Nguyen"]["present"] is False
        assert all(r["term"] == "steven" for r in rows)
        assert {r["entity_id"] for r in rows} == {levy, williams, nguyen}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_term_nobody_answers_to_returns_nothing():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        user = f"u-{uuid.uuid4()}"
        await _person(conn, user, "Raj Kumar", project=IMMIGRATION)
        rows = await conn.fetch(BARE_NAME_CANDIDATES_SQL, user, ["hassan"], IMMIGRATION, "person")
        assert rows == []
        rows = await conn.fetch(BARE_NAME_CANDIDATES_SQL, user, ["raj"], IMMIGRATION, "person")
        assert [r["name"] for r in rows] == ["Raj Kumar"] and rows[0]["present"] is True
    finally:
        await conn.close()
