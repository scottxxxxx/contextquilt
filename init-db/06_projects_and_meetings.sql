-- Projects as first-class entities with stable IDs
-- Project names can change; the ID never does.
-- Meetings are grouped under projects.

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,          -- Stable ID from the app (e.g., ShoulderSurf project UUID)
    user_id TEXT NOT NULL,                -- Which user owns this project
    name TEXT NOT NULL,                   -- Display name (renameable)
    status TEXT DEFAULT 'active',         -- active, archived
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

-- Add project_id and meeting_id to context_patches
-- project_id replaces the text `project` column for stable references
ALTER TABLE context_patches ADD COLUMN IF NOT EXISTS project_id TEXT;
ALTER TABLE context_patches ADD COLUMN IF NOT EXISTS meeting_id TEXT;

CREATE INDEX IF NOT EXISTS idx_patches_project_id ON context_patches(project_id);
CREATE INDEX IF NOT EXISTS idx_patches_meeting_id ON context_patches(meeting_id);
