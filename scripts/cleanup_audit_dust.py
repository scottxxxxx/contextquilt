#!/usr/bin/env python3
"""One-time sweep for the 2026-07-23 memory-audit dust (finding 6).

Three surgical cleanups, all archive/null — never hard-delete (archived
status flows through the quilt delta `deleted` array; the tombstone
lesson, 2026-07-15):

1. WRONG-YEAR DEADLINE DATES — active patches whose `value.deadline_date`
   sits more than 180 days before the patch's own created_at. That shape
   is a hallucinated year from the pre-guard era (the live window is now
   60 days against the meeting date; this sweep uses 180 against
   created_at so stream-replayed historical meetings are never touched).
   The bogus `deadline_date` and its derived `overdue_since` stamp are
   nulled; `value.deadline` (as spoken) and the patch itself are kept.

2. DUPLICATE PROJECT CHIPS — active project-type patches sharing a
   subject and identical (case-folded) text. Keeper = earliest
   created_at (the locked keeper rule); the rest are archived.

3. AND-JOINED PERSON PATCHES — person patches whose text is exactly
   "<Name> and <Name>". The live enforcer deliberately does not split
   on " and " (legitimate "Pinehurst and Family" style names), so these are
   archived, not split. Second tokens that make the phrase a plausible
   single unit (family, team, co, sons, associates) are excluded.

Dry-run by default; --apply executes each cleanup in a transaction.

Usage:
    DATABASE_URL=postgres://... python scripts/cleanup_audit_dust.py
    DATABASE_URL=postgres://... python scripts/cleanup_audit_dust.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

WRONG_YEAR_DEADLINES_SQL = """
    SELECT p.patch_id, ps.subject_key, p.patch_type,
           p.value->>'deadline_date' AS deadline_date,
           p.created_at::date AS created,
           left(p.value->>'text', 60) AS text
    FROM context_patches p
    JOIN patch_subjects ps ON ps.patch_id = p.patch_id
    WHERE COALESCE(p.status, 'active') = 'active'
      AND p.value->>'deadline_date' ~ '^\\d{4}-\\d{2}-\\d{2}$'
      AND (p.value->>'deadline_date')::date < p.created_at::date - 180
    ORDER BY p.value->>'deadline_date'
"""

NULL_DEADLINE_SQL = """
    UPDATE context_patches
    SET value = (value - 'deadline_date') - 'overdue_since',
        updated_at = NOW()
    WHERE patch_id = ANY($1)
"""

DUP_PROJECT_CHIPS_SQL = """
    SELECT p.patch_id, ps.subject_key, left(p.value->>'text', 60) AS text,
           p.created_at
    FROM context_patches p
    JOIN patch_subjects ps ON ps.patch_id = p.patch_id
    WHERE p.patch_type = 'project'
      AND COALESCE(p.status, 'active') = 'active'
      AND EXISTS (
          SELECT 1 FROM context_patches q
          JOIN patch_subjects qs ON qs.patch_id = q.patch_id
          WHERE q.patch_type = 'project'
            AND COALESCE(q.status, 'active') = 'active'
            AND q.patch_id <> p.patch_id
            AND qs.subject_key = ps.subject_key
            AND lower(q.value->>'text') = lower(p.value->>'text')
      )
    ORDER BY ps.subject_key, lower(p.value->>'text'), p.created_at
"""

# Second tokens that make an and-joined phrase read as one unit, not two
# people. Case-insensitive.
_AND_JOIN_UNIT_WORDS = ("family", "team", "co", "sons", "associates", "partners")

AND_JOINED_PERSONS_SQL = """
    SELECT p.patch_id, ps.subject_key, p.value->>'text' AS text,
           p.created_at::date AS created
    FROM context_patches p
    JOIN patch_subjects ps ON ps.patch_id = p.patch_id
    WHERE p.patch_type = 'person'
      AND COALESCE(p.status, 'active') = 'active'
      AND p.value->>'text' ~ '^[A-Z][A-Za-z''-]+ and [A-Z][A-Za-z''-]+$'
      AND lower(split_part(p.value->>'text', ' and ', 2)) <> ALL($1::text[])
    ORDER BY ps.subject_key, p.created_at
"""

ARCHIVE_SQL = """
    UPDATE context_patches
    SET status = 'archived', updated_at = NOW(),
        value = jsonb_set(value, '{archive_cause}', '"cleanup"')
    WHERE patch_id = ANY($1)
"""


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL env required")
    conn = await asyncpg.connect(dsn)
    try:
        # --- 1. wrong-year deadlines -------------------------------
        rows = await conn.fetch(WRONG_YEAR_DEADLINES_SQL)
        print(f"[1] wrong-year deadline_date rows: {len(rows)}")
        for r in rows:
            print(f"    {r['patch_id']}  {r['patch_type']:<11} deadline={r['deadline_date']} "
                  f"created={r['created']}  {r['text']!r}")
        deadline_ids = [r["patch_id"] for r in rows]

        # --- 2. duplicate project chips ----------------------------
        rows = await conn.fetch(DUP_PROJECT_CHIPS_SQL)
        keep: dict[tuple, object] = {}
        for r in rows:  # ordered by created_at, first seen per key wins
            keep.setdefault((r["subject_key"], r["text"].lower()), r["patch_id"])
        dup_ids = [r["patch_id"] for r in rows
                   if keep[(r["subject_key"], r["text"].lower())] != r["patch_id"]]
        print(f"[2] duplicate project chips: {len(rows)} in dup groups, archiving {len(dup_ids)}")
        for r in rows:
            marker = "KEEP  " if keep[(r["subject_key"], r["text"].lower())] == r["patch_id"] else "ARCHIVE"
            print(f"    {marker} {r['patch_id']}  {r['created_at']:%Y-%m-%d}  {r['text']!r}")

        # --- 3. and-joined person patches --------------------------
        rows = await conn.fetch(AND_JOINED_PERSONS_SQL, list(_AND_JOIN_UNIT_WORDS))
        print(f"[3] and-joined person patches: {len(rows)}")
        for r in rows:
            print(f"    {r['patch_id']}  created={r['created']}  {r['text']!r}")
        person_ids = [r["patch_id"] for r in rows]

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to execute.")
            return

        async with conn.transaction():
            if deadline_ids:
                await conn.execute(NULL_DEADLINE_SQL, deadline_ids)
            if dup_ids or person_ids:
                await conn.execute(ARCHIVE_SQL, dup_ids + person_ids)
        print(f"\nAPPLIED: {len(deadline_ids)} deadlines nulled, "
              f"{len(dup_ids)} dup chips + {len(person_ids)} and-joined persons archived.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
