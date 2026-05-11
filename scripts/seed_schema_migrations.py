"""
One-shot bootstrap for the schema_migrations tracker on an existing
ContextQuilt database.

Background: before run_migrations.py existed, the deploy pipeline applied
every init-db/*.sql on every deploy via psql in a bash loop. That loop
is being retired in favor of a tracked runner. On a fresh DB the new
runner just applies everything from scratch — but on the existing prod
DB, every migration has already been applied through the old loop, so
the new runner needs a populated schema_migrations table or it will try
to re-apply 01_init.sql against the drifted schema and break the deploy.

This script:
    1. Creates the schema_migrations table if missing.
    2. INSERTs a row for every init-db/*.sql file with the current
       on-disk sha256, marking them as already-applied (now()).
    3. Skips files already recorded — safe to re-run.

It is a one-time tool. Once prod is seeded the deploy pipeline calls
run_migrations.py exclusively; this script should not need to be invoked
again.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/seed_schema_migrations.py
    DATABASE_URL='...' python scripts/seed_schema_migrations.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename     TEXT PRIMARY KEY,
    sha256       TEXT NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


async def run(database_url: str, migrations_dir: Path, dry_run: bool) -> int:
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        print(f"No *.sql files found in {migrations_dir}", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(SCHEMA_MIGRATIONS_DDL)
        existing = {
            r["filename"]
            for r in await conn.fetch("SELECT filename FROM schema_migrations")
        }

        to_seed: list[tuple[str, str]] = []
        for f in files:
            if f.name in existing:
                continue
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            to_seed.append((f.name, sha))

        if not to_seed:
            print(
                f"schema_migrations already covers all {len(files)} file(s); "
                "nothing to seed."
            )
            return 0

        print(f"Will mark {len(to_seed)} file(s) as already-applied:")
        for name, sha in to_seed:
            print(f"  + {name}  sha256={sha[:12]}…")

        if dry_run:
            print("Dry run — no changes written.")
            return 0

        async with conn.transaction():
            await conn.executemany(
                "INSERT INTO schema_migrations (filename, sha256) VALUES ($1, $2) "
                "ON CONFLICT (filename) DO NOTHING",
                to_seed,
            )
        print(f"Seeded {len(to_seed)} row(s) into schema_migrations.")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migrations-dir",
        default="init-db",
        help="Directory containing *.sql migration files (default: init-db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be seeded without writing.",
    )
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        return 1

    migrations_dir = Path(args.migrations_dir).resolve()
    if not migrations_dir.is_dir():
        print(f"ERROR: {migrations_dir} is not a directory.", file=sys.stderr)
        return 1

    return asyncio.run(run(database_url, migrations_dir, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
