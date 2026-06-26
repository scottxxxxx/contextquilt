"""Integration tests for the structured-ingest write path (doc §12, deliverable #7).

These exercise the real write path against Postgres:
  - the longitudinal branch of `store_connected_patches` (one series identity
    + an observation history table, NOT a dedup-collapsed merge), and
  - `Worker.handle_structured_ingest` end to end: privacy gate, whole-batch
    rejection, and a happy-path TR ingest.

They need a live Postgres reachable via TEST_DATABASE_URL and are skipped
otherwise — set it to a throwaway DB, e.g.
    postgres://postgres:postgres@localhost:5432/cq_structured_test

Because the module imports `src/worker.py` (asyncpg, redis, the contextquilt
package) it is in the "ignore locally" bucket alongside test_run_migrations —
run it in CI / docker-compose, not the bare unit suite:
    TEST_DATABASE_URL=... pytest tests/unit/test_structured_ingest_db.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

# ------------------------------------------------------------------
# Load the non-package modules by path (neither scripts/run_migrations.py
# nor src/worker.py is importable as a top-level package module).
# ------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DB = REPO_ROOT / "init-db"
TR_MANIFEST_PATH = INIT_DB / "25_techrehearsal_schema.json"


def _load_by_path(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


run_migrations = _load_by_path("run_migrations", REPO_ROOT / "scripts" / "run_migrations.py")
worker_mod = _load_by_path("worker", REPO_ROOT / "src" / "worker.py")
Worker = worker_mod.Worker
store_connected_patches = worker_mod.store_connected_patches


TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

# Apply the full CQ schema at most once per session.
_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    # Idempotent: run_migrations tracks applied files in schema_migrations and
    # no-ops on a second run.
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0, "migrations failed to apply against TEST_DATABASE_URL"
    _SCHEMA_READY["done"] = True


async def _provision_tr_app(conn: asyncpg.Connection) -> str:
    """Create a fresh TR application + register the real TR manifest under it.
    Returns the app_id (UUID string). A unique app per test keeps tests
    isolated without cross-test teardown.
    """
    app_id = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO applications (app_id, app_name, client_secret_hash)
           VALUES ($1::uuid, $2, $3)""",
        app_id, f"techrehearsal-test-{app_id[:8]}", "x" * 16,
    )
    with open(TR_MANIFEST_PATH) as f:
        manifest = json.load(f)
    manifest["app_id"] = app_id
    await conn.execute(
        """INSERT INTO app_schemas (app_id, version, manifest, registered_by)
           VALUES ($1::uuid, $2, $3, $4)""",
        app_id, manifest["version"], json.dumps(manifest), "test",
    )
    return app_id


async def _active_patch_ids(conn: asyncpg.Connection, user_id: str, patch_type: str) -> list:
    rows = await conn.fetch(
        """SELECT cp.patch_id
             FROM context_patches cp
             JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND cp.patch_type = $2
              AND COALESCE(cp.status, 'active') = 'active'""",
        f"user:{user_id}", patch_type,
    )
    return [r["patch_id"] for r in rows]


async def _all_active_patch_count(conn: asyncpg.Connection, user_id: str) -> int:
    return await conn.fetchval(
        """SELECT COUNT(*)
             FROM context_patches cp
             JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND COALESCE(cp.status, 'active') = 'active'""",
        f"user:{user_id}",
    )


def _rating(skill: str, rating: str, ordinal: int) -> dict:
    return {
        "type": "skill_rating",
        "value": {
            "text": f"{skill}: {rating}",
            "skill": skill,
            "rating": rating,
            "rating_ordinal": ordinal,
        },
    }


# ==================================================================
# Longitudinal storage — store_connected_patches direct
# ==================================================================


async def test_longitudinal_append_one_identity_two_observations():
    """Two ratings of the SAME skill collapse to one series identity but keep
    BOTH observations — the trajectory is preserved, not overwritten."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    user = f"tr-{uuid.uuid4().hex[:10]}"
    project_id = str(uuid.uuid4())
    try:
        app_id = await _provision_tr_app(conn)
        lt = {"skill_rating": "skill"}

        await store_connected_patches(
            conn, user, [_rating("conflict question", "Weak", 1)],
            "structured_ingest", app_id, None, "Staff BE @ Acme", project_id,
            "run-1", "mock_run", longitudinal_types=lt,
        )
        await store_connected_patches(
            conn, user, [_rating("conflict question", "Strong", 3)],
            "structured_ingest", app_id, None, "Staff BE @ Acme", project_id,
            "run-2", "mock_run", longitudinal_types=lt,
        )

        identities = await _active_patch_ids(conn, user, "skill_rating")
        assert len(identities) == 1, "expected a single series identity row"
        pid = identities[0]

        obs = await conn.fetchval(
            "SELECT COUNT(*) FROM patch_observations WHERE patch_id = $1", pid
        )
        assert obs == 2, "expected both observations retained in history"

        # The identity snapshot holds the LATEST point (hot path reads this).
        row = await conn.fetchrow("SELECT value FROM context_patches WHERE patch_id = $1", pid)
        value = row["value"]
        value = json.loads(value) if isinstance(value, str) else value
        assert value["rating"] == "Strong"
    finally:
        await conn.execute(
            "DELETE FROM context_patches WHERE patch_id IN "
            "(SELECT patch_id FROM patch_subjects WHERE subject_key = $1)",
            f"user:{user}",
        )
        await conn.close()


async def test_longitudinal_distinct_skills_open_separate_series():
    """Different skill descriptors open distinct series — no cross-skill merge."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    user = f"tr-{uuid.uuid4().hex[:10]}"
    project_id = str(uuid.uuid4())
    try:
        app_id = await _provision_tr_app(conn)
        lt = {"skill_rating": "skill"}
        for skill, rating, ordinal, run in [
            ("conflict question", "Weak", 1, "run-1"),
            ("system design", "Meets", 2, "run-1"),
        ]:
            await store_connected_patches(
                conn, user, [_rating(skill, rating, ordinal)],
                "structured_ingest", app_id, None, "Staff BE @ Acme", project_id,
                run, "mock_run", longitudinal_types=lt,
            )

        identities = await _active_patch_ids(conn, user, "skill_rating")
        assert len(identities) == 2, "distinct skills must not collapse together"
    finally:
        await conn.execute(
            "DELETE FROM context_patches WHERE patch_id IN "
            "(SELECT patch_id FROM patch_subjects WHERE subject_key = $1)",
            f"user:{user}",
        )
        await conn.close()


# ==================================================================
# handle_structured_ingest — privacy gate, batch rejection, happy path
# ==================================================================


def _make_worker(pool: asyncpg.Pool) -> "Worker":
    w = Worker()
    w.db = pool
    w.redis = None  # reject paths never reach Redis; happy path sends no entities
    return w


async def test_privacy_gate_rejects_transcript_field():
    """A transcript-shaped field rejects the whole request before any write."""
    await _ensure_schema()
    pool = await asyncpg.create_pool(TEST_DB, min_size=1, max_size=2)
    conn = await asyncpg.connect(TEST_DB)
    user = f"tr-{uuid.uuid4().hex[:10]}"
    try:
        app_id = await _provision_tr_app(conn)
        worker = _make_worker(pool)
        await worker.handle_structured_ingest({
            "user_id": user,
            "app_id": app_id,
            "transcript": "raw capture that must never reach CQ",
            "patches": [_rating("conflict question", "Weak", 1)],
            "metadata": {"project": "Staff BE @ Acme", "project_id": str(uuid.uuid4())},
        })
        assert await _all_active_patch_count(conn, user) == 0
    finally:
        await pool.close()
        await conn.execute(
            "DELETE FROM context_patches WHERE patch_id IN "
            "(SELECT patch_id FROM patch_subjects WHERE subject_key = $1)",
            f"user:{user}",
        )
        await conn.close()


async def test_invalid_patch_rejects_whole_batch():
    """One invalid patch (unknown type) rejects the ENTIRE batch atomically —
    the valid patches in the same batch are not partially written."""
    await _ensure_schema()
    pool = await asyncpg.create_pool(TEST_DB, min_size=1, max_size=2)
    conn = await asyncpg.connect(TEST_DB)
    user = f"tr-{uuid.uuid4().hex[:10]}"
    try:
        app_id = await _provision_tr_app(conn)
        worker = _make_worker(pool)
        await worker.handle_structured_ingest({
            "user_id": user,
            "app_id": app_id,
            "patches": [
                {"type": "rehearsal", "value": {"text": "Staff BE @ Acme"}},  # valid
                {"type": "not_a_real_type", "value": {"text": "boom"}},        # invalid
            ],
            "metadata": {"project": "Staff BE @ Acme", "project_id": str(uuid.uuid4())},
        })
        assert await _all_active_patch_count(conn, user) == 0, \
            "a single bad patch must reject the whole batch (nothing written)"
    finally:
        await pool.close()
        await conn.execute(
            "DELETE FROM context_patches WHERE patch_id IN "
            "(SELECT patch_id FROM patch_subjects WHERE subject_key = $1)",
            f"user:{user}",
        )
        await conn.close()


async def test_happy_path_writes_patches_and_observations():
    """A valid TR batch ingests: standalone patches land, and the two ratings
    of one skill become a single identity with two observation rows."""
    await _ensure_schema()
    pool = await asyncpg.create_pool(TEST_DB, min_size=1, max_size=2)
    conn = await asyncpg.connect(TEST_DB)
    user = f"tr-{uuid.uuid4().hex[:10]}"
    project_id = str(uuid.uuid4())
    try:
        app_id = await _provision_tr_app(conn)
        worker = _make_worker(pool)
        await worker.handle_structured_ingest({
            "user_id": user,
            "app_id": app_id,
            "patches": [
                {"type": "rehearsal", "value": {"text": "Staff BE @ Acme", "status": "active"}},
                _rating("conflict question", "Weak", 1),
                _rating("conflict question", "Meets", 2),
                {"type": "gap", "value": {"text": "no failure story yet", "gap_kind": "story"}},
            ],
            "entities": [],
            "relationships": [],
            "metadata": {
                "project": "Staff BE @ Acme", "project_id": project_id,
                "origin_id": "run-1", "origin_type": "mock_run",
            },
        })

        # rehearsal + one skill_rating identity + gap = 3 distinct patches
        assert await _all_active_patch_count(conn, user) == 3

        ratings = await _active_patch_ids(conn, user, "skill_rating")
        assert len(ratings) == 1
        obs = await conn.fetchval(
            "SELECT COUNT(*) FROM patch_observations WHERE patch_id = $1", ratings[0]
        )
        assert obs == 2

        assert len(await _active_patch_ids(conn, user, "rehearsal")) == 1
        assert len(await _active_patch_ids(conn, user, "gap")) == 1
    finally:
        await pool.close()
        await conn.execute(
            "DELETE FROM context_patches WHERE patch_id IN "
            "(SELECT patch_id FROM patch_subjects WHERE subject_key = $1)",
            f"user:{user}",
        )
        await conn.close()
