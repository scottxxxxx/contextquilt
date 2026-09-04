#!/usr/bin/env python3
"""Rename the stored patch type `behavior` to `moment`.

Scott ruled the rename on 2026-09-04. The manifest already describes the
type precisely as one observed instance of how a named participant
conducted themselves, which is a single moment, while "behavior" is a
mass noun for a pattern and a clinical one. Every other type names the
item you are holding; this one named the domain.

WHY THIS IS SAFE TO RUN NOW AND WAS NOT BEFORE. `patch_type` is
wire-visible and ShoulderSurf holds a client-side enum, so a rename is a
BREAK at the reader rather than an additive change. They shipped first
(`62451cb`), and their enum accepts BOTH spellings, so old rows and new
rows both render while this runs and afterwards. That is the whole
reason the sequence was theirs, then this.

TWO TABLES, and the second is easy to forget. `context_patches.patch_type`
holds the stored rows. `patch_type_registry.type_key` holds what the
facet runtime resolves through, and a stale row there means the runtime
falls back to the SS floor for a type that no longer exists under that
name. Re-registering the manifest writes the new key; this removes the
old one so the two cannot both answer.

ARCHIVED ROWS MIGRATE TOO. They flow through the quilt delta's `deleted`
array, and a client that has already seen them under one type name must
see the same rows under the new one rather than a fresh set.

Dry run by default. `--apply` writes, inside one transaction.

    DATABASE_URL=... python scripts/migrate_behavior_to_moment.py
    DATABASE_URL=... python scripts/migrate_behavior_to_moment.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

OLD = "behavior"
NEW = "moment"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        by_status = await conn.fetch(
            "SELECT status, count(*) AS n FROM context_patches "
            "WHERE patch_type = $1 GROUP BY status ORDER BY n DESC", OLD)
        total = sum(r["n"] for r in by_status)
        already = await conn.fetchval(
            "SELECT count(*) FROM context_patches WHERE patch_type = $1", NEW)
        registry = await conn.fetch(
            "SELECT type_key, app_id, display_name FROM patch_type_registry "
            "WHERE type_key = ANY($1::text[])", [OLD, NEW])

        print(f"context_patches with patch_type={OLD!r}: {total}")
        for r in by_status:
            print(f"  {r['status']:<10} {r['n']}")
        print(f"context_patches already {NEW!r}: {already}")
        print(f"\npatch_type_registry rows for either name: {len(registry)}")
        for r in registry:
            print(f"  type_key={r['type_key']!r} app={str(r['app_id'])[:8]} "
                  f"display={r['display_name']!r}")

        if total == 0 and not any(r["type_key"] == OLD for r in registry):
            # Names itself: "already migrated" and "pointed at the wrong
            # database" must not share a silence.
            any_patches = await conn.fetchval("SELECT count(*) FROM context_patches")
            print(f"\nNothing to migrate. ({any_patches} patches exist overall, so "
                  "this ran against real data and the rename is already done.)")
            return 0

        if not args.apply:
            print(f"\nDRY RUN. --apply rewrites {total} patches and "
                  f"{sum(1 for r in registry if r['type_key'] == OLD)} registry rows, "
                  "in one transaction.")
            return 0

        async with conn.transaction():
            patched = await conn.execute(
                "UPDATE context_patches SET patch_type = $2 WHERE patch_type = $1",
                OLD, NEW)
            # The registry is rewritten by re-registering the manifest, so
            # the OLD key is deleted rather than renamed: renaming would
            # collide with the row registration has already written, and
            # leaving it lets two keys answer for one type.
            dropped = await conn.execute(
                "DELETE FROM patch_type_registry WHERE type_key = $1", OLD)

        print(f"\npatches:  {patched}")
        print(f"registry: {dropped}")
        print("\nACCEPTANCE IS NOT THAT THIS EXITED 0. Confirm zero rows remain "
              f"under {OLD!r}, that the count under {NEW!r} matches {total}, and "
              "that the manifest has been re-registered so the runtime resolves "
              "the new key.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
