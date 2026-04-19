-- ============================================================
-- Migration 12 — Per-Patch Permanence Override
-- ============================================================
-- Adds a nullable permanence_override column on context_patches.
-- When NULL, the patch uses its type's default permanence (from
-- patch_type_registry). When non-NULL, the patch uses the override.
--
-- Supports:
--   - User-driven pinning ("keep forever")
--   - App-driven promotion (detect load-bearing patches, raise their
--     permanence automatically)
--   - User shortening ("let this fade sooner than normal")
--
-- Principle: permanence is a default, not a rule. Types declare how
-- long patches of this kind usually matter; individual patches can
-- deviate up or down based on their actual importance.
--
-- See docs/design/app-schema-registration.md "Per-patch permanence
-- override" section for design rationale and decay behavior.
-- ============================================================


-- 1. Add the override column, nullable
ALTER TABLE context_patches
    ADD COLUMN IF NOT EXISTS permanence_override TEXT;

-- 2. Audit column: who set the override
ALTER TABLE context_patches
    ADD COLUMN IF NOT EXISTS permanence_override_source TEXT;
    -- Values: 'user' | 'app' | NULL when override is NULL

-- 3. Constrain to valid permanence classes (allow NULL for the default case)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_permanence_override'
    ) THEN
        ALTER TABLE context_patches
            ADD CONSTRAINT chk_permanence_override
            CHECK (permanence_override IS NULL OR permanence_override IN (
                'permanent', 'decade', 'year', 'quarter',
                'month', 'week', 'day'
            ));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_permanence_override_source'
    ) THEN
        ALTER TABLE context_patches
            ADD CONSTRAINT chk_permanence_override_source
            CHECK (permanence_override_source IS NULL OR permanence_override_source IN (
                'user', 'app'
            ));
    END IF;
END $$;

-- 4. Index for the decay worker (only indexes rows that have an override)
CREATE INDEX IF NOT EXISTS idx_patches_permanence_override
    ON context_patches(permanence_override)
    WHERE permanence_override IS NOT NULL;
