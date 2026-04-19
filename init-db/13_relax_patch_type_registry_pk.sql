-- ============================================================
-- Migration 13 — Relax patch_type_registry primary key
-- ============================================================
-- The original PR 1 migration (10) added an `app_id` column to
-- patch_type_registry but left the primary key as (type_key) alone —
-- which makes it impossible to register per-app types that share a
-- domain_type name with the universal seed (e.g., SS's "trait" vs
-- the universal "trait").
--
-- This migration:
--   1. Drops the old (type_key) primary key
--   2. Adds a surrogate UUID primary key (registry_id) so any tooling
--      that expects a PK still has one
--   3. Adds a NULL-safe unique index on (type_key, app_id) that
--      treats NULL app_id as a sentinel — same pattern already used
--      by connection_vocabulary.
--
-- Caught in production when registering the ShoulderSurf manifest
-- hit: `duplicate key value violates unique constraint
-- "patch_type_registry_pkey" — Key (type_key)=(trait) already exists.`
-- ============================================================

DO $$
BEGIN
    -- Drop the old single-column PK if it still exists
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'patch_type_registry_pkey'
          AND conrelid = 'patch_type_registry'::regclass
    ) THEN
        -- Check whether the existing PK is on (type_key) alone
        IF EXISTS (
            SELECT 1 FROM pg_constraint c
            JOIN pg_attribute a
              ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
            WHERE c.conname = 'patch_type_registry_pkey'
              AND a.attname = 'type_key'
              AND array_length(c.conkey, 1) = 1
        ) THEN
            ALTER TABLE patch_type_registry DROP CONSTRAINT patch_type_registry_pkey;
        END IF;
    END IF;

    -- Add surrogate registry_id column
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'patch_type_registry' AND column_name = 'registry_id'
    ) THEN
        ALTER TABLE patch_type_registry
            ADD COLUMN registry_id UUID DEFAULT gen_random_uuid();
        UPDATE patch_type_registry SET registry_id = gen_random_uuid() WHERE registry_id IS NULL;
        ALTER TABLE patch_type_registry ALTER COLUMN registry_id SET NOT NULL;
    END IF;

    -- Attach PK to the surrogate column (no-op if already there)
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'patch_type_registry'::regclass
          AND contype = 'p'
    ) THEN
        ALTER TABLE patch_type_registry ADD PRIMARY KEY (registry_id);
    END IF;
END $$;

-- NULL-safe unique index — (type_key, app_id) with NULL app_id
-- collapsed to a fixed UUID sentinel so PostgreSQL treats universal
-- seed rows as each having a distinct identity.
CREATE UNIQUE INDEX IF NOT EXISTS patch_type_registry_unique
    ON patch_type_registry (
        type_key,
        COALESCE(app_id, '00000000-0000-0000-0000-000000000000'::uuid)
    );
