-- Entity aliases: alternate surface forms for a canonical entity.
--
-- The same real-world person/project appears under multiple names across
-- meetings ("Sarah", "Sarah Abrams", "S. Abrams"; "ABM", "ABM Industries").
-- Exact-name keying made each form its own entities row with its own
-- relationship neighborhood, fragmenting the graph — recall matching
-- "Sarah" missed everything attached to "Sarah Abrams". Same failure
-- class as the ABM project_id split (PR #110), one layer down.
--
-- An alias row maps a surface form to its canonical entity. The worker
-- records aliases at write time (conservative heuristic: token subset or
-- initial expansion against same-type entities, only on a unique match),
-- recall matches aliases in the Redis entity index and resolves them to
-- the canonical entity for graph traversal, and
-- scripts/backfill_entity_aliases.py merges pre-existing duplicates.

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    entity_id UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'heuristic',  -- heuristic | merge_backfill | app
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- One resolution per surface form per user (case-insensitive).
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_aliases_user_alias
    ON entity_aliases (user_id, LOWER(alias));

-- Cascade-delete lookups and canonical-side joins.
CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity
    ON entity_aliases (entity_id);
