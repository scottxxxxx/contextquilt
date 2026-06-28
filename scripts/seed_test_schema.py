#!/usr/bin/env python3
"""Build ContextQuilt's full schema on a FRESH Postgres (CI, tests, local dev).

`scripts/run_migrations.py` cannot build the schema from a clean database:
init-db has drifted from prod, which was seeded long ago and then evolved via
one-off scripts/migrate_*.py that were never folded back into the chain.

The blocking item is `04_connected_quilt.sql`. Its connection_vocabulary seed
does `INSERT ... ON CONFLICT (label, app_id)`, but uniqueness is an EXPRESSION
unique index on `(label, COALESCE(app_id, '000...'::uuid))`. Postgres can't infer
a bare-column conflict target from an expression index, so a fresh apply aborts
with "no unique or exclusion constraint matching the ON CONFLICT specification"
(this also breaks `docker-compose up` from a clean volume). 04 is already applied
and sha-seeded on prod, so it can't be edited without a prod schema_migrations
re-stamp, which is tracked as a separate operator-run change.

This helper applies a CORRECTED 04 but records each file's REAL on-disk sha256 in
schema_migrations, so a subsequent run_migrations.py against the UNMODIFIED repo
sees every file as already-applied and no-ops (no drift abort). The other two
drift gaps (missing patch_subjects / patch_usage_metrics tables and a still
NOT NULL context_patches.subject_key) are fixed for real by the in-repo migration
26_reconcile_fresh_schema.sql, which this helper applies like any other file.

Once 04 is corrected on prod (+ re-stamp), delete the 04 shim below; at that point
a plain run_migrations.py builds a fresh schema and this helper can go away.

Usage:
    TEST_DATABASE_URL=postgresql://... python scripts/seed_test_schema.py
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg

REPO_ROOT = Path(__file__).resolve().parents[1]
INIT_DB = REPO_ROOT / "init-db"

# 04 shim: make the conflict target match the expression unique index that the
# file itself creates (idx_connection_vocab_unique). Identical effect, but
# Postgres can actually infer it.
_04_FILE = "04_connected_quilt.sql"
_04_BAD = "ON CONFLICT (label, app_id) DO NOTHING;"
_04_GOOD = (
    "ON CONFLICT (label, COALESCE(app_id, "
    "'00000000-0000-0000-0000-000000000000'::uuid)) DO NOTHING;"
)


async def main() -> int:
    db = os.getenv("TEST_DATABASE_URL")
    if not db:
        print("TEST_DATABASE_URL is required", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(db)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ DEFAULT now())"
        )
        for path in sorted(INIT_DB.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            # REAL on-disk sha (matches run_migrations.discover_migrations), so a
            # later run_migrations.py recognises the file as already-applied even
            # though we applied the shimmed body.
            sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if path.name == _04_FILE:
                if _04_BAD not in sql:
                    print(
                        f"WARNING: {_04_FILE} no longer contains the expected "
                        "ON CONFLICT shim target. If 04 has been corrected, drop "
                        "this helper's shim (and likely the whole helper).",
                        file=sys.stderr,
                    )
                sql = sql.replace(_04_BAD, _04_GOOD)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename, sha256) VALUES ($1, $2) "
                    "ON CONFLICT (filename) DO UPDATE SET sha256 = EXCLUDED.sha256",
                    path.name,
                    sha,
                )
        n = await conn.fetchval("SELECT count(*) FROM schema_migrations")
        print(f"seed_test_schema: schema built, {n} migration(s) recorded")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
