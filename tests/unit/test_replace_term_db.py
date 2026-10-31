"""Fixing a misheard word in one meeting, EXECUTED against Postgres.

`term_replace.apply` is the database half of POST .../replace-term, the
same function the route calls, run here against migrated tables so its
SQL parses, binds and touches exactly what it should. Same harness as
the other *_db tests: needs TEST_DATABASE_URL, skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import json
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
        "run_migrations_replace_term", REPO_ROOT / "scripts" / "run_migrations.py"
    )
    from src.contextquilt.services import term_replace as tr  # noqa: E402

_SCHEMA_READY = {"done": False}


async def _ensure_schema() -> None:
    if _SCHEMA_READY["done"]:
        return
    rc = await run_migrations.run(TEST_DB, INIT_DB, dry_run=False)
    assert rc == 0
    _SCHEMA_READY["done"] = True


async def _patch(conn, user_id, origin_id, value, *, status="active",
                 ptype="commitment", origin_mode=None, sources=None):
    pid = uuid.uuid4()
    await conn.execute(
        """INSERT INTO context_patches
               (patch_id, patch_name, patch_type, value, origin_id, origin_type,
                status, origin_mode, source_patch_ids)
           VALUES ($1, 'row', $2, $3::jsonb, $4, 'meeting', $5,
                   COALESCE($6, 'inferred'), $7::text[])""",
        pid, ptype, json.dumps(value), origin_id, status, origin_mode, sources)
    await conn.execute(
        "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
        pid, f"user:{user_id}")
    return pid


async def _entity(conn, user_id, name, etype="person", meetings=()):
    eid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO entities (entity_id, user_id, name, entity_type) VALUES ($1, $2, $3, $4)",
        eid, user_id, name, etype)
    for m in meetings:
        await conn.execute(
            """INSERT INTO person_appearances (user_id, entity_id, origin_id, origin_type)
               VALUES ($1, $2, $3, 'meeting')""",
            user_id, eid, m)
    return eid


async def _value(conn, pid):
    return json.loads(await conn.fetchval(
        "SELECT value::text FROM context_patches WHERE patch_id = $1", pid))


async def _apply(conn, user_id, origin_id, term="Cora", to="Kore.ai", match_case=False):
    async with conn.transaction():
        return await tr.apply(conn, user_id, "meeting", origin_id,
                              tr.compile_term(term, match_case), to, term=term)


@pytest.mark.asyncio
async def test_every_statement_runs_on_an_unknown_meeting():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        r = await _apply(conn, str(uuid.uuid4()), "no-such-meeting")
        assert r["matches"] == 0 and r["patch_ids"] == []
        assert r["entities_renamed"] == [] and r["derived_referencing"] == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_this_meetings_facts_are_rewritten_in_every_status_and_no_other():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u, m = str(uuid.uuid4()), f"m-{uuid.uuid4().hex[:8]}"
        live = await _patch(conn, u, m, {"text": "Send Cora the deck", "owner": "Cora"})
        done = await _patch(conn, u, m, {"text": "Cora pricing call"}, status="archived")
        untouched = await _patch(conn, u, m, {"text": "Coral reef survey"})
        other = await _patch(conn, u, "m-other", {"text": "Cora elsewhere"})
        before = await conn.fetchval(
            "SELECT updated_at FROM context_patches WHERE patch_id = $1", live)

        r = await _apply(conn, u, m)

        assert r["matches"] == 3
        assert sorted(r["patch_ids"]) == sorted([str(live), str(done)])
        assert (await _value(conn, live))["text"] == "Send Kore.ai the deck"
        assert (await _value(conn, live))["owner"] == "Kore.ai"
        assert (await _value(conn, done))["text"] == "Kore.ai pricing call"
        assert (await _value(conn, untouched))["text"] == "Coral reef survey"
        assert (await _value(conn, other))["text"] == "Cora elsewhere"
        after = await conn.fetchval(
            "SELECT updated_at FROM context_patches WHERE patch_id = $1", live)
        assert after != before, "the delta needs updated_at to carry the fix"
        mode = await conn.fetchval(
            "SELECT origin_mode FROM context_patches WHERE patch_id = $1", live)
        assert mode == "inferred"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_repeat_changes_nothing():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u, m = str(uuid.uuid4()), f"m-{uuid.uuid4().hex[:8]}"
        await _patch(conn, u, m, {"text": "Cora said yes"})
        assert (await _apply(conn, u, m))["matches"] == 1
        again = await _apply(conn, u, m)
        assert again["matches"] == 0 and again["patch_ids"] == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_cues_are_rewritten():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u, m = str(uuid.uuid4()), f"m-{uuid.uuid4().hex[:8]}"
        p = await _patch(conn, u, m, {"text": "pricing"})
        await conn.execute("INSERT INTO patch_cues (patch_id, cue) VALUES ($1, 'cora pricing')", p)
        await conn.execute("INSERT INTO patch_cues (patch_id, cue) VALUES ($1, 'coral reef')", p)
        r = await _apply(conn, u, m)
        cues = sorted(c["cue"] for c in await conn.fetch(
            "SELECT cue FROM patch_cues WHERE patch_id = $1", p))
        assert r["cues_changed"] == 1
        assert cues == ["coral reef", "kore.ai pricing"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_entities_are_renamed_only_when_provably_this_meetings():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u, m = str(uuid.uuid4()), f"m-{uuid.uuid4().hex[:8]}"
        await _patch(conn, u, m, {"text": "anything"})
        only_here = await _entity(conn, u, "Cora Lee", meetings=[m])
        shared = await _entity(conn, u, "Cora", meetings=[m, "m-other"])
        company = await _entity(conn, u, "Cora Labs", etype="company")
        await _entity(conn, u, "Kore.ai Smith", meetings=["m-x"])
        blocked = await _entity(conn, u, "Cora Smith", meetings=[m])
        unrelated = await _entity(conn, u, "Coral Ng", meetings=[m])

        r = await _apply(conn, u, m)

        names = {e["entity_id"]: e for e in r["entities_renamed"]}
        assert set(names) == {str(only_here)}
        assert names[str(only_here)]["new_name"] == "Kore.ai Lee"
        reasons = {e["entity_id"]: e["reason"] for e in r["entities_skipped"]}
        assert reasons == {
            str(shared): tr.SKIP_OTHER_MEETINGS,
            str(company): tr.SKIP_NO_MEETING_RECORD,
            str(blocked): tr.SKIP_TARGET_EXISTS,
        }
        stored = {row["entity_id"]: row["name"] for row in await conn.fetch(
            "SELECT entity_id, name FROM entities WHERE user_id = $1", u)}
        assert stored[only_here] == "Kore.ai Lee"
        assert stored[shared] == "Cora"
        assert stored[unrelated] == "Coral Ng"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_like_wildcards_in_the_term_are_literal():
    """An underscore or percent in the term must not widen the ILIKE
    prefilter into matching everything (the pattern would still refuse
    the rows, but this pins the escape the SQL depends on)."""
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u, m = str(uuid.uuid4()), f"m-{uuid.uuid4().hex[:8]}"
        await _patch(conn, u, m, {"text": "anything"})
        await _entity(conn, u, "Abc", meetings=[m])
        rows = await conn.fetch(tr.ENTITY_CANDIDATES_SQL, u, tr.like_escape("_%"))
        assert rows == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_derived_prose_is_counted_not_edited():
    await _ensure_schema()
    conn = await asyncpg.connect(TEST_DB)
    try:
        u, m = str(uuid.uuid4()), f"m-{uuid.uuid4().hex[:8]}"
        src = await _patch(conn, u, m, {"text": "Cora deck"})
        card = await _patch(conn, u, None, {"text": "Often follows up with Cora"},
                            ptype="insight", origin_mode="derived", sources=[str(src)])
        quiet = await _patch(conn, u, None, {"text": "Prefers mornings"},
                             ptype="insight", origin_mode="derived", sources=[str(src)])
        r = await _apply(conn, u, m)
        assert r["derived_referencing"] == 1
        assert (await _value(conn, card))["text"] == "Often follows up with Cora"
        assert (await _value(conn, quiet))["text"] == "Prefers mornings"
    finally:
        await conn.close()
