"""
Apply init-db/*.sql migrations against the ContextQuilt database.

Replaces the old "rerun-every-file-every-deploy" loop in the deploy
workflow. Tracks applied migrations in a `schema_migrations` table by
filename + sha256, so:

  - Already-applied files are skipped.
  - Editing an applied file is detected as drift and aborts the deploy.
  - New files run inside a transaction; failure rolls back cleanly.

Bootstrap safety: if `schema_migrations` is empty but `context_patches`
already exists (i.e. this is an existing prod instance where migrations
ran via the old loop), the runner refuses to do anything and points the
operator at `scripts/seed_schema_migrations.py`. Without that guard a
fresh runner would re-execute 01_init.sql against drifted schema and
break the deploy — the exact failure mode this script exists to fix.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/run_migrations.py
    DATABASE_URL='...' python scripts/run_migrations.py --migrations-dir init-db
    DATABASE_URL='...' python scripts/run_migrations.py --dry-run

Exit status:
    0  all migrations applied (or up-to-date — no-op)
    1  drift detected, bootstrap required, or migration failed
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import NamedTuple

import asyncpg

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename     TEXT PRIMARY KEY,
    sha256       TEXT NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


class Migration(NamedTuple):
    filename: str
    sha256: str
    sql: str


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    files = sorted(migrations_dir.glob("*.sql"))
    out: list[Migration] = []
    for f in files:
        sql = f.read_text(encoding="utf-8")
        out.append(
            Migration(
                filename=f.name,
                sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                sql=sql,
            )
        )
    return out


async def needs_bootstrap(conn: asyncpg.Connection) -> bool:
    """An existing prod (context_patches exists) with an empty migrations
    table means the old runner has been applying these files inline.
    Refuse to run until the operator backfills via seed_schema_migrations.py."""
    cp_exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'context_patches')"
    )
    if not cp_exists:
        return False
    row_count = await conn.fetchval("SELECT COUNT(*) FROM schema_migrations")
    return row_count == 0


async def run(database_url: str, migrations_dir: Path, dry_run: bool) -> int:
    migrations = discover_migrations(migrations_dir)
    if not migrations:
        print(f"No *.sql files found in {migrations_dir}", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(SCHEMA_MIGRATIONS_DDL)

        if await needs_bootstrap(conn):
            print(
                "ERROR: context_patches exists but schema_migrations is empty.\n"
                "This looks like an existing prod that hasn't been seeded yet.\n"
                "Run `python scripts/seed_schema_migrations.py` once on this DB,\n"
                "then re-run this script.",
                file=sys.stderr,
            )
            return 1

        applied = {
            r["filename"]: r["sha256"]
            for r in await conn.fetch("SELECT filename, sha256 FROM schema_migrations")
        }

        pending: list[Migration] = []
        for m in migrations:
            prior = applied.get(m.filename)
            if prior is None:
                pending.append(m)
                continue
            if prior != m.sha256:
                print(
                    f"ERROR: {m.filename} was applied previously but its contents "
                    f"have changed (recorded sha256={prior[:12]}…, "
                    f"current sha256={m.sha256[:12]}…). Migrations are immutable "
                    "once applied. If you need to change behavior, add a new "
                    "migration file instead.",
                    file=sys.stderr,
                )
                return 1

        if not pending:
            print(f"Schema up-to-date ({len(migrations)} migration(s), 0 pending).")
            return 0

        print(f"{len(pending)} pending migration(s):")
        for m in pending:
            print(f"  → {m.filename}")
        if dry_run:
            print("Dry run — no changes applied.")
            return 0

        for m in pending:
            print(f"Applying {m.filename}…")
            async with conn.transaction():
                await conn.execute(m.sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename, sha256) VALUES ($1, $2)",
                    m.filename,
                    m.sha256,
                )
            print(f"  ✓ {m.filename}")

        print(f"Applied {len(pending)} migration(s).")
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
        help="Print pending migrations without applying them.",
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
