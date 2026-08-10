"""Remap person_appearances.project_id stamps that point at ARCHIVED
projects, when exactly one ACTIVE project with the same name exists.

Why this exists (found 2026-08-10, exposed by the Vijay 5-way merge):
appearances carry the project_id that was live at ingest time. The ABM
project-id split was canonicalized in June (keeper 10437AFE), but the
person_appearances table shipped LATER (migration 30) and its backfill
stamped ids from source patches written in April/May, so six dead ABM
ids live on in appearance rows. The person detail's projects[] rollup
keys on (project_id, name), so one human renders "ABM" seven times and
project_count counts all seven.

Safety properties:
- DRY RUN by default; --apply writes.
- No hardcoded ids: a stamp is remapped only when its project row is
  status='archived' AND exactly ONE active project shares the exact
  name (the unique-candidate guard; the entity-aliasing rule). Zero or
  two-plus active candidates: reported, untouched.
- A remap is a plain in-place UPDATE: the PK is (user, entity, origin),
  one row per (person, meeting) regardless of project_id, so collision
  is unrepresentable.
- Also REPORTS (never touches) context_patches rows carrying archived
  project ids, so the companion question is answered with numbers.

Usage:
    python scripts/backfill_remap_archived_project_stamps.py [--apply]
"""

import argparse
import asyncio
import os
import sys

import asyncpg

FIND_REMAPPABLE = """
    SELECT pa.user_id, pa.entity_id, pa.origin_id,
           pa.project_id AS dead_id, dead.name,
           act.project_id AS canonical_id
    FROM person_appearances pa
    JOIN projects dead ON dead.project_id = pa.project_id
    JOIN LATERAL (
        SELECT p2.project_id
        FROM projects p2
        WHERE p2.name = dead.name
          AND COALESCE(p2.status, 'active') = 'active'
    ) act ON TRUE
    WHERE COALESCE(dead.status, 'active') = 'archived'
      AND (
        SELECT count(*) FROM projects p3
        WHERE p3.name = dead.name
          AND COALESCE(p3.status, 'active') = 'active'
      ) = 1
    ORDER BY dead.name, pa.user_id, pa.origin_id
"""

FIND_UNRESOLVABLE = """
    SELECT dead.name, pa.project_id AS dead_id, count(*) AS stamps,
           (SELECT count(*) FROM projects p3
            WHERE p3.name = dead.name
              AND COALESCE(p3.status, 'active') = 'active') AS active_candidates
    FROM person_appearances pa
    JOIN projects dead ON dead.project_id = pa.project_id
    WHERE COALESCE(dead.status, 'active') = 'archived'
    GROUP BY dead.name, pa.project_id
    HAVING (SELECT count(*) FROM projects p3
            WHERE p3.name = dead.name
              AND COALESCE(p3.status, 'active') = 'active') <> 1
    ORDER BY dead.name
"""

REPORT_PATCH_STAMPS = """
    SELECT dead.name, cp.project_id AS dead_id,
           count(*) AS patches,
           count(*) FILTER (WHERE COALESCE(cp.status,'active')='active') AS active_patches
    FROM context_patches cp
    JOIN projects dead ON dead.project_id = cp.project_id
    WHERE COALESCE(dead.status, 'active') = 'archived'
    GROUP BY dead.name, cp.project_id
    ORDER BY dead.name
"""

# The PK is (user_id, entity_id, origin_id): exactly one row per
# (person, meeting) regardless of project_id, so a remap can never
# collide with another row and is a plain in-place UPDATE. (First draft
# carried the merge's fold-on-conflict machinery; the PK makes that
# case unrepresentable.)
REMAP_ONE = """
    UPDATE person_appearances
    SET project_id = $4
    WHERE user_id = $1 AND entity_id = $2::uuid AND origin_id = $3
      AND project_id = $5
"""


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    parser.add_argument(
        "--treat-as", action="append", default=[], metavar="DEAD=ACTIVE",
        help="Fold stamps whose ARCHIVED project is named DEAD into the "
             "unique ACTIVE project named ACTIVE. For human-adjudicated "
             "cases the unique-candidate guard cannot resolve (e.g. an ASR "
             "mangle: 'All Bravo Mark=ABM', Scott's call 2026-08-10). The "
             "target must still resolve to exactly one active project.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is required")
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(FIND_REMAPPABLE)
        by_dead: dict = {}
        for r in rows:
            key = (r["name"], str(r["dead_id"]), str(r["canonical_id"]))
            by_dead[key] = by_dead.get(key, 0) + 1
        print(f"Remappable stamps: {len(rows)}")
        for (name, dead, canon), n in sorted(by_dead.items()):
            print(f"  {name!r}: {dead} -> {canon}  ({n} stamps)")

        unresolvable = await conn.fetch(FIND_UNRESOLVABLE)
        if unresolvable:
            print("\nUNRESOLVABLE (0 or 2+ active same-name candidates; untouched):")
            for r in unresolvable:
                print(f"  {r['name']!r} dead={r['dead_id']} stamps={r['stamps']} "
                      f"active_candidates={r['active_candidates']}")

        patch_stamps = await conn.fetch(REPORT_PATCH_STAMPS)
        if patch_stamps:
            print("\nREPORT ONLY - context_patches carrying archived project ids:")
            for r in patch_stamps:
                print(f"  {r['name']!r} dead={r['dead_id']} patches={r['patches']} "
                      f"(active {r['active_patches']})")

        # Human-adjudicated folds: stamps under an archived DEAD-named
        # project remap to the unique ACTIVE-named project. Still guarded:
        # an ambiguous target refuses the whole override.
        override_rows = []
        for spec in args.treat_as:
            dead_name, _, active_name = spec.partition("=")
            if not dead_name or not active_name:
                sys.exit(f"--treat-as must be DEAD=ACTIVE, got {spec!r}")
            targets = await conn.fetch(
                "SELECT project_id FROM projects WHERE name = $1 "
                "AND COALESCE(status, 'active') = 'active'",
                active_name,
            )
            if len(targets) != 1:
                sys.exit(
                    f"--treat-as {spec!r}: {len(targets)} active projects named "
                    f"{active_name!r}; need exactly one"
                )
            target_id = targets[0]["project_id"]
            stamps = await conn.fetch(
                """
                SELECT pa.user_id, pa.entity_id, pa.origin_id,
                       pa.project_id AS dead_id
                FROM person_appearances pa
                JOIN projects dead ON dead.project_id = pa.project_id
                WHERE dead.name = $1
                  AND COALESCE(dead.status, 'active') = 'archived'
                """,
                dead_name,
            )
            print(f"\nOVERRIDE {dead_name!r} -> {active_name!r} ({target_id}): "
                  f"{len(stamps)} stamps")
            override_rows.extend(
                dict(r, canonical_id=target_id) for r in stamps
            )

        if not args.apply:
            print("\nDRY RUN - nothing written. Re-run with --apply.")
            return

        remapped = 0
        async with conn.transaction():
            for r in list(rows) + override_rows:
                tag = await conn.execute(
                    REMAP_ONE,
                    r["user_id"], r["entity_id"], r["origin_id"],
                    r["canonical_id"], r["dead_id"],
                )
                if tag.endswith(" 1"):
                    remapped += 1
        planned = len(rows) + len(override_rows)
        print(f"\nAPPLIED: {remapped} stamps remapped "
              f"(of {planned} planned; a shortfall means concurrent change, investigate).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
