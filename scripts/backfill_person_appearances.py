#!/usr/bin/env python3
"""
Reconstruct person_appearances for history (docs/architecture/16-people.md 8c).

person_appearances (migration 30) only fills forward from the next
extraction. Without a backfill, every person the user has ever met shows
a zero meeting count on day one and the People feature reads as knowing
nothing, which is the opposite of the point.

TWO TIERS, and the split matters.

  Tier 1, Postgres-derived. Complete over all patch history with no
  retention dependency. Every patch carries `origin_id`, and ownership is
  recorded two ways: a raw `value.owner` string, and an explicit `owns`
  connection from a person patch. Both resolve to a person entity through
  the same exact-then-alias path store_entities uses. Deterministic, no
  LLM call.

  Tier 2, stream-derived. Tier 1 can only see people who OWNED something.
  Someone named in a meeting who owned nothing produces no owner-bearing
  patch and is invisible to it. The ingest stream keeps the original
  request `content`, so scanning retained transcripts for known entity
  names recovers those. Bounded by whatever the stream still holds:
  `memory_updates` has no settled MAXLEN policy, which is exactly why
  tier 2 cannot be the only tier.

Both tiers are idempotent and safe to re-run. Timestamps come from the
source patch or stream entry, never NOW(): setting them to import time
would make `last_seen_at DESC` meaningless and would tell the app every
meeting happened today.

USAGE

    DATABASE_URL=... python scripts/backfill_person_appearances.py
    DATABASE_URL=... python scripts/backfill_person_appearances.py --apply
    DATABASE_URL=... python scripts/backfill_person_appearances.py --tier 1 --apply
    ... --user <user_id>     restrict to one user
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Compound owners ("Brian/Sarah") are split the same way the extraction
# sanitizers split them, so this agrees with what the worker would record.
from contextquilt.services.extraction_schema import (  # noqa: E402
    _split_compound_owner,
    is_placeholder_or_self_person,
)

# A surface form shorter than this is not matched in free text: "Ann"
# would fire inside "Announce", and a backfill that writes noise is worse
# than one that misses a row.
MIN_FREE_TEXT_NAME = 4


async def load_people(conn, user_id: str | None):
    """surface form (lowercased) -> canonical entity_id, per user.

    Merged entities resolve forward, so a name folded into someone else
    lands on the survivor rather than reviving the dead row.
    """
    rows = await conn.fetch(
        """
        SELECT e.entity_id, e.user_id, e.name, e.merged_into
        FROM entities e
        WHERE e.entity_type = 'person'
          AND ($1::text IS NULL OR e.user_id = $1)
        """,
        user_id,
    )
    merged = {str(r["entity_id"]): str(r["merged_into"]) for r in rows if r["merged_into"]}

    def resolve(eid: str) -> str:
        seen = set()
        while eid in merged and eid not in seen:
            seen.add(eid)
            eid = merged[eid]
        return eid

    forms: dict[str, dict[str, str]] = defaultdict(dict)
    canonical_name: dict[str, str] = {}
    for r in rows:
        eid = resolve(str(r["entity_id"]))
        canonical_name[eid] = r["name"]
        if r["name"] and not is_placeholder_or_self_person(r["name"]):
            forms[r["user_id"]][r["name"].strip().lower()] = eid

    alias_rows = await conn.fetch(
        """
        SELECT a.user_id, a.alias, a.entity_id
        FROM entity_aliases a
        JOIN entities e ON e.entity_id = a.entity_id
        WHERE e.entity_type = 'person'
          AND ($1::text IS NULL OR a.user_id = $1)
        """,
        user_id,
    )
    for r in alias_rows:
        if r["alias"] and not is_placeholder_or_self_person(r["alias"]):
            forms[r["user_id"]][r["alias"].strip().lower()] = resolve(str(r["entity_id"]))

    return forms, canonical_name


async def tier1(conn, forms, user_id: str | None):
    """Owner strings and `owns` edges on origin-bearing patches."""
    found: dict[tuple, dict] = {}

    def record(uid, eid, origin_id, origin_type, project_id, ts):
        key = (uid, eid, origin_id)
        cur = found.get(key)
        if cur is None:
            found[key] = {
                "user_id": uid, "entity_id": eid, "origin_id": origin_id,
                "origin_type": origin_type or "meeting", "project_id": project_id,
                "first": ts, "last": ts,
            }
            return
        if ts and cur["first"] and ts < cur["first"]:
            cur["first"] = ts
        if ts and cur["last"] and ts > cur["last"]:
            cur["last"] = ts
        if cur["project_id"] is None:
            cur["project_id"] = project_id

    # 1a. Raw value.owner on anything anchored to an origin.
    owner_rows = await conn.fetch(
        """
        SELECT ps.subject_key, cp.origin_id, cp.origin_type, cp.project_id,
               cp.created_at, cp.value->>'owner' AS owner
        FROM context_patches cp
        JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
        WHERE cp.origin_id IS NOT NULL
          AND cp.value->>'owner' IS NOT NULL
          AND ($1::text IS NULL OR ps.subject_key = 'user:' || $1)
        """,
        user_id,
    )
    for r in owner_rows:
        uid = r["subject_key"].removeprefix("user:")
        table = forms.get(uid) or {}
        for part in _split_compound_owner(r["owner"]):
            eid = table.get((part or "").strip().lower())
            if eid:
                record(uid, eid, r["origin_id"], r["origin_type"],
                       r["project_id"], r["created_at"])

    # 1b. Explicit `owns` edges: person patch -> item anchored to an origin.
    owns_rows = await conn.fetch(
        """
        SELECT ps.subject_key, tgt.origin_id, tgt.origin_type, tgt.project_id,
               tgt.created_at, src.value->>'text' AS person_text
        FROM patch_connections pc
        JOIN context_patches src ON src.patch_id = pc.from_patch_id
        JOIN context_patches tgt ON tgt.patch_id = pc.to_patch_id
        JOIN patch_subjects ps ON ps.patch_id = tgt.patch_id
        WHERE pc.connection_label = 'owns'
          AND src.patch_type = 'person'
          AND tgt.origin_id IS NOT NULL
          AND ($1::text IS NULL OR ps.subject_key = 'user:' || $1)
        """,
        user_id,
    )
    for r in owns_rows:
        uid = r["subject_key"].removeprefix("user:")
        eid = (forms.get(uid) or {}).get((r["person_text"] or "").strip().lower())
        if eid:
            record(uid, eid, r["origin_id"], r["origin_type"],
                   r["project_id"], r["created_at"])

    return found


async def tier2(redis_url, forms, user_id, existing_keys):
    """Mention-level appearances from retained transcripts.

    Only adds keys tier 1 did not already find, so the cheaper and more
    certain signal always wins on timestamps and project scope.
    """
    try:
        import redis.asyncio as redis
    except ImportError:
        print("  tier 2 skipped: redis package unavailable", file=sys.stderr)
        return {}

    client = redis.from_url(redis_url, decode_responses=True)
    try:
        entries = await client.xrange("memory_updates", "-", "+")
    except Exception as e:
        print(f"  tier 2 skipped: cannot read stream ({str(e)[:80]})", file=sys.stderr)
        return {}
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

    found: dict[tuple, dict] = {}
    patterns: dict[str, list] = {}

    for _sid, fields in entries:
        raw = fields.get("data")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        content = payload.get("content")
        meta = payload.get("metadata") or {}
        uid = payload.get("user_id")
        origin_id = meta.get("origin_id")
        if not (content and uid and origin_id):
            continue
        if user_id and uid != user_id:
            continue

        table = forms.get(uid) or {}
        if uid not in patterns:
            patterns[uid] = [
                (re.compile(rf"\b{re.escape(form)}\b", re.IGNORECASE), eid)
                for form, eid in table.items()
                if len(form) >= MIN_FREE_TEXT_NAME
            ]

        ts = payload.get("timestamp")
        for rx, eid in patterns[uid]:
            key = (uid, eid, str(origin_id))
            if key in existing_keys or key in found:
                continue
            if rx.search(content):
                found[key] = {
                    "user_id": uid, "entity_id": eid, "origin_id": str(origin_id),
                    "origin_type": meta.get("origin_type") or "meeting",
                    "project_id": meta.get("project_id"),
                    "first": ts, "last": ts,
                }
    return found


async def write(conn, rows) -> int:
    written = 0
    for r in rows.values():
        await conn.execute(
            """
            INSERT INTO person_appearances
                (user_id, entity_id, origin_id, origin_type, project_id,
                 first_seen_at, last_seen_at)
            VALUES ($1, $2::uuid, $3, $4, $5,
                    COALESCE($6::timestamptz, NOW()),
                    COALESCE($7::timestamptz, NOW()))
            ON CONFLICT (user_id, entity_id, origin_id) DO UPDATE SET
                first_seen_at = LEAST(person_appearances.first_seen_at,
                                      EXCLUDED.first_seen_at),
                last_seen_at  = GREATEST(person_appearances.last_seen_at,
                                         EXCLUDED.last_seen_at),
                project_id    = COALESCE(person_appearances.project_id,
                                         EXCLUDED.project_id)
            """,
            r["user_id"], r["entity_id"], r["origin_id"], r["origin_type"],
            r["project_id"], r["first"], r["last"],
        )
        written += 1
    return written


async def main(args) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is required")

    conn = await asyncpg.connect(dsn)
    try:
        before = await conn.fetchval("SELECT count(*) FROM person_appearances")
        forms, canonical = await load_people(conn, args.user)
        print(f"known person surface forms: {sum(len(v) for v in forms.values())} "
              f"across {len(forms)} users")

        rows: dict = {}
        if args.tier in ("1", "both"):
            t1 = await tier1(conn, forms, args.user)
            print(f"tier 1 (owner strings + owns edges): {len(t1)} appearances")
            rows.update(t1)

        if args.tier in ("2", "both"):
            redis_url = os.environ.get("REDIS_URL")
            if not redis_url:
                host = os.environ.get("REDIS_HOST", "localhost")
                port = os.environ.get("REDIS_PORT", "6379")
                pw = os.environ.get("REDIS_PASSWORD")
                redis_url = f"redis://:{pw}@{host}:{port}" if pw else f"redis://{host}:{port}"
            t2 = await tier2(redis_url, forms, args.user, set(rows.keys()))
            print(f"tier 2 (retained transcripts):       {len(t2)} additional")
            rows.update(t2)

        people = len({(k[0], k[1]) for k in rows})
        meetings = len({(k[0], k[2]) for k in rows})
        print(f"\ntotal: {len(rows)} appearance rows, {people} people, {meetings} meetings")

        top = sorted(
            ((sum(1 for k in rows if k[1] == eid), eid) for eid in {k[1] for k in rows}),
            reverse=True,
        )[:8]
        print("busiest people:")
        for n, eid in top:
            print(f"  {n:4d} meetings  {canonical.get(eid, eid)}")

        if args.apply:
            written = await write(conn, rows)
            after = await conn.fetchval("SELECT count(*) FROM person_appearances")
            print(f"\nAPPLIED: {written} upserts, table {before} -> {after}")
        else:
            print(f"\nDRY RUN (use --apply to write). Table currently holds {before} rows.")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default is dry run)")
    ap.add_argument("--tier", choices=["1", "2", "both"], default="both")
    ap.add_argument("--user", help="restrict to one user_id")
    asyncio.run(main(ap.parse_args()))
