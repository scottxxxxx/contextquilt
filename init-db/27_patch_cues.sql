-- 27: Patch cues — associative retrieval index (roadmap: cue index).
--
-- A cue is a short lowercase topic phrase naming what a patch is ABOUT
-- ("pricing model", "visa paperwork"), emitted by the extraction LLM at
-- write time and sanitized by sanitize_cues. Cues widen recall beyond
-- entity-name matches: request text that mentions a topic but no entity
-- name can still surface the right patches.
--
-- Source of truth is this table (cues are popped from value before the
-- patch row is written). User scoping goes through patch_subjects.

CREATE TABLE IF NOT EXISTS patch_cues (
    patch_id   UUID NOT NULL REFERENCES context_patches(patch_id) ON DELETE CASCADE,
    cue        TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (patch_id, cue)
);

-- Recall's cue-index rehydration reads all cues for a user (join through
-- patch_subjects on patch_id, covered by its PK); the read path also
-- fetches patches by matched cue.
CREATE INDEX IF NOT EXISTS idx_patch_cues_cue ON patch_cues (cue);
