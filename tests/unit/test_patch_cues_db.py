"""Integration tests for cue storage (patch_cues) in store_connected_patches.

Same harness rules as test_structured_ingest_db.py: needs a live Postgres
via TEST_DATABASE_URL (CI / docker), skipped otherwise; imports src/worker.py
by path, so it sits in the "ignore locally" bucket.
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
    import asyncpg  # noqa: E402 — guarded, absent in the bare local venv

    run_migrations = _load_by_path("run_migrations_cues", REPO_ROOT / "scripts" / "run_migrations.py")
    worker_mod = _load_by_path("worker_cues", REPO_ROOT / "src" / "worker.py")
    store_connected_patches = worker_mod.store_connected_patches

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0, "migrations failed to apply against TEST_DATABASE_URL"
    _SCHEMA_READY["done"] = True


async def _cues_for_user(conn, user_id: str) -> dict:
    rows = await conn.fetch(
        """SELECT pc.patch_id, pc.cue
             FROM patch_cues pc
             JOIN patch_subjects ps ON ps.patch_id = pc.patch_id
            WHERE ps.subject_key = $1
            ORDER BY pc.cue""",
        f"user:{user_id}",
    )
    out: dict = {}
    for r in rows:
        out.setdefault(str(r["patch_id"]), []).append(r["cue"])
    return out


async def test_new_patch_stores_cues_and_strips_them_from_value():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        user_id = f"cue-test-{uuid.uuid4().hex[:8]}"
        stored = await store_connected_patches(
            conn, user_id,
            [{
                "type": "commitment",
                "value": {"text": "Finalize the pricing tiers before launch",
                          "owner": "Dana", "cues": ["pricing model", "launch checklist"]},
                "connects_to": [],
            }],
            source_prompt="cue_test",
        )
        assert stored == 1
        by_patch = await _cues_for_user(conn, user_id)
        assert len(by_patch) == 1
        (cues,) = by_patch.values()
        assert cues == ["launch checklist", "pricing model"]
        # value JSONB must NOT carry cues — patch_cues is the source of truth
        val = await conn.fetchval(
            """SELECT cp.value FROM context_patches cp
                 JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                WHERE ps.subject_key = $1""",
            f"user:{user_id}",
        )
        assert '"cues"' not in val
    finally:
        await conn.close()


async def test_dedup_reobservation_unions_cues_forward():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        user_id = f"cue-test-{uuid.uuid4().hex[:8]}"
        base = {
            "type": "commitment",
            "value": {"text": "Finalize the pricing tiers before launch",
                      "owner": "Dana", "cues": ["pricing model"]},
            "connects_to": [],
        }
        await store_connected_patches(conn, user_id, [base], source_prompt="cue_test")
        # Same fact re-observed (trigram-identical text), new cue attached
        again = {
            "type": "commitment",
            "value": {"text": "Finalize the pricing tiers before launch",
                      "owner": "Dana", "cues": ["pricing model", "tier structure"]},
            "connects_to": [],
        }
        stored = await store_connected_patches(conn, user_id, [again], source_prompt="cue_test")
        assert stored == 0  # deduped, not inserted
        by_patch = await _cues_for_user(conn, user_id)
        assert len(by_patch) == 1  # still one patch
        (cues,) = by_patch.values()
        assert cues == ["pricing model", "tier structure"]  # union, no dupes
    finally:
        await conn.close()


async def test_junk_cues_from_structured_ingest_are_normalized():
    """Structured-ingest payloads bypass the sanitizer chain — the worker's
    defensive normalize_cue_list must still catch junk."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        user_id = f"cue-test-{uuid.uuid4().hex[:8]}"
        await store_connected_patches(
            conn, user_id,
            [{
                "type": "takeaway",
                "value": {"text": "Anchor answers with metrics",
                          "cues": ["  STAR Method ", "meeting", 42, "ab", None]},
                "connects_to": [],
            }],
            source_prompt="cue_test",
        )
        by_patch = await _cues_for_user(conn, user_id)
        (cues,) = by_patch.values()
        assert cues == ["star method"]
    finally:
        await conn.close()
