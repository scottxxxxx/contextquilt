import asyncio
import os
import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

migration_sql = """
-- 
-- NEW CONTEXT PATCH SCHEMA (v2)
-- 

CREATE TABLE IF NOT EXISTS context_patches (
    patch_id UUID PRIMARY KEY,                   -- Global unique ID
    subject_key TEXT NOT NULL,                   -- "user:123", "org:acme", etc.
    patch_name TEXT NOT NULL,                    -- "food_allergies", "home_city"
    
    -- Semantics & Content
    patch_type TEXT NOT NULL,                    -- "preference", "identity", "trait", "experience"
    value JSONB NOT NULL,                        -- The actual data content
    
    -- Metadata / Search Attributes
    origin_mode TEXT DEFAULT 'inferred',         -- "declared", "inferred", "derived"
    source_prompt TEXT DEFAULT 'none',           -- "detective", "tailor", etc.
    confidence FLOAT DEFAULT 1.0, 
    
    -- Governance
    sensitivity TEXT DEFAULT 'normal',           -- "normal", "pii", "phi", "secret"
    value_type_hint TEXT,                        -- "string", "number", "boolean", "object", "array"
    
    -- Sync & Linear
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    source_patch_ids TEXT[]                      -- Lineage tracking (array of UUID strings)
);

-- Indexes for fast lookup by subject and name
CREATE INDEX IF NOT EXISTS idx_patches_subject ON context_patches(subject_key);
CREATE INDEX IF NOT EXISTS idx_patches_name ON context_patches(patch_name);
CREATE INDEX IF NOT EXISTS idx_patches_subject_name ON context_patches(subject_key, patch_name);


CREATE TABLE IF NOT EXISTS context_patch_acl (
    patch_id UUID NOT NULL REFERENCES context_patches(patch_id) ON DELETE CASCADE,
    app_id UUID NOT NULL REFERENCES applications(app_id) ON DELETE CASCADE,
    
    can_read BOOLEAN DEFAULT TRUE,
    can_write BOOLEAN DEFAULT FALSE,
    can_delete BOOLEAN DEFAULT FALSE,
    
    PRIMARY KEY (patch_id, app_id)
);

CREATE INDEX IF NOT EXISTS idx_acl_patch_id ON context_patch_acl(patch_id);
CREATE INDEX IF NOT EXISTS idx_acl_app_id ON context_patch_acl(app_id);
"""

async def migrate():
    print(f"Connecting to {DATABASE_URL}...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        print("Connected.")
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    print("Applying migration...")
    try:
        await conn.execute(migration_sql)
        print("Migration applied successfully.")
    except Exception as e:
        print(f"Migration failed: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(migrate())
