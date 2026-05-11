"""Unit + integration tests for scripts/run_migrations.py.

The pure-function tests run anywhere. The integration tests need a
Postgres instance reachable via the TEST_DATABASE_URL env var and are
skipped otherwise — set it locally to `postgres://postgres:postgres@localhost:5432/cq_migration_test`
against a throwaway DB.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

import asyncpg
import pytest

# scripts/ isn't a package — load the module by path so we can import the
# helpers without restructuring the repo.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
_RUNNER_PATH = _SCRIPTS_DIR / "run_migrations.py"
_spec = importlib.util.spec_from_file_location("run_migrations", _RUNNER_PATH)
assert _spec and _spec.loader
run_migrations = importlib.util.module_from_spec(_spec)
sys.modules["run_migrations"] = run_migrations
_spec.loader.exec_module(run_migrations)


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    d = tmp_path / "init-db"
    d.mkdir()
    (d / "01_a.sql").write_text("CREATE TABLE a (id INT PRIMARY KEY);\n")
    (d / "02_b.sql").write_text("CREATE TABLE b (id INT PRIMARY KEY);\n")
    (d / "10_c.sql").write_text("CREATE TABLE c (id INT PRIMARY KEY);\n")
    return d


class TestDiscoverMigrations:
    def test_sorts_lexically(self, migrations_dir: Path):
        out = run_migrations.discover_migrations(migrations_dir)
        assert [m.filename for m in out] == ["01_a.sql", "02_b.sql", "10_c.sql"]

    def test_computes_sha256(self, migrations_dir: Path):
        out = run_migrations.discover_migrations(migrations_dir)
        body = (migrations_dir / "01_a.sql").read_bytes()
        assert out[0].sha256 == hashlib.sha256(body).hexdigest()

    def test_returns_empty_on_empty_dir(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert run_migrations.discover_migrations(empty) == []


# ----- Integration tests -----
# Skip cleanly when TEST_DATABASE_URL is not set. We don't want the unit
# suite to require Postgres.

TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark_integration = pytest.mark.skipif(
    not TEST_DB, reason="TEST_DATABASE_URL not set"
)


async def _reset(conn: asyncpg.Connection) -> None:
    await conn.execute("DROP TABLE IF EXISTS schema_migrations")
    await conn.execute("DROP TABLE IF EXISTS context_patches")
    await conn.execute("DROP TABLE IF EXISTS a")
    await conn.execute("DROP TABLE IF EXISTS b")
    await conn.execute("DROP TABLE IF EXISTS c")


@pytestmark_integration
class TestRunner:
    async def test_fresh_db_applies_everything(self, migrations_dir: Path):
        assert TEST_DB
        conn = await asyncpg.connect(TEST_DB)
        try:
            await _reset(conn)
            rc = await run_migrations.run(TEST_DB, migrations_dir, dry_run=False)
            assert rc == 0
            applied = {
                r["filename"]
                for r in await conn.fetch("SELECT filename FROM schema_migrations")
            }
            assert applied == {"01_a.sql", "02_b.sql", "10_c.sql"}
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'a')"
            )
        finally:
            await _reset(conn)
            await conn.close()

    async def test_second_run_is_noop(self, migrations_dir: Path):
        assert TEST_DB
        conn = await asyncpg.connect(TEST_DB)
        try:
            await _reset(conn)
            await run_migrations.run(TEST_DB, migrations_dir, dry_run=False)
            rc = await run_migrations.run(TEST_DB, migrations_dir, dry_run=False)
            assert rc == 0
            # Still only one row per migration.
            count = await conn.fetchval("SELECT COUNT(*) FROM schema_migrations")
            assert count == 3
        finally:
            await _reset(conn)
            await conn.close()

    async def test_tampered_file_aborts(self, migrations_dir: Path):
        assert TEST_DB
        conn = await asyncpg.connect(TEST_DB)
        try:
            await _reset(conn)
            await run_migrations.run(TEST_DB, migrations_dir, dry_run=False)

            # Edit an applied file. Next run must refuse.
            (migrations_dir / "01_a.sql").write_text(
                "CREATE TABLE a (id INT PRIMARY KEY, x INT);\n"
            )
            rc = await run_migrations.run(TEST_DB, migrations_dir, dry_run=False)
            assert rc == 1
        finally:
            await _reset(conn)
            await conn.close()

    async def test_bootstrap_refuses_unseeded_existing_prod(
        self, migrations_dir: Path
    ):
        """Existing prod (context_patches exists, schema_migrations empty)
        must abort and tell the operator to seed."""
        assert TEST_DB
        conn = await asyncpg.connect(TEST_DB)
        try:
            await _reset(conn)
            # Simulate the existing-prod state.
            await conn.execute(
                "CREATE TABLE context_patches (patch_id UUID PRIMARY KEY)"
            )
            rc = await run_migrations.run(TEST_DB, migrations_dir, dry_run=False)
            assert rc == 1
            # No migrations should have been applied.
            count = await conn.fetchval("SELECT COUNT(*) FROM schema_migrations")
            assert count == 0
        finally:
            await _reset(conn)
            await conn.close()

    async def test_new_file_after_seeded_db_applies_cleanly(
        self, migrations_dir: Path
    ):
        """After bootstrap (everything marked applied), adding a new file
        on disk should cause just that file to be applied."""
        assert TEST_DB
        conn = await asyncpg.connect(TEST_DB)
        try:
            await _reset(conn)
            # First apply everything as if it ran through the runner.
            await run_migrations.run(TEST_DB, migrations_dir, dry_run=False)

            # Add a new migration file.
            (migrations_dir / "11_d.sql").write_text(
                "CREATE TABLE d (id INT PRIMARY KEY);\n"
            )
            rc = await run_migrations.run(TEST_DB, migrations_dir, dry_run=False)
            assert rc == 0

            applied = {
                r["filename"]
                for r in await conn.fetch("SELECT filename FROM schema_migrations")
            }
            assert "11_d.sql" in applied
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'd')"
            )
        finally:
            await _reset(conn)
            await conn.execute("DROP TABLE IF EXISTS d")
            await conn.close()
