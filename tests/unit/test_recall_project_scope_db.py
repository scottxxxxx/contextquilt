"""The recall project scope rule, executed against a real Postgres.

test_recall_scope.py asserts the predicate text. This file runs it,
because the 2026-09-04 defect was rows coming back, not text being
absent: a project chat for the Immigration project was served three of
another project's interview moments and none of its own decisions.

Same harness as test_cue_leg_scope_db.py: needs TEST_DATABASE_URL,
skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contextquilt.services.cue_matching import build_cue_fetch
from contextquilt.services.recall_scope import FLAT_LIMIT, build_flat_fetch

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DB = REPO_ROOT / "init-db"

AGE = ("AND ($4::int IS NULL OR cp.patch_type = ANY($3::text[]) "
       "OR COALESCE(cp.last_observed_at, cp.created_at)::date "
       ">= ((NOW() AT TIME ZONE 'utc')::date - $4::int))")
UNIVERSAL = ["trait", "preference", "goal", "constraint"]

IMMIGRATION = "10FF20F9-0000-0000-0000-000000000001"
ONSTAK = "EA39935A-0000-0000-0000-000000000002"
MEETING_IMM = "BCF304AB-0000-0000-0000-00000000000A"
MEETING_ONS = "B4E4FC7D-0000-0000-0000-00000000000B"
MEETING_UNASSIGNED = "9BCAC120-0000-0000-0000-00000000000C"


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
        "run_migrations_recall_scope", REPO_ROOT / "scripts" / "run_migrations.py"
    )

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0, "migrations failed to apply against TEST_DATABASE_URL"
    _SCHEMA_READY["done"] = True


async def _patch(conn, subject_key, text, patch_type, *, project_id=None,
                 origin_id=None, created_at=None, cue=None):
    patch_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO context_patches
            (patch_id, patch_name, patch_type, value, project_id, origin_id, origin_type,
             status, created_at)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, 'active', $8)
        """,
        patch_id, text[:40], patch_type, f'{{"text": "{text}"}}', project_id,
        origin_id, "meeting" if origin_id else None,
        created_at or datetime.now(timezone.utc),
    )
    await conn.execute(
        "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
        patch_id, subject_key,
    )
    if cue:
        await conn.execute(
            "INSERT INTO patch_cues (patch_id, cue) VALUES ($1, $2)", patch_id, cue)
    return patch_id


async def _fixture(conn, subject_key):
    """The 2026-09-04 shape. Times are explicit so the eviction case is
    deterministic: the Immigration decision is OLDER than every Onstak row."""
    t0 = datetime.now(timezone.utc) - timedelta(hours=3)
    ids = {
        # Immigration: a stamped decision, and a moment that carries only its meeting.
        "imm_decision": await _patch(
            conn, subject_key, "app will focus on three core capabilities", "decision",
            project_id=IMMIGRATION, origin_id=MEETING_IMM, created_at=t0, cue="app"),
        "imm_moment": await _patch(
            conn, subject_key, "steven asked why every provider went down at once", "moment",
            origin_id=MEETING_IMM, created_at=t0 + timedelta(minutes=1), cue="app"),
        # Onstak: a stamped commitment proves the meeting's project; the moment carries none.
        "ons_commitment": await _patch(
            conn, subject_key, "hassan will call back mid next week", "commitment",
            project_id=ONSTAK, origin_id=MEETING_ONS, created_at=t0 + timedelta(hours=1)),
        "ons_moment": await _patch(
            conn, subject_key, "raj asked whether the agent was built in house", "moment",
            origin_id=MEETING_ONS, created_at=t0 + timedelta(hours=1, minutes=1), cue="app"),
        # A meeting nobody assigned anywhere: keeps today's reach.
        "unassigned_commitment": await _patch(
            conn, subject_key, "jitendra will update the excel sheet", "commitment",
            origin_id=MEETING_UNASSIGNED, created_at=t0 + timedelta(hours=1, minutes=2)),
        # Legacy memory with no meeting and no project at all.
        "legacy_person": await _patch(
            conn, subject_key, "hassan: recruiter at onstack", "person",
            created_at=t0 + timedelta(hours=1, minutes=3)),
        # Universal, stamped to the OTHER project, still served everywhere.
        "trait": await _patch(
            conn, subject_key, "prefers demos over documents", "trait",
            project_id=ONSTAK, created_at=t0 + timedelta(hours=1, minutes=4)),
    }
    # The eviction: more newer Onstak moments than the whole old window held.
    ids["ons_flood"] = [
        await _patch(
            conn, subject_key, f"raj follow up question {i}", "moment",
            origin_id=MEETING_ONS, created_at=t0 + timedelta(hours=2, seconds=i))
        for i in range(FLAT_LIMIT + 5)
    ]
    return ids


async def _flat(conn, subject_key, **kw):
    sql, args = build_flat_fetch(subject_key, UNIVERSAL, None, AGE, **kw)
    return [r["patch_id"] for r in await conn.fetch(sql, *args)]


async def _cue(conn, subject_key, **kw):
    sql, args = build_cue_fetch(subject_key, ["app"], UNIVERSAL, None, AGE, **kw)
    return {r["patch_id"] for r in await conn.fetch(sql, *args)}


@pytest.mark.asyncio
async def test_another_projects_moment_is_not_served_to_this_project():
    """The leak. Raj's interview question rendered into the Immigration chat."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject = f"user:{uuid.uuid4()}"
        ids = await _fixture(conn, subject)
        got = set(await _flat(conn, subject, recall_project_id=IMMIGRATION))
        assert ids["ons_moment"] not in got
        assert not (set(ids["ons_flood"]) & got)
        assert ids["ons_commitment"] not in got
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_this_projects_moment_is_served_through_its_meeting():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject = f"user:{uuid.uuid4()}"
        ids = await _fixture(conn, subject)
        got = set(await _flat(conn, subject, recall_project_id=IMMIGRATION))
        assert ids["imm_moment"] in got
        assert ids["imm_decision"] in got
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_busy_unrelated_meeting_cannot_evict_the_projects_own_rows():
    """The eviction. 25 newer Onstak moments used to fill the single window
    and the Immigration decision never reached the scorer."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject = f"user:{uuid.uuid4()}"
        ids = await _fixture(conn, subject)
        got = await _flat(conn, subject, recall_project_id=IMMIGRATION)
        assert ids["imm_decision"] in got
        assert len(got) <= 2 * FLAT_LIMIT
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_unassigned_meetings_and_legacy_rows_keep_their_reach():
    """Deliberate: no evidence a row belongs elsewhere means it is still
    served, exactly as before. Only proven foreign rows leave."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject = f"user:{uuid.uuid4()}"
        ids = await _fixture(conn, subject)
        got = set(await _flat(conn, subject, recall_project_id=IMMIGRATION))
        assert ids["unassigned_commitment"] in got
        assert ids["legacy_person"] in got
        assert ids["trait"] in got
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_project_name_fallback_applies_the_same_rule():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject = f"user:{uuid.uuid4()}"
        t0 = datetime.now(timezone.utc)
        mine = await _patch(conn, subject, "decided on slack", "decision",
                            project_id=None, origin_id=MEETING_IMM, created_at=t0)
        await conn.execute(
            "UPDATE context_patches SET project = 'Immigration' WHERE patch_id = $1", mine)
        theirs_anchor = await _patch(conn, subject, "kore anchor", "commitment",
                                     origin_id=MEETING_ONS, created_at=t0)
        await conn.execute(
            "UPDATE context_patches SET project = 'Kore' WHERE patch_id = $1", theirs_anchor)
        theirs_moment = await _patch(conn, subject, "kore moment", "moment",
                                     origin_id=MEETING_ONS, created_at=t0)
        got = set(await _flat(conn, subject, recall_project="Immigration"))
        assert mine in got
        assert theirs_moment not in got
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_cue_leg_applies_the_same_rule():
    """A cue hit on 'app' must not become a side door for the foreign moment."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject = f"user:{uuid.uuid4()}"
        ids = await _fixture(conn, subject)
        got = await _cue(conn, subject, recall_project_id=IMMIGRATION)
        assert ids["ons_moment"] not in got
        assert ids["imm_moment"] in got
        assert ids["imm_decision"] in got
    finally:
        await conn.close()
