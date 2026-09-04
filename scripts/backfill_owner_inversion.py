#!/usr/bin/env python3
"""Archive behavior rows whose owner is the OBJECT of their own sentence.

"Asked Vijay to have Arnav join the Tuesday enablement call" stamped
`owner="Vijay"` says Vijay asked himself. The actor is whoever was
speaking, which the row does not record, so the observation is real and
its attribution is not. It renders on Vijay's page as his conduct, and
doc 16 section 5.13 says a served name may assert only what was observed.

The ingest rule that stops new ones lives in
`extraction_schema.sanitize_behavior_observations`. THIS SCRIPT IMPORTS
THAT MODULE'S PREDICATE rather than restating it, which is the standing
rule for backfills here: a second copy of a rule drifts from the first,
silently, and the drift shows up as history and live ingest disagreeing
about the same sentence.

ARCHIVED, never deleted. Archived status flows through the quilt delta's
`deleted` array so clients converge on sync; a hard delete is a tombstone
nobody receives, which this codebase learned on 2026-07-15.

Dry run by default. `--apply` writes.

    DATABASE_URL=... python scripts/backfill_owner_inversion.py
    DATABASE_URL=... python scripts/backfill_owner_inversion.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, "/app/src")

from contextquilt.services.extraction_schema import (  # noqa: E402
    BEHAVIOR_OBSERVATION_TYPES,
    owner_named_as_object,
)

ARCHIVE_CAUSE = "owner_inverted"


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
        types = list(BEHAVIOR_OBSERVATION_TYPES)
        rows = await conn.fetch(
            """
            SELECT patch_id, patch_type, value->>'text' AS text,
                   value->>'owner' AS owner, created_at::date AS created,
                   origin_id
            FROM context_patches
            WHERE patch_type = ANY($1::text[])
              AND status = 'active'
              AND value->>'owner' IS NOT NULL
            ORDER BY created_at
            """,
            types,
        )
        print(f"active rows of types {types}: {len(rows)}")

        hits = []
        for r in rows:
            obj = owner_named_as_object(r["text"], r["owner"])
            if obj is not None:
                hits.append((r, obj))

        if not hits:
            # Names itself. "None found" and "the predicate is broken"
            # must not share a silence, and this script's whole job is
            # the predicate.
            print("No inverted rows found. The predicate ran against "
                  f"{len(rows)} candidates and matched none.")
            return 0

        print(f"\nINVERTED: {len(hits)} of {len(rows)} "
              f"({100.0 * len(hits) / len(rows):.1f}%)\n")
        for r, obj in hits:
            print(f"  {r['created']}  owner={r['owner']!r}  matched {obj!r}")
            print(f"     {r['text'][:150]}")

        by_owner: dict[str, int] = {}
        for r, _ in hits:
            by_owner[r["owner"]] = by_owner.get(r["owner"], 0) + 1
        print("\nper owner, because this is a claim about a named human:")
        for owner, n in sorted(by_owner.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>3}  {owner}")

        if not args.apply:
            print(f"\nDRY RUN. --apply archives these {len(hits)} rows with "
                  f"archive_cause={ARCHIVE_CAUSE!r}.")
            return 0

        ids = [r["patch_id"] for r, _ in hits]
        result = await conn.execute(
            """
            UPDATE context_patches
               SET status = 'archived',
                   updated_at = NOW(),
                   value = jsonb_set(value, '{archive_cause}', $2::jsonb)
             WHERE patch_id = ANY($1::uuid[])
            """,
            ids, f'"{ARCHIVE_CAUSE}"',
        )
        print(f"\narchived: {result}")
        print("Rows are archived, not deleted, so the delta sync's `deleted` "
              "array carries them to every client.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
