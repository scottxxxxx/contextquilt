#!/usr/bin/env python3
"""Archive orphan project patches and remove Speaker-N entities.

Two cleanups agreed in the context-flow contract (item 7), both
pre-cleared by the SS team (2026-07-17):

1. ORPHAN PROJECT PATCHES — patch_type='project', origin_mode='inferred',
   active, with NO companion patches referencing their text via the
   `project` column and NO connections. These are extraction
   over-extraction (milestones typed as projects); they surface as dead
   filter chips. ARCHIVED, never hard-deleted: archived status flows
   through the quilt delta `deleted` array so clients converge on sync
   (the hard-delete tombstone lesson, 2026-07-15).

2. SPEAKER-N ENTITIES — entities named like "Speaker 4": diarization
   labels that leaked past the unnamed-speaker extraction rule (all
   known cases predate it). Removed along with their relationship edges
   and alias rows. The Redis entity index self-heals within its TTL or
   on the next extraction rebuild.

Dry-run by default; --apply executes each cleanup in a transaction.

Usage:
    DATABASE_URL=postgres://... python scripts/cleanup_orphan_memory.py
    DATABASE_URL=postgres://... python scripts/cleanup_orphan_memory.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

ORPHAN_PROJECTS_SQL = """
    SELECT p.patch_id, ps.subject_key, left(p.value->>'text', 70) AS text,
           p.created_at::date AS created
    FROM context_patches p
    JOIN patch_subjects ps ON ps.patch_id = p.patch_id
    WHERE p.patch_type = 'project'
      AND p.origin_mode = 'inferred'
      AND COALESCE(p.status, 'active') = 'active'
      AND NOT EXISTS (
          SELECT 1 FROM context_patches c
          WHERE c.project = p.value->>'text'
            AND COALESCE(c.status, 'active') = 'active'
      )
      AND NOT EXISTS (
          SELECT 1 FROM patch_connections pc
          WHERE pc.from_patch_id = p.patch_id OR pc.to_patch_id = p.patch_id
      )
    ORDER BY ps.subject_key, p.created_at
"""

SPEAKER_ENTITIES_SQL = """
    SELECT entity_id, user_id, name, first_seen_at::date AS first_seen
    FROM entities
    WHERE name ~* '^speaker ?[0-9]+$'
    ORDER BY user_id, first_seen_at
"""


async def run(database_url: str, apply: bool) -> int:
    conn = await asyncpg.connect(database_url)
    try:
        orphans = await conn.fetch(ORPHAN_PROJECTS_SQL)
        speakers = await conn.fetch(SPEAKER_ENTITIES_SQL)

        print(f"— Orphan project patches to ARCHIVE: {len(orphans)}")
        for r in orphans:
            print(f"    {r['patch_id']}  {r['subject_key']:<45} {r['created']}  {r['text']}")
        print(f"\n— Speaker-N entities to REMOVE: {len(speakers)}")
        for r in speakers:
            print(f"    {r['entity_id']}  user={r['user_id']}  {r['name']}  (first seen {r['first_seen']})")

        if not apply:
            print("\nDry run — pass --apply to execute.")
            return 0

        async with conn.transaction():
            if orphans:
                archived = await conn.execute(
                    """
                    UPDATE context_patches SET status = 'archived', updated_at = NOW(),
                        value = jsonb_set(value, '{archive_cause}', '"cleanup"')
                    WHERE patch_id = ANY($1::uuid[])
                      AND COALESCE(status, 'active') = 'active'
                    """,
                    [r["patch_id"] for r in orphans],
                )
                print(f"\nArchived: {archived}")
            if speakers:
                ids = [r["entity_id"] for r in speakers]
                rels = await conn.execute(
                    "DELETE FROM relationships WHERE from_entity_id = ANY($1::uuid[]) OR to_entity_id = ANY($1::uuid[])",
                    ids,
                )
                aliases = await conn.execute(
                    "DELETE FROM entity_aliases WHERE entity_id = ANY($1::uuid[])", ids
                )
                ents = await conn.execute(
                    "DELETE FROM entities WHERE entity_id = ANY($1::uuid[])", ids
                )
                print(f"Removed: {ents} (relationships: {rels}, aliases: {aliases})")
        print("Done. Entity index self-heals within TTL; archived patches flow via delta sync.")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        return 1
    return asyncio.run(run(database_url, args.apply))


if __name__ == "__main__":
    sys.exit(main())
