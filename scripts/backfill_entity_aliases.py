"""
Merge alias-form duplicate entities and record their aliases.

Background
----------

Entities were exact-name keyed, so the same person/project accumulated
separate rows per surface form ("Sarah", "Sarah Abrams", "S. Abrams"),
each with its own relationship neighborhood. The forward path now
resolves surface forms through entity_aliases at write time; this
script repairs the rows that fragmented before that shipped.

What it does
------------

Per user and entity_type, runs the live `find_alias_candidate` heuristic
(token subset / initial expansion, UNIQUE candidate only) across the
existing entities. For each proposed pair, in one transaction:

  1. repoints relationships from the duplicate to the canonical entity
     (deleting rows that would collide with an existing canonical edge,
     and any self-edges produced by the merge)
  2. repoints any alias rows already attached to the duplicate
  3. records the duplicate's name as an alias of the canonical
     (source = 'merge_backfill')
  4. folds mention_count / description into the canonical
  5. deletes the duplicate entity

Chains and ambiguity are skipped: an entity may appear in at most one
proposal side per run (re-run to converge). Stale Redis entity indexes
stay correct without invalidation — the merged name resolves through
its alias row.

Read-only by default; `--apply` writes.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/backfill_entity_aliases.py
    DATABASE_URL='postgres://...' python scripts/backfill_entity_aliases.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from contextquilt.services.entity_aliasing import find_alias_candidate  # noqa: E402


async def merge_pair(conn, user_id: str, dup, canonical) -> None:
    """Fold `dup` into `canonical` (same user, same entity_type)."""
    async with conn.transaction():
        # 1a. Repoint outgoing edges unless the canonical already has them
        await conn.execute(
            """
            UPDATE relationships r SET from_entity_id = $1
             WHERE r.from_entity_id = $2 AND r.user_id = $3
               AND NOT EXISTS (
                   SELECT 1 FROM relationships r2
                    WHERE r2.user_id = $3 AND r2.from_entity_id = $1
                      AND r2.to_entity_id = r.to_entity_id
                      AND r2.relationship_type = r.relationship_type
               )
            """,
            canonical["entity_id"], dup["entity_id"], user_id,
        )
        # 1b. Incoming edges
        await conn.execute(
            """
            UPDATE relationships r SET to_entity_id = $1
             WHERE r.to_entity_id = $2 AND r.user_id = $3
               AND NOT EXISTS (
                   SELECT 1 FROM relationships r2
                    WHERE r2.user_id = $3 AND r2.to_entity_id = $1
                      AND r2.from_entity_id = r.from_entity_id
                      AND r2.relationship_type = r.relationship_type
               )
            """,
            canonical["entity_id"], dup["entity_id"], user_id,
        )
        # 1c. Drop unmovable leftovers and self-edges from the merge
        await conn.execute(
            "DELETE FROM relationships WHERE user_id = $1 AND (from_entity_id = $2 OR to_entity_id = $2)",
            user_id, dup["entity_id"],
        )
        await conn.execute(
            "DELETE FROM relationships WHERE user_id = $1 AND from_entity_id = $2 AND to_entity_id = $2",
            user_id, canonical["entity_id"],
        )
        # 2. Repoint alias rows already attached to the duplicate
        await conn.execute(
            "UPDATE entity_aliases SET entity_id = $1 WHERE entity_id = $2",
            canonical["entity_id"], dup["entity_id"],
        )
        # 3. Record the duplicate's name as an alias
        await conn.execute(
            """
            INSERT INTO entity_aliases (user_id, entity_id, alias, source)
            VALUES ($1, $2, $3, 'merge_backfill')
            ON CONFLICT (user_id, LOWER(alias)) DO NOTHING
            """,
            user_id, canonical["entity_id"], dup["name"],
        )
        # 4. Fold counters/description into the canonical
        await conn.execute(
            """
            UPDATE entities SET
                mention_count = mention_count + $1,
                description = COALESCE(NULLIF(description, ''), $2),
                last_seen_at = GREATEST(last_seen_at, $3)
            WHERE entity_id = $4
            """,
            dup["mention_count"] or 0, dup["description"], dup["last_seen_at"],
            canonical["entity_id"],
        )
        # 5. Remove the duplicate
        await conn.execute(
            "DELETE FROM entities WHERE entity_id = $1", dup["entity_id"]
        )


async def main(apply: bool) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(dsn)
    try:
        # Pairs the user explicitly refused to merge (POST
        # /v1/people/{u}/keep-separate). This read FAILS CLOSED on
        # purpose: merging is destructive here (the duplicate row is
        # deleted), and a merge that overturns an explicit "these are
        # different people" is exactly the answer this table exists to
        # protect. If the table cannot be read, the tool stops rather
        # than merging blind.
        try:
            sep_rows = await conn.fetch(
                "SELECT user_id, entity_id_lo, entity_id_hi FROM entity_separations"
            )
        except Exception as e:
            print(
                "Cannot read entity_separations, refusing to merge "
                f"(apply migration 29 first): {e}",
                file=sys.stderr,
            )
            sys.exit(1)
        separated = {
            (r["user_id"], str(r["entity_id_lo"]), str(r["entity_id_hi"]))
            for r in sep_rows
        }

        def is_separated(user_id, a, b) -> bool:
            lo, hi = sorted((str(a), str(b)))
            return (user_id, lo, hi) in separated

        groups = await conn.fetch(
            "SELECT DISTINCT user_id, entity_type FROM entities "
            "WHERE merged_into IS NULL ORDER BY user_id, entity_type"
        )
        proposals = []
        for g in groups:
            rows = await conn.fetch(
                """
                SELECT entity_id, name, description, mention_count, last_seen_at
                  FROM entities
                 WHERE user_id = $1 AND entity_type = $2
                   AND merged_into IS NULL
                """,
                g["user_id"], g["entity_type"],
            )
            by_id = {r["entity_id"]: r for r in rows}
            for row in rows:
                others = [(r["entity_id"], r["name"]) for r in rows if r["entity_id"] != row["entity_id"]]
                match = find_alias_candidate(row["name"], others)
                if not match:
                    continue
                entity_id, _, direction = match
                if direction == "name_is_alias":
                    # row is the short form → fold row into the matched entity
                    if is_separated(g["user_id"], row["entity_id"], entity_id):
                        print(
                            f"  SKIP (kept separate by user) {row['name']!r} -> "
                            f"{by_id[entity_id]['name']!r} [{g['user_id']} {g['entity_type']}]"
                        )
                        continue
                    proposals.append((g["user_id"], g["entity_type"], row, by_id[entity_id]))
                # name_is_canonical pairs surface again from the other
                # side of the scan as name_is_alias — skip to avoid dupes.

        # Conservative de-chaining: each entity may appear on at most one
        # side of at most one merge this run. Re-run to converge.
        seen_ids: set = set()
        final = []
        for user_id, etype, dup, canonical in proposals:
            if dup["entity_id"] in seen_ids or canonical["entity_id"] in seen_ids:
                print(f"  SKIP (chained) {dup['name']!r} -> {canonical['name']!r} [{user_id} {etype}]")
                continue
            seen_ids.add(dup["entity_id"])
            seen_ids.add(canonical["entity_id"])
            final.append((user_id, etype, dup, canonical))

        for user_id, etype, dup, canonical in final:
            print(f"  MERGE [{etype}] {dup['name']!r} -> {canonical['name']!r} (user {user_id})")
            if apply:
                await merge_pair(conn, user_id, dup, canonical)

        mode = "APPLIED" if apply else "DRY RUN (use --apply to write)"
        print(f"\n{mode}: {len(final)} merges ({len(proposals) - len(final)} skipped as chained)")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
