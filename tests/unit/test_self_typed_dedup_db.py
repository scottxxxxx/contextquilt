"""The self-typed candidate queries, executed against a real Postgres.

The pure tests pin the SQL text. This runs it, because the defect was a
pair of rows nobody ever asked about: two traits at similarity 0.30
sharing three cues, below the floor the store path used, so the judge
never saw them and recall rendered both.

Same harness as test_cue_leg_scope_db.py: TEST_DATABASE_URL or skip.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

from contextquilt.services.semantic_dedup import (
    CUE_CANDIDATE_SQL,
    SELF_TRIGRAM_CANDIDATE_SQL,
    SELF_TYPED_DEDUP_FLOOR,
    SEMANTIC_DEDUP_FLOOR,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DB = REPO_ROOT / "init-db"

A = ("Values security as a foundational concern across all layers; applies "
     "defense-in-depth philosophy and believes security cannot be solved by "
     "prompt engineering alone.")
B = ("Passionate about security and defense-in-depth; skeptical of single-point "
     "solutions and prompt-only security approaches; believes security must be "
     "implemented at every layer.")


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
        "run_migrations_self_dedup", REPO_ROOT / "scripts" / "run_migrations.py"
    )

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0, "migrations failed to apply against TEST_DATABASE_URL"
    _SCHEMA_READY["done"] = True


async def _patch(conn, subject_key, text, patch_type, *, owner=None, cues=()):
    patch_id = uuid.uuid4()
    import json
    value = {"text": text}
    if owner:
        value["owner"] = owner
    await conn.execute(
        "INSERT INTO context_patches (patch_id, patch_name, patch_type, value, status) "
        "VALUES ($1, $2, $3, $4::jsonb, 'active')",
        patch_id, text[:40], patch_type, json.dumps(value),
    )
    await conn.execute(
        "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)", patch_id, subject_key)
    for cue in cues:
        await conn.execute("INSERT INTO patch_cues (patch_id, cue) VALUES ($1, $2)", patch_id, cue)
    return patch_id


@pytest.mark.asyncio
async def test_the_security_pair_is_found_at_the_new_floor_and_not_at_the_old():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject = f"user:{uuid.uuid4()}"
        existing = await _patch(conn, subject, A, "trait",
                                cues=("defense in depth", "layered approach", "security-first"))
        old = await conn.fetchrow(SELF_TRIGRAM_CANDIDATE_SQL, subject, "trait", B,
                                  SEMANTIC_DEDUP_FLOOR, "")
        new = await conn.fetchrow(SELF_TRIGRAM_CANDIDATE_SQL, subject, "trait", B,
                                  SELF_TYPED_DEDUP_FLOOR, "")
        assert old is None, f"the old floor found it at sim {old and old['sim']}"
        assert new is not None and new["patch_id"] == existing
        assert SELF_TYPED_DEDUP_FLOOR < new["sim"] < SEMANTIC_DEDUP_FLOOR
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_cue_candidate_finds_it_by_shared_cues_regardless_of_wording():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject = f"user:{uuid.uuid4()}"
        existing = await _patch(conn, subject, A, "trait",
                                cues=("defense in depth", "layered approach", "security-first"))
        unrelated = await _patch(conn, subject, "Prefers demos over documents", "trait",
                                 cues=("demo-driven",))
        row = await conn.fetchrow(CUE_CANDIDATE_SQL, subject, "trait",
                                  "Thinks every layer needs its own lock",
                                  ["security-first", "defense in depth"], "")
        assert row is not None and row["patch_id"] == existing
        assert row["shared_cues"] == 2
        assert row["patch_id"] != unrelated
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_colleagues_preference_never_folds_into_the_users():
    """Same words, different owner: not a candidate on either query."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        subject = f"user:{uuid.uuid4()}"
        theirs = await _patch(conn, subject, "Prefers Slack over email for anything urgent",
                              "preference", owner="Steven Williams", cues=("slack",))
        by_text = await conn.fetchrow(SELF_TRIGRAM_CANDIDATE_SQL, subject, "preference",
                                      "Prefers Slack over email for anything urgent",
                                      SELF_TYPED_DEDUP_FLOOR, "")
        by_cue = await conn.fetchrow(CUE_CANDIDATE_SQL, subject, "preference",
                                     "Would rather get pinged on Slack", ["slack"], "")
        assert by_text is None and by_cue is None
        # And the same owner still matches.
        same_owner = await conn.fetchrow(SELF_TRIGRAM_CANDIDATE_SQL, subject, "preference",
                                         "Prefers Slack over email for anything urgent",
                                         SELF_TYPED_DEDUP_FLOOR, "Steven Williams")
        assert same_owner is not None and same_owner["patch_id"] == theirs
    finally:
        await conn.close()
