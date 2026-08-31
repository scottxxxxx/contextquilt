"""The cue leg, run as SQL against a real Postgres.

The pure tests in test_cue_matching.py assert the predicate is in the
string. This file executes the string, because the incident of
2026-08-30 was a row coming back, not a substring being absent: a
project chat was served another customer's overdue commitment through
this leg, which carried no project predicate at all.

Same harness rules as test_patch_cues_db.py: needs a live Postgres via
TEST_DATABASE_URL (CI / docker), skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

from contextquilt.services.cue_matching import build_cue_fetch

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DB = REPO_ROOT / "init-db"

# The recall age predicate exactly as main.py formats it for this leg.
AGE = ("AND ($4::int IS NULL OR cp.patch_type = ANY($3::text[]) "
       "OR COALESCE(cp.last_observed_at, cp.created_at)::date "
       ">= ((NOW() AT TIME ZONE 'utc')::date - $4::int))")

UNIVERSAL = ["trait", "preference", "goal", "constraint"]

PROJECT_A = "10437AFE-0000-0000-0000-000000000001"
PROJECT_B = "1A1FCD43-0000-0000-0000-000000000002"


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
    import asyncpg  # noqa: E402 — guarded, absent in the bare local venv

    run_migrations = _load_by_path(
        "run_migrations_cue_scope", REPO_ROOT / "scripts" / "run_migrations.py"
    )

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0, "migrations failed to apply against TEST_DATABASE_URL"
    _SCHEMA_READY["done"] = True


async def _patch(conn, subject_key, text, patch_type, cue, project_id=None):
    """One active patch, subject-linked and cue-indexed. Returns its id."""
    patch_id = uuid.uuid4()
    # No subject_key column here: migration 26 dropped it and patch_subjects
    # carries the link. The worker's own insert is the shape to copy.
    await conn.execute(
        """
        INSERT INTO context_patches
            (patch_id, patch_name, patch_type, value, project_id, status)
        VALUES ($1, $2, $3, $4::jsonb, $5, 'active')
        """,
        patch_id, text[:40], patch_type, f'{{"text": "{text}"}}', project_id,
    )
    await conn.execute(
        "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
        patch_id, subject_key,
    )
    await conn.execute(
        "INSERT INTO patch_cues (patch_id, cue) VALUES ($1, $2)", patch_id, cue,
    )
    return patch_id


async def _fixture(conn, subject_key):
    """Four patches, one cue, deliberately spanning the scope boundary."""
    return {
        "mine": await _patch(
            conn, subject_key, "ship the api gateway", "commitment",
            "api", PROJECT_A),
        "theirs": await _patch(
            conn, subject_key, "the other customer api migration", "commitment",
            "api", PROJECT_B),
        "unstamped": await _patch(
            conn, subject_key, "api rate limits are unclear", "blocker",
            "api", None),
        "universal": await _patch(
            conn, subject_key, "prefers api docs over calls", "preference",
            "api", PROJECT_B),
    }


async def _run(conn, subject_key, **kw):
    sql, args = build_cue_fetch(
        subject_key, ["api"], UNIVERSAL, None, AGE, **kw)
    rows = await conn.fetch(sql, *args)
    return {r["patch_id"] for r in rows}


@pytest.mark.asyncio
async def test_a_scoped_cue_hit_cannot_serve_another_projects_patch():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject_key = f"user:{uuid.uuid4()}"
        ids = await _fixture(conn, subject_key)

        got = await _run(conn, subject_key, recall_project_id=PROJECT_A)

        # The incident, and the assertion that would have caught it.
        assert ids["theirs"] not in got, (
            "another project's commitment came back through the cue leg"
        )
        # Mirroring the flat leg means these three stay, exactly as they
        # would on the flat leg for the same request.
        assert ids["mine"] in got
        assert ids["unstamped"] in got
        assert ids["universal"] in got
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_same_holds_when_the_scope_is_a_project_name():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject_key = f"user:{uuid.uuid4()}"
        mine = await _patch(
            conn, subject_key, "ship the api gateway", "commitment", "api")
        theirs = await _patch(
            conn, subject_key, "their api migration", "commitment", "api")
        await conn.execute(
            "UPDATE context_patches SET project = 'Falcon' WHERE patch_id = $1", mine)
        await conn.execute(
            "UPDATE context_patches SET project = 'Annapurna' WHERE patch_id = $1", theirs)

        got = await _run(conn, subject_key, recall_project="Falcon")

        assert theirs not in got
        assert mine in got
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_an_unscoped_request_still_gets_the_associative_leg():
    # Left deliberately open: with no current project there is nothing to
    # scope to, and this is the case the leg was built for. If this test
    # ever has to change, that is a posture decision, not a bug fix.
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject_key = f"user:{uuid.uuid4()}"
        ids = await _fixture(conn, subject_key)
        got = await _run(conn, subject_key)
        assert set(ids.values()) <= got
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_age_window_still_bounds_this_leg_when_scoped():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject_key = f"user:{uuid.uuid4()}"
        ids = await _fixture(conn, subject_key)
        await conn.execute(
            "UPDATE context_patches SET created_at = NOW() - INTERVAL '400 days', "
            "last_observed_at = NULL WHERE patch_id = $1", ids["mine"])

        sql, args = build_cue_fetch(
            subject_key, ["api"], UNIVERSAL, 30, AGE,
            recall_project_id=PROJECT_A)
        got = {r["patch_id"] for r in await conn.fetch(sql, *args)}

        assert ids["mine"] not in got, "the 30-day window did not reach this leg"
        assert ids["universal"] in got, "a universal type is exempt from the window"
    finally:
        await conn.close()
