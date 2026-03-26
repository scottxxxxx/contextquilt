-- Add project scope to context_patches
-- This allows filtering patches by project/customer context during recall,
-- preventing cross-project context bleed (e.g., Florida Blue facts leaking
-- into a different customer's meeting context).

ALTER TABLE context_patches ADD COLUMN IF NOT EXISTS project TEXT;

CREATE INDEX IF NOT EXISTS idx_patches_project ON context_patches (project);
