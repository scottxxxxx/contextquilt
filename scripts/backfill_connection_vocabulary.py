"""
Repair stored patch_connections that violate the manifest vocabulary.

Background
----------

The extraction LLM emits edges with vocabulary labels but off-spec
type combos: reversed direction (blocker --blocked_by--> commitment
instead of commitment --blocked_by--> blocker), pairs no orientation
allows (owns commitment->decision), and labels that aren't in the
vocabulary at all (works_with). Nothing validated these at write time
until enforce_connection_vocabulary shipped on the forward path, so
historical rows are polluted. ShoulderSurf's client-side validator
drops them silently, which loses real semantic content (the reversed
ones) in the quilt UI.

What it does
------------

Loads the app's latest registered manifest from app_schemas, then for
every labeled connection between two patches classifies the edge with
the live `classify_connection` rules:

  - valid     → role normalized to the spec role if it drifted
  - reversed  → from/to swapped (and role set to the spec role); if the
                swap collides with an existing row on the unique
                (from, to, role) key, the redundant reversed row is
                deleted instead
  - invalid   → deleted

Read-only by default; `--apply` writes. Reuses the live classifier so
the backfill stays in sync with the forward path.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/backfill_connection_vocabulary.py
    DATABASE_URL='postgres://...' python scripts/backfill_connection_vocabulary.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from contextquilt.services.extraction_schema import (  # noqa: E402
    build_label_specs,
    classify_connection,
)


async def main(apply: bool) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(dsn)
    try:
        manifest_raw = await conn.fetchval(
            "SELECT manifest FROM app_schemas ORDER BY version DESC LIMIT 1"
        )
        if not manifest_raw:
            print("no registered manifest found in app_schemas", file=sys.stderr)
            sys.exit(1)
        manifest = json.loads(manifest_raw) if isinstance(manifest_raw, str) else manifest_raw
        label_specs = build_label_specs(manifest.get("connection_labels"))
        print(f"manifest v{manifest.get('version')}: {len(label_specs)} labels in vocabulary")

        rows = await conn.fetch(
            """
            SELECT pc.connection_id, pc.connection_label, pc.connection_role,
                   pc.from_patch_id, pc.to_patch_id,
                   fp.patch_type AS from_type, tp.patch_type AS to_type
              FROM patch_connections pc
              JOIN context_patches fp ON fp.patch_id = pc.from_patch_id
              JOIN context_patches tp ON tp.patch_id = pc.to_patch_id
             WHERE pc.connection_label IS NOT NULL
             ORDER BY pc.created_at
            """
        )
        print(f"{len(rows)} labeled connections to check")

        ok = role_fixed = flipped = deleted = 0
        for r in rows:
            verdict, spec_role = classify_connection(
                r["connection_label"], r["from_type"], r["to_type"], label_specs
            )
            sig = f"{r['connection_label']}: {r['from_type']} -> {r['to_type']}"
            if verdict == "valid":
                if r["connection_role"] != spec_role:
                    print(f"  ROLE FIX  {sig}  ({r['connection_role']} -> {spec_role})  {r['connection_id']}")
                    if apply:
                        await conn.execute(
                            "UPDATE patch_connections SET connection_role = $1 WHERE connection_id = $2",
                            spec_role, r["connection_id"],
                        )
                    role_fixed += 1
                else:
                    ok += 1
            elif verdict == "reversed":
                print(f"  FLIP      {sig}  {r['connection_id']}")
                if apply:
                    collision = await conn.fetchval(
                        """
                        SELECT 1 FROM patch_connections
                         WHERE from_patch_id = $1 AND to_patch_id = $2 AND connection_role = $3
                        """,
                        r["to_patch_id"], r["from_patch_id"], spec_role,
                    )
                    if collision:
                        await conn.execute(
                            "DELETE FROM patch_connections WHERE connection_id = $1",
                            r["connection_id"],
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE patch_connections
                               SET from_patch_id = $1, to_patch_id = $2, connection_role = $3
                             WHERE connection_id = $4
                            """,
                            r["to_patch_id"], r["from_patch_id"], spec_role, r["connection_id"],
                        )
                flipped += 1
            else:
                print(f"  DELETE    {sig}  {r['connection_id']}")
                if apply:
                    await conn.execute(
                        "DELETE FROM patch_connections WHERE connection_id = $1",
                        r["connection_id"],
                    )
                deleted += 1

        mode = "APPLIED" if apply else "DRY RUN (use --apply to write)"
        print(f"\n{mode}: {ok} ok, {role_fixed} role fixes, {flipped} flips, {deleted} deletions")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
