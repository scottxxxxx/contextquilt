-- ============================================================
-- Migration 10 — App Schema Registration
-- ============================================================
-- Adds:
--   1. facet and permanence columns on patch_type_registry
--   2. app_schemas table (versioned manifest history per app)
--   3. origin_id and origin_type columns on context_patches
--      (generalizes meeting_id)
--   4. about_patch_id column on context_patches (Attribute patches
--      can describe non-user Connection patches)
--   5. Relaxed belongs_to vocabulary to allow Connection → Connection
--      hierarchies
--
-- Prerequisite: migrations 01-09 applied.
-- Backward compatibility: meeting_id column is retained during
-- transition. Code that reads from it continues to work.
-- ============================================================


-- ------------------------------------------------------------
-- 1. Add facet and permanence to patch_type_registry
-- ------------------------------------------------------------

ALTER TABLE patch_type_registry
    ADD COLUMN IF NOT EXISTS facet TEXT,
    ADD COLUMN IF NOT EXISTS permanence TEXT;

-- Backfill facet and permanence for the universal built-in types
-- (based on the v1 6-facet model — see docs/memos/patch-taxonomy-simplification.md).
UPDATE patch_type_registry SET facet = 'Attribute',  permanence = 'year'    WHERE type_key = 'trait';
UPDATE patch_type_registry SET facet = 'Affinity',   permanence = 'year'    WHERE type_key = 'preference';
UPDATE patch_type_registry SET facet = 'Attribute',  permanence = 'year'    WHERE type_key = 'identity';
UPDATE patch_type_registry SET facet = 'Connection', permanence = 'decade'  WHERE type_key = 'person';
UPDATE patch_type_registry SET facet = 'Connection', permanence = 'quarter' WHERE type_key = 'project';
UPDATE patch_type_registry SET facet = 'Connection', permanence = 'year'    WHERE type_key = 'role';
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'year'    WHERE type_key = 'decision';
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'month'   WHERE type_key = 'commitment';
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'week'    WHERE type_key = 'blocker';
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'week'    WHERE type_key = 'takeaway';
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'month'   WHERE type_key = 'experience';

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
-- 2. Remove dead universal types (identity, experience)
-- ------------------------------------------------------------
-- Only remove if no active patches reference them.
-- Per the taxonomy validation tests (see docs/memos/patch-taxonomy-simplification.md),
-- these types have zero data in the ShoulderSurf corpus.

DO $$
DECLARE
    identity_patch_count INTEGER;
    experience_patch_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO identity_patch_count
        FROM context_patches WHERE patch_type = 'identity';
    SELECT COUNT(*) INTO experience_patch_count
        FROM context_patches WHERE patch_type = 'experience';

    IF identity_patch_count = 0 THEN
        DELETE FROM patch_type_registry
            WHERE type_key = 'identity' AND app_id IS NULL;
        RAISE NOTICE 'Removed universal identity type from registry (0 patches referenced it).';
    ELSE
        RAISE NOTICE 'Retained identity type — % patches still reference it.', identity_patch_count;
    END IF;

    IF experience_patch_count = 0 THEN
        DELETE FROM patch_type_registry
            WHERE type_key = 'experience' AND app_id IS NULL;
        RAISE NOTICE 'Removed universal experience type from registry (0 patches referenced it).';
    ELSE
        RAISE NOTICE 'Retained experience type — % patches still reference it.', experience_patch_count;
    END IF;
END $$;


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
-- 3b. Create entity_type_registry (parallel to patch_type_registry)
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
-- 4. Generalize meeting_id → origin_id + origin_type
-- ------------------------------------------------------------
-- Adds new columns and backfills from meeting_id. The old meeting_id
-- column is retained during the transition; code should prefer
-- origin_id going forward.

ALTER TABLE context_patches
    ADD COLUMN IF NOT EXISTS origin_id TEXT,
    ADD COLUMN IF NOT EXISTS origin_type TEXT;

-- Backfill: any existing patch with a meeting_id becomes an
-- origin_id with origin_type = 'meeting'.
UPDATE context_patches
    SET origin_id = meeting_id,
        origin_type = 'meeting'
    WHERE meeting_id IS NOT NULL
      AND origin_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_patches_origin
    ON context_patches(origin_type, origin_id);


-- ------------------------------------------------------------
-- 5. Add about_patch_id for Attribute patches targeting non-user entities
-- ------------------------------------------------------------
-- Nullable. When non-NULL, the patch describes the target Connection
-- patch rather than the submitting user.

ALTER TABLE context_patches
    ADD COLUMN IF NOT EXISTS about_patch_id UUID
    REFERENCES context_patches(patch_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_patches_about ON context_patches(about_patch_id);


-- ------------------------------------------------------------
-- 6. Relax belongs_to vocabulary for Connection → Connection hierarchy
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
-- 7. Add origin_types declaration to app_schemas manifest
-- ------------------------------------------------------------
-- No DDL needed — this lives inside the manifest JSONB on app_schemas.
-- Documented here for discoverability.
--
-- Example manifest origin_types array:
--   "origin_types": ["meeting", "imported_recording", "typed_note"]
--
-- The schema registration endpoint validates that any origin_type
-- used on a patch for this app appears in the registered list.


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
