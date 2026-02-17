import asyncio
import asyncpg
import os
import uuid

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

async def migrate_prompt_versions():
    print(f"Connecting to {DATABASE_URL}...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    print("Starting Prompt Versioning Migration...")

    async with conn.transaction():
        # 1. Create Registry Table (Metadata)
        print("Creating prompt_registry table...")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_registry (
                prompt_key TEXT PRIMARY KEY,
                prompt_name TEXT NOT NULL,
                description TEXT
            );
        """)

        # 2. Create Versions Table (History)
        print("Creating prompt_versions table...")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_versions (
                version_id UUID PRIMARY KEY,
                prompt_key TEXT NOT NULL REFERENCES prompt_registry(prompt_key) ON DELETE CASCADE,
                version_num INTEGER NOT NULL,
                prompt_text TEXT NOT NULL,
                
                is_active BOOLEAN DEFAULT FALSE,
                change_reason TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                created_by TEXT DEFAULT 'system'
            );
            
            CREATE INDEX IF NOT EXISTS idx_prompt_versions_key ON prompt_versions(prompt_key);
            CREATE INDEX IF NOT EXISTS idx_prompt_versions_active ON prompt_versions(prompt_key) WHERE is_active = TRUE;
        """)

        # 3. Migrate Existing Data
        print("Migrating existing prompts...")
        # Check if old table exists
        table_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'system_prompts'
            );
        """)

        if table_exists:
            rows = await conn.fetch("SELECT * FROM system_prompts")
            for row in rows:
                key = row['prompt_key']
                name = row['prompt_name']
                desc = row.get('description', '')
                text = row['prompt_text']

                # Insert into Registry
                await conn.execute("""
                    INSERT INTO prompt_registry (prompt_key, prompt_name, description)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (prompt_key) DO UPDATE 
                    SET prompt_name = EXCLUDED.prompt_name
                """, key, name, desc)

                # Insert Version 1
                v_id = str(uuid.uuid4())
                await conn.execute("""
                    INSERT INTO prompt_versions (version_id, prompt_key, version_num, prompt_text, is_active, change_reason)
                    VALUES ($1, $2, 1, $3, TRUE, 'Initial migration from v1 schema')
                """, v_id, key, text)
                
            print(f"Migrated {len(rows)} prompts.")
            
            # 4. Drop old table
            print("Dropping legacy system_prompts table...")
            await conn.execute("DROP TABLE system_prompts")
        else:
            print("No legacy system_prompts table found. Skipping data migration.")

    await conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    asyncio.run(migrate_prompt_versions())
