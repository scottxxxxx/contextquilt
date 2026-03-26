-- Connected Quilt Model
-- Transforms CQ from flat patches to a connected graph where:
-- - Patches have extensible types (app-defined via registry)
-- - Patches are connected via typed edges (role + label)
-- - Roles drive lifecycle (archive cascades, completion unblocks)
-- - Labels are app vocabulary (belongs_to, blocked_by, motivated_by)

-- ============================================================
-- 1. Patch Type Registry — what kinds of patches can exist
-- ============================================================

CREATE TABLE IF NOT EXISTS patch_type_registry (
    type_key TEXT PRIMARY KEY,                -- "trait", "commitment", "blocker"
    app_id UUID REFERENCES applications(app_id) ON DELETE CASCADE,
                                              -- NULL = built-in (available to all apps)
    display_name TEXT NOT NULL,               -- Human-readable name
    schema JSONB NOT NULL DEFAULT '{}',       -- Expected shape of value JSONB
    persistence TEXT DEFAULT 'sticky',        -- "sticky", "decaying", "completable"
    default_ttl_days INTEGER,                 -- NULL = no expiry
    is_completable BOOLEAN DEFAULT FALSE,     -- Can this patch be marked "done"?
    project_scoped BOOLEAN DEFAULT FALSE,     -- Does this type typically belong to a project?
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Seed built-in types (universal, app_id = NULL)
INSERT INTO patch_type_registry (type_key, app_id, display_name, schema, persistence, is_completable, project_scoped) VALUES
    ('trait',       NULL, 'Trait',       '{"text": "string"}', 'sticky', FALSE, FALSE),
    ('preference',  NULL, 'Preference',  '{"text": "string"}', 'sticky', FALSE, FALSE),
    ('identity',    NULL, 'Identity',    '{"text": "string", "role": "string?", "org": "string?"}', 'sticky', FALSE, FALSE),
    ('experience',  NULL, 'Experience',  '{"text": "string", "participants": "string[]?"}', 'decaying', FALSE, TRUE),
    ('role',        NULL, 'Role',        '{"text": "string"}', 'sticky', FALSE, FALSE),
    ('person',      NULL, 'Person',      '{"text": "string"}', 'sticky', FALSE, FALSE),
    ('project',     NULL, 'Project',     '{"text": "string", "status": "string?"}', 'sticky', FALSE, FALSE),
    ('decision',    NULL, 'Decision',    '{"text": "string", "rationale": "string?"}', 'sticky', FALSE, TRUE),
    ('commitment',  NULL, 'Commitment',  '{"text": "string", "owner": "string?", "deadline": "string?"}', 'completable', TRUE, TRUE),
    ('blocker',     NULL, 'Blocker',     '{"text": "string"}', 'completable', TRUE, TRUE),
    ('takeaway',    NULL, 'Takeaway',    '{"text": "string"}', 'decaying', FALSE, TRUE)
ON CONFLICT (type_key) DO NOTHING;

-- ============================================================
-- 2. Patch Connections — typed edges between patches (the stitching)
-- ============================================================

CREATE TABLE IF NOT EXISTS patch_connections (
    connection_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_patch_id UUID NOT NULL REFERENCES context_patches(patch_id) ON DELETE CASCADE,
    to_patch_id UUID NOT NULL REFERENCES context_patches(patch_id) ON DELETE CASCADE,
    connection_role TEXT NOT NULL,             -- Structural: parent, depends_on, resolves, replaces, informs
    connection_label TEXT,                     -- Semantic (app vocabulary): belongs_to, blocked_by, motivated_by
    context TEXT,                              -- Optional explanation
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(from_patch_id, to_patch_id, connection_role)
);

CREATE INDEX IF NOT EXISTS idx_connections_from ON patch_connections(from_patch_id);
CREATE INDEX IF NOT EXISTS idx_connections_to ON patch_connections(to_patch_id);
CREATE INDEX IF NOT EXISTS idx_connections_role ON patch_connections(connection_role);

-- ============================================================
-- 3. Connection Vocabulary — per-app registered labels
-- ============================================================

CREATE TABLE IF NOT EXISTS connection_vocabulary (
    label TEXT NOT NULL,
    app_id UUID REFERENCES applications(app_id) ON DELETE CASCADE,
                                              -- NULL = universal vocabulary
    role TEXT NOT NULL,                       -- Which structural role this maps to
    from_types TEXT[],                        -- Which patch types can be "from"
    to_types TEXT[],                          -- Which patch types can be "to"
    description TEXT,                         -- What this connection means
    PRIMARY KEY (label, app_id)
);

-- Seed universal vocabulary (app_id = NULL)
INSERT INTO connection_vocabulary (label, app_id, role, from_types, to_types, description) VALUES
    ('belongs_to',   NULL, 'parent',     ARRAY['decision','commitment','blocker','takeaway','role'], ARRAY['project'], 'Child belongs to parent project — archives cascade'),
    ('works_on',     NULL, 'informs',    ARRAY['person'], ARRAY['project'], 'Person is involved in a project'),
    ('owns',         NULL, 'informs',    ARRAY['person'], ARRAY['commitment','blocker','decision'], 'Person is responsible for this'),
    ('blocked_by',   NULL, 'depends_on', ARRAY['commitment'], ARRAY['blocker'], 'Commitment blocked until blocker clears'),
    ('unblocks',     NULL, 'resolves',   ARRAY['blocker'], ARRAY['commitment'], 'Clearing this blocker frees the commitment'),
    ('motivated_by', NULL, 'informs',    ARRAY['decision'], ARRAY['preference','takeaway'], 'Decision driven by preference or insight'),
    ('supersedes',   NULL, 'replaces',   ARRAY['decision'], ARRAY['decision'], 'New decision replaces old one')
ON CONFLICT (label, app_id) DO NOTHING;

-- ============================================================
-- 4. Extend existing tables
-- ============================================================

-- Patch status lifecycle: active → completed → archived
ALTER TABLE context_patches ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active';
ALTER TABLE context_patches ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE;
CREATE INDEX IF NOT EXISTS idx_patches_status ON context_patches(status);

-- Per-app policy configuration (extraction caps, budgets, decay rules)
ALTER TABLE applications ADD COLUMN IF NOT EXISTS policy JSONB DEFAULT '{}'::jsonb;
