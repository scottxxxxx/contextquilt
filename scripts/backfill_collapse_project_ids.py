"""
Collapse orphan project_ids into a single canonical target.

Background
----------

Upstream sync layers in some client apps can produce multiple
distinct project_id UUIDs for the same logical project — e.g., when
two devices each generate a fresh UUID for the same named project
and a sync convergence flips the canonical UUID, orphaning prior
meetings under the displaced UUID until a launch-time relink heals
them by name. The result CQ-side: patches for one engagement
fragment across several project_ids, breaking project-scoped recall.

This script folds patches under one or more source project_ids into
a single target project_id, optionally re-canonicalizing the
`project` text field. Reusable for any project_id consolidation.

What it does
------------

For every patch under any --source project_id (active or archived):
  - UPDATE context_patches SET project_id=$target,
    project=$rename (if --rename given), updated_at=NOW()
    WHERE patch_id=$pid

For every --source project_id in the projects table:
  - UPDATE projects SET status='archived', updated_at=NOW()
    WHERE project_id=$source

Read-only by default; --apply writes. One transaction per row for
patches (matches existing backfill pattern, makes interrupted runs
idempotent on re-run). Projects-table archiving in a single batch
at the end.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/backfill_collapse_project_ids.py \\
      --target 531968BF-32F1-496A-A931-23676694C5CE \\
      --source 0BFB079D-D067-4C68-AE33-E4377F54B83D \\
      --source 342D8FD7-FCAA-41E1-AA9F-9ED91C3A79D3 \\
      [--source ...] \\
      --rename ABM
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

FIND_PATCHES_SQL = """
SELECT
    cp.patch_id,
    cp.project_id,
    cp.project,
    cp.status,
    cp.patch_type,
    LEFT(cp.value->>'text', 80) AS text_preview
FROM context_patches cp
WHERE cp.project_id = ANY($1::text[])
ORDER BY cp.project_id, cp.created_at
"""

FIND_PROJECT_SQL = """
SELECT project_id, user_id, name, status
FROM projects
WHERE project_id = $1
"""

FIND_PROJECTS_SQL = """
SELECT project_id, user_id, name, status
FROM projects
WHERE project_id = ANY($1::text[])
"""

UPDATE_PATCH_SQL = """
UPDATE context_patches
   SET project_id = $1,
       project = COALESCE($2, project),
       updated_at = NOW()
 WHERE patch_id = $3
"""

ARCHIVE_PROJECTS_SQL = """
UPDATE projects
   SET status = 'archived',
       updated_at = NOW()
 WHERE project_id = ANY($1::text[])
"""


async def run(
    database_url: str,
    target: str,
    sources: list[str],
    rename: str | None,
    apply: bool,
) -> int:
    if target in sources:
        print(f"ERROR: target {target} also listed as source.", file=sys.stderr)
        return 1
    if not sources:
        print("ERROR: at least one --source required.", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(database_url)
    try:
        target_row = await conn.fetchrow(FIND_PROJECT_SQL, target)
        source_rows = await conn.fetch(FIND_PROJECTS_SQL, sources)
        patch_rows = await conn.fetch(FIND_PATCHES_SQL, sources)

        user_ids = set()
        if target_row:
            user_ids.add(target_row["user_id"])
        for r in source_rows:
            user_ids.add(r["user_id"])
        if len(user_ids) > 1:
            print(
                f"ERROR: target + sources span {len(user_ids)} distinct user_ids: "
                f"{sorted(user_ids)!r}. Refusing to migrate across users.",
                file=sys.stderr,
            )
            return 1

        print("=" * 72)
        print("COLLAPSE PROJECT IDS — audit report")
        print("=" * 72)
        print(f"Target:   {target}")
        if target_row:
            print(
                f"          user_id={target_row['user_id']} "
                f"name={target_row['name']!r} status={target_row['status']!r}"
            )
        else:
            print("          WARNING: target not found in projects table")
        print(f"Sources:  {len(sources)} project_id(s)")
        for src in sources:
            row = next((r for r in source_rows if r["project_id"] == src), None)
            if row:
                print(
                    f"  {src}  user_id={row['user_id']} "
                    f"name={row['name']!r} status={row['status']!r}"
                )
            else:
                print(f"  {src}  (not in projects table)")
        if rename:
            print(f"Rename:   project text → {rename!r}")
        else:
            print("Rename:   (preserve existing project text)")
        print(f"Mode:     {'APPLY' if apply else 'DRY-RUN'}")
        print()

        if not patch_rows:
            print("No patches under any source project_id. Nothing to move.")
        else:
            by_source: dict[str, list] = {}
            for r in patch_rows:
                by_source.setdefault(r["project_id"], []).append(r)
            print(f"Patches to move: {len(patch_rows)}")
            for src in sources:
                rs = by_source.get(src, [])
                if not rs:
                    print(f"  {src}: 0 patches")
                    continue
                active = sum(1 for r in rs if r["status"] == "active")
                other = len(rs) - active
                print(f"  {src}: {len(rs)} patches  ({active} active, {other} non-active)")
                for r in rs[:3]:
                    text = r["text_preview"] or "(no text)"
                    print(f"    [{r['status']}] {r['patch_type']}: {text!r}")
                if len(rs) > 3:
                    print(f"    ... and {len(rs) - 3} more")
            print()

        if not apply:
            print("Dry-run only. Re-run with --apply to write the migration.")
            return 0

        moved = 0
        for r in patch_rows:
            async with conn.transaction():
                await conn.execute(
                    UPDATE_PATCH_SQL,
                    target,
                    rename,
                    r["patch_id"],
                )
            moved += 1

        archived_count = 0
        if source_rows:
            result = await conn.execute(ARCHIVE_PROJECTS_SQL, sources)
            archived_count = int(result.split()[-1]) if result else 0

        print(f"Moved   {moved} patch(es) to target.")
        print(f"Archived {archived_count} project record(s) in projects table.")
        print()

        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM context_patches WHERE project_id = ANY($1::text[])",
            sources,
        )
        on_target = await conn.fetchval(
            "SELECT COUNT(*) FROM context_patches WHERE project_id = $1",
            target,
        )
        print(f"Post-migration verify: {remaining} patches still under source IDs (should be 0).")
        print(f"Post-migration verify: {on_target} patches now under target.")
        return 0 if remaining == 0 else 2
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        required=True,
        help="Target project_id to collapse all sources into.",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        required=True,
        help="Source project_id to fold. Repeat for multiple sources.",
    )
    parser.add_argument(
        "--rename",
        default=None,
        help="If given, set project text field to this value on all moved patches.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag the script is read-only.",
    )
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        return 1
    return asyncio.run(run(database_url, args.target, args.source, args.rename, args.apply))


if __name__ == "__main__":
    sys.exit(main())
