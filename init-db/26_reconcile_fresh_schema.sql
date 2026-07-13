-- 26_reconcile_fresh_schema.sql
-- Reconcile init-db with prod's REAL schema for FRESH environments
-- (local `docker-compose up`, CI, the TEST_DATABASE_URL integration suite).
--
-- These objects already exist on prod: they were applied operationally via
-- scripts/migrate_normalization.py and scripts/migrate_usage_metrics.py, which
-- were never folded back into the init-db migration chain. So prod has them but
-- a from-scratch apply of init-db does not. Every statement below is idempotent,
-- which makes this file a pure no-op on prod and the real fix only on a clean DB.
--
-- (The 04_connected_quilt.sql ON CONFLICT drift — the other fresh-apply blocker —
-- is corrected directly in 04 + a prod schema_migrations re-stamp via
-- scripts/restamp_migration_sha.py. With both fixes, run_migrations.py builds a
-- fresh schema end to end with no shim.)

-- (gap b) Subject index: the subject_key -> patch association. Created on prod by
-- scripts/migrate_normalization.py; the core write path inserts into it.
CREATE TABLE IF NOT EXISTS patch_subjects (
    patch_id    UUID NOT NULL REFERENCES context_patches(patch_id) ON DELETE CASCADE,
    subject_key TEXT NOT NULL,
    PRIMARY KEY (patch_id, subject_key)
);
CREATE INDEX IF NOT EXISTS idx_patch_subjects_subject ON patch_subjects(subject_key);

-- (gap b) Usage metrics drive decay + recall freshness. Created on prod by
-- scripts/migrate_usage_metrics.py; the write path inserts into it.
CREATE TABLE IF NOT EXISTS patch_usage_metrics (
    patch_id                UUID PRIMARY KEY REFERENCES context_patches(patch_id) ON DELETE CASCADE,
    access_count            INTEGER DEFAULT 0,
    last_accessed_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_accessed_by_app_id UUID,
    current_decay_score     FLOAT DEFAULT 1.0
);

-- (gap c) Legacy column: the subject moved to patch_subjects. Dropped on prod by
-- scripts/migrate_normalization.py, but still NOT NULL in 01_init.sql. The write
-- path no longer sets it, so on a fresh DB the NOT NULL blocks every patch insert.
ALTER TABLE context_patches DROP COLUMN IF EXISTS subject_key;
