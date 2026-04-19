-- ============================================================
-- Migration 10 — App Schema Registration
-- ============================================================
-- Adds:
--   1. facet and permanence columns on patch_type_registry
--   2. Hard cleanup of retired patch types (identity, experience,
--      feature, deadline) and their associated patches
--   3. app_schemas table (versioned manifest history per app)
--   4. entity_type_registry (per-app entity type declarations)
--   5. Replaces meeting_id with origin_id + origin_type on
--      context_patches and extraction_metrics
--   6. about_patch_id column on context_patches (Attribute patches
--      can describe non-user Connection patches)
--   7. Relaxed belongs_to vocabulary to allow Connection → Connection
--      hierarchies
--
-- Prerequisite: migrations 01-09 applied.
-- No backward compatibility: SS is pre-launch, data is test-only.
-- This migration deletes retired-type patches and drops the old
-- meeting_id column directly. If existing clients rely on either,
-- they will break — intentional per the v1 taxonomy decision.
-- ============================================================


-- ------------------------------------------------------------
-- 1. Add facet and permanence to patch_type_registry
-- ------------------------------------------------------------

ALTER TABLE patch_type_registry
    ADD COLUMN IF NOT EXISTS facet TEXT,
    ADD COLUMN IF NOT EXISTS permanence TEXT;

-- Backfill facet and permanence for the universal built-in types
-- we're keeping. identity and experience are deleted in section 2.
-- See docs/memos/patch-taxonomy-simplification.md for the v1 6-facet model.
UPDATE patch_type_registry SET facet = 'Attribute',  permanence = 'year'    WHERE type_key = 'trait';
UPDATE patch_type_registry SET facet = 'Affinity',   permanence = 'year'    WHERE type_key = 'preference';
UPDATE patch_type_registry SET facet = 'Connection', permanence = 'decade'  WHERE type_key = 'person';
UPDATE patch_type_registry SET facet = 'Connection', permanence = 'quarter' WHERE type_key = 'project';
UPDATE patch_type_registry SET facet = 'Connection', permanence = 'year'    WHERE type_key = 'role';
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'year'    WHERE type_key = 'decision';
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'month'   WHERE type_key = 'commitment';
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'week'    WHERE type_key = 'blocker';
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'week'    WHERE type_key = 'takeaway';

-- Add constraints (only if not already present)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_patch_type_facet'
    ) THEN
        ALTER TABLE patch_type_registry
            ADD CONSTRAINT chk_patch_type_facet
            CHECK (facet IS NULL OR facet IN (
                'Attribute', 'Affinity', 'Intention',
                'Constraint', 'Connection', 'Episode'
            ));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_patch_type_permanence'
    ) THEN
        ALTER TABLE patch_type_registry
            ADD CONSTRAINT chk_patch_type_permanence
            CHECK (permanence IS NULL OR permanence IN (
                'permanent', 'decade', 'year', 'quarter',
                'month', 'week', 'day'
            ));
    END IF;
END $$;


-- ------------------------------------------------------------
-- 2. Hard cleanup of retired patch types
-- ------------------------------------------------------------
-- Per the v1 taxonomy decision:
--   - identity: cut (redundant with trait + connection)
--   - experience: legacy, replaced by decision/commitment/blocker/takeaway/event
--   - feature, deadline: were never patch types in CQ, but delete any
--     misclassified rows defensively.
--
-- Deletes patches unconditionally. Cascades through FK constraints
-- to patch_subjects, patch_usage_metrics, patch_connections, and
-- context_patch_acl. SS is pre-launch; test data is disposable.

DO $$
DECLARE
    deleted_patches INTEGER;
BEGIN
    WITH deleted AS (
        DELETE FROM context_patches
        WHERE patch_type IN ('identity', 'experience', 'feature', 'deadline')
        RETURNING patch_id
    )
    SELECT COUNT(*) INTO deleted_patches FROM deleted;
    RAISE NOTICE 'Deleted % patches of retired types (identity, experience, feature, deadline).', deleted_patches;
END $$;

-- Remove the registry rows for these types.
DELETE FROM patch_type_registry
    WHERE type_key IN ('identity', 'experience', 'feature', 'deadline')
      AND app_id IS NULL;


-- ------------------------------------------------------------
-- 3. Create app_schemas table (versioned manifest history)
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_schemas (
    schema_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_id          UUID NOT NULL REFERENCES applications(app_id) ON DELETE CASCADE,
    version         INT NOT NULL,
    manifest        JSONB NOT NULL,
    registered_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    registered_by   TEXT,
    UNIQUE(app_id, version)
);

CREATE INDEX IF NOT EXISTS idx_app_schemas_current
    ON app_schemas(app_id, version DESC);


-- ------------------------------------------------------------
-- 4. Create entity_type_registry (parallel to patch_type_registry)
-- ------------------------------------------------------------
-- Per-app declaration of which entity types are valid and should be
-- indexed for name-matching during recall. Apps that don't register
-- entity types fall back to CQ's default universal set.

CREATE TABLE IF NOT EXISTS entity_type_registry (
    entity_type     TEXT NOT NULL,
    app_id          UUID REFERENCES applications(app_id) ON DELETE CASCADE,
                                            -- NULL = built-in (universal fallback)
    display_name    TEXT NOT NULL,
    description     TEXT,
    indexed         BOOLEAN DEFAULT TRUE,   -- Keep a Redis name index
    extraction_rules JSONB DEFAULT '{}',    -- Optional per-app guidance
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Unique per (entity_type, app_id) with NULL-safe index
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_type_registry_unique
    ON entity_type_registry (
        entity_type,
        COALESCE(app_id, '00000000-0000-0000-0000-000000000000'::uuid)
    );

-- Seed universal entity types (matches src/contextquilt/services/extraction_schema.py ENTITY_TYPES)
INSERT INTO entity_type_registry (entity_type, app_id, display_name, description, indexed) VALUES
    ('person',   NULL, 'Person',   'A named individual.', TRUE),
    ('project',  NULL, 'Project',  'A named unit of ongoing work.', TRUE),
    ('company',  NULL, 'Company',  'A named organizational entity.', TRUE),
    ('feature',  NULL, 'Feature',  'A product feature or capability.', TRUE),
    ('artifact', NULL, 'Artifact', 'A concrete deliverable or document.', TRUE),
    ('deadline', NULL, 'Deadline', 'A date mentioned as a deadline.', TRUE),
    ('metric',   NULL, 'Metric',   'A quantitative value or target.', TRUE)
ON CONFLICT DO NOTHING;


-- ------------------------------------------------------------
-- 5. Replace meeting_id with origin_id + origin_type
-- ------------------------------------------------------------
-- SS is pre-launch; existing meeting_id values are test data and
-- are not preserved. Add new columns, drop the old column directly.

ALTER TABLE context_patches
    ADD COLUMN IF NOT EXISTS origin_id TEXT,
    ADD COLUMN IF NOT EXISTS origin_type TEXT;

CREATE INDEX IF NOT EXISTS idx_patches_origin
    ON context_patches(origin_type, origin_id);

DROP INDEX IF EXISTS idx_patches_meeting_id;
ALTER TABLE context_patches DROP COLUMN IF EXISTS meeting_id;

-- Same rename on extraction_metrics (admin dashboard ingestion log).
ALTER TABLE extraction_metrics
    ADD COLUMN IF NOT EXISTS origin_id TEXT,
    ADD COLUMN IF NOT EXISTS origin_type TEXT;

CREATE INDEX IF NOT EXISTS idx_metrics_origin
    ON extraction_metrics(origin_type, origin_id);

DROP INDEX IF EXISTS idx_metrics_meeting;
ALTER TABLE extraction_metrics DROP COLUMN IF EXISTS meeting_id;


-- ------------------------------------------------------------
-- 6. Add about_patch_id for Attribute patches targeting non-user entities
-- ------------------------------------------------------------
-- Nullable. When non-NULL, the patch describes the target Connection
-- patch rather than the submitting user.

ALTER TABLE context_patches
    ADD COLUMN IF NOT EXISTS about_patch_id UUID
    REFERENCES context_patches(patch_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_patches_about ON context_patches(about_patch_id);


-- ------------------------------------------------------------
-- 7. Relax belongs_to vocabulary for Connection → Connection hierarchy
-- ------------------------------------------------------------
-- Allows novel → chapter → scene, dissertation → chapter, and
-- sub-project relationships without requiring a new connection label.

UPDATE connection_vocabulary
    SET from_types = array(
        SELECT DISTINCT unnest(from_types || ARRAY['person', 'org', 'project']::TEXT[])
    )
    WHERE label = 'belongs_to' AND app_id IS NULL;

-- Note: individual apps can further extend belongs_to's from_types when
-- they register their own schemas, e.g., a research app may add
-- ARRAY['citation', 'experiment'] to its belongs_to definition.


-- ------------------------------------------------------------
-- Sanity checks
-- ------------------------------------------------------------

DO $$
DECLARE
    unmapped INTEGER;
BEGIN
    SELECT COUNT(*) INTO unmapped
        FROM patch_type_registry
        WHERE app_id IS NULL AND (facet IS NULL OR permanence IS NULL);
    IF unmapped > 0 THEN
        RAISE WARNING 'Universal patch type registry has % rows missing facet/permanence after backfill', unmapped;
    END IF;
END $$;
