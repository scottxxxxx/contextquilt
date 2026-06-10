"""
Backfill origin_id/origin_type on role patches that missed the May fix.

Background
----------

Before 2026-05-04, store_connected_patches set project/project_id on
role patches (when they had a belongs_to parent) but not origin_id —
ShoulderSurf's null-origin detector surfaced this and the forward path
was fixed. Five rows written 2026-04-22/23 predate that fix and were
never backfilled. SS's 2026-06-10 report lists them explicitly.

What it does
------------

For every active role patch with project_id set and origin_id NULL,
look for sibling patches in the same project written at the exact same
created_at (one extraction batch shares a single timestamp). If the
siblings agree on exactly one (origin_id, origin_type), copy it onto
the role patch. Ambiguous or sibling-less rows are reported and
skipped.

Read-only by default; `--apply` writes.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/backfill_role_origins.py
    DATABASE_URL='postgres://...' python scripts/backfill_role_origins.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg


async def main(apply: bool) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT patch_id, project, project_id, created_at
              FROM context_patches
             WHERE patch_type = 'role'
               AND project_id IS NOT NULL
               AND origin_id IS NULL
               AND COALESCE(status, 'active') = 'active'
             ORDER BY created_at
            """
        )
        print(f"{len(rows)} active role patches with project_id and NULL origin_id")

        fixed = skipped = 0
        for r in rows:
            origins = await conn.fetch(
                """
                SELECT DISTINCT origin_id, origin_type
                  FROM context_patches
                 WHERE project_id = $1
                   AND created_at = $2
                   AND origin_id IS NOT NULL
                """,
                r["project_id"], r["created_at"],
            )
            if len(origins) != 1:
                print(f"  SKIP {r['patch_id']} ({r['project']}): {len(origins)} candidate origins")
                skipped += 1
                continue
            origin_id, origin_type = origins[0]["origin_id"], origins[0]["origin_type"]
            print(f"  FIX  {r['patch_id']} ({r['project']}) -> origin_id={origin_id}")
            if apply:
                await conn.execute(
                    """
                    UPDATE context_patches
                       SET origin_id = $1, origin_type = $2, updated_at = NOW()
                     WHERE patch_id = $3
                    """,
                    origin_id, origin_type, r["patch_id"],
                )
            fixed += 1

        mode = "APPLIED" if apply else "DRY RUN (use --apply to write)"
        print(f"\n{mode}: {fixed} fixed, {skipped} skipped")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
