#!/usr/bin/env python3
"""Re-stamp the recorded sha256 of an ALREADY-APPLIED init-db migration.

`run_migrations.py` treats migrations as immutable: if a file's on-disk sha256
no longer matches the value recorded in `schema_migrations`, the next run (and
therefore the next deploy) ABORTS with a drift error. That guard is correct — it
stops a changed migration from silently re-running against a live schema.

But occasionally a file must be corrected in a way that is BEHAVIOR-PRESERVING on
an already-applied database — i.e. the edit changes nothing about the schema that
prod already has, only how a fresh apply behaves. The motivating case is
`04_connected_quilt.sql`: its connection_vocabulary seed used
`ON CONFLICT (label, app_id)`, which Postgres can't infer from the expression
unique index `(label, COALESCE(app_id, ...))`, so a from-scratch apply aborted.
The corrected target seeds the identical rows; on prod those rows already exist,
so the edit is a no-op there — only the recorded sha needs to catch up so the
deploy's drift guard doesn't fire.

This script updates exactly one schema_migrations row to the current on-disk
sha256 of its file. It is dry-run by default and only the named file is touched.

PRECONDITION (operator's responsibility): only run this when the file edit is
behavior-preserving on the already-applied prod schema. If the edit actually
changes schema behavior, add a NEW migration instead — do NOT re-stamp.

Usage:
    # inspect (no write):
    DATABASE_URL=postgres://... python scripts/restamp_migration_sha.py
    # apply to prod (operator, after review):
    DATABASE_URL=postgres://... python scripts/restamp_migration_sha.py --apply
    # a different file:
    DATABASE_URL=postgres://... python scripts/restamp_migration_sha.py \
        --filename 04_connected_quilt.sql --apply
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg

REPO_ROOT = Path(__file__).resolve().parents[1]


async def restamp(database_url: str, filename: str, apply: bool) -> int:
    path = REPO_ROOT / "init-db" / filename
    if not path.is_file():
        print(f"ERROR: {path} does not exist.", file=sys.stderr)
        return 1
    # Match run_migrations.discover_migrations exactly: sha256 of the utf-8 text.
    new_sha = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()

    conn = await asyncpg.connect(database_url)
    try:
        recorded = await conn.fetchval(
            "SELECT sha256 FROM schema_migrations WHERE filename = $1", filename
        )
        if recorded is None:
            print(
                f"ERROR: {filename} is not recorded in schema_migrations. This tool "
                "re-stamps an already-applied file; it will not insert a new row.",
                file=sys.stderr,
            )
            return 1

        if recorded == new_sha:
            print(f"{filename}: already up-to-date (sha256={new_sha[:12]}…). No change.")
            return 0

        print(f"{filename}:")
        print(f"  recorded sha256 : {recorded}")
        print(f"  on-disk  sha256 : {new_sha}")
        if not apply:
            print("\nDry run — pass --apply to update the recorded sha (one row).")
            return 0

        result = await conn.execute(
            "UPDATE schema_migrations SET sha256 = $1 WHERE filename = $2",
            new_sha,
            filename,
        )
        print(f"\nUPDATE {result.split()[-1]} row — recorded sha re-stamped.")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--filename",
        default="04_connected_quilt.sql",
        help="init-db migration filename to re-stamp (default: 04_connected_quilt.sql)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the change. Without it, only prints the diff (dry run).",
    )
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        return 1
    return asyncio.run(restamp(database_url, args.filename, args.apply))


if __name__ == "__main__":
    sys.exit(main())
