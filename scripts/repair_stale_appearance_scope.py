#!/usr/bin/env python3
"""Presence rows still stamped with a project that was unscoped.

`POST /v1/projects/{u}/{pid}/unscope` cleared `context_patches` and not
`person_appearances` until 2026-09-03. Its documented mirror,
`unassign-project`, cleared both. So every project deleted before that
fix left its presence rows pointing at it. The route is fixed; this is
the repair for the history it already made.

WHY THIS IS A REPORT FIRST AND A WRITE ONLY IF ASKED. There is no record
of which projects were unscoped, so the candidate has to be inferred, and
the obvious inference is a proxy: "a project with no patches left". That
is TRUE of an unscoped project and also true of a project that simply has
not accumulated anything yet, which is a real state for a project created
minutes ago. The two are indistinguishable from the data, so the default
is to print them grouped by project and let a human look at the names
before anything is nulled. A wrong null here does not lose a person, it
loses the fact that the person was in that project's meetings, which is
not recoverable from anywhere else.

`--apply` requires `--project-id` naming exactly which projects to clear,
because a blanket write on a proxy predicate is the shape of the bug this
repairs.

    DATABASE_URL=... python scripts/repair_stale_appearance_scope.py
    DATABASE_URL=... python scripts/repair_stale_appearance_scope.py \
        --apply --project-id A --project-id B
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

REPORT_SQL = """
    SELECT pa.project_id,
           p.name                                   AS project_name,
           count(*)                                 AS appearance_rows,
           count(DISTINCT pa.entity_id)             AS people,
           count(DISTINCT pa.origin_id)             AS meetings,
           (SELECT count(*) FROM context_patches cp
             WHERE cp.project_id = pa.project_id)   AS patches_any_status,
           min(pa.last_seen_at)::date               AS oldest,
           max(pa.last_seen_at)::date               AS newest
    FROM person_appearances pa
    LEFT JOIN projects p ON p.project_id = pa.project_id
    WHERE pa.project_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM context_patches cp
                      WHERE cp.project_id = pa.project_id)
    GROUP BY pa.project_id, p.name
    ORDER BY count(*) DESC
"""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--project-id", action="append", default=[],
                    help="repeatable; required with --apply")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(REPORT_SQL)
        if not rows:
            # Names itself: "none found" and "the query is wrong" must
            # not share a silence.
            total = await conn.fetchval(
                "SELECT count(*) FROM person_appearances WHERE project_id IS NOT NULL")
            print("No presence rows point at a project with zero patches. "
                  f"({total} scoped appearance rows exist overall, so the "
                  "query did run against data.)")
            return 0

        print("Presence rows scoped to a project that has NO patches left.")
        print("A project deleted before 2026-09-03 looks exactly like a project")
        print("created five minutes ago. Read the names before clearing anything.\n")
        total_rows = 0
        for r in rows:
            total_rows += r["appearance_rows"]
            print(f"  {r['appearance_rows']:>4} rows  {r['people']:>3} people  "
                  f"{r['meetings']:>3} meetings  {r['oldest']}..{r['newest']}  "
                  f"{r['project_name']!r}  ({r['project_id']})")
        print(f"\n{total_rows} appearance rows across {len(rows)} projects.")

        if not args.apply:
            print("\nDRY RUN. To clear, name the projects explicitly:")
            print("  --apply --project-id <id> [--project-id <id> ...]")
            return 0

        if not args.project_id:
            print("\n--apply requires at least one --project-id. A blanket write "
                  "on a proxy predicate is the shape of the bug this repairs.",
                  file=sys.stderr)
            return 2

        known = {r["project_id"] for r in rows}
        unknown = [p for p in args.project_id if p not in known]
        if unknown:
            print(f"\nThese are not in the report and will not be touched: {unknown}",
                  file=sys.stderr)
            return 2

        result = await conn.execute(
            "UPDATE person_appearances SET project_id = NULL "
            "WHERE project_id = ANY($1::text[])",
            args.project_id,
        )
        print(f"\ncleared: {result}")
        print("The rows survive with their scope removed. Presence was never "
              "the project's to destroy.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
