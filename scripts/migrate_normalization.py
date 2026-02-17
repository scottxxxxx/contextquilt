import asyncio
import asyncpg
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

async def migrate_normalization():
    print(f"Connecting to {DATABASE_URL}...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    print("Starting Normalization Migration...")

    # 1. Create Association Table
    print("Creating patch_subjects table...")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS patch_subjects (
            patch_id UUID NOT NULL REFERENCES context_patches(patch_id) ON DELETE CASCADE,
            subject_key TEXT NOT NULL,
            PRIMARY KEY (patch_id, subject_key)
        );
        CREATE INDEX IF NOT EXISTS idx_patch_subjects_subject ON patch_subjects(subject_key);
    """)

    # 2. Migrate Existing Data
    print("Migrating existing subject keys...")
    # Check if column exists before trying to migrate
    # If this runs twice, we don't want to error out
    try:
        # Check if subject_key exists in context_patches
        val = await conn.fetchval("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='context_patches' AND column_name='subject_key'
        """)
        
        if val:
            # Copy data
            await conn.execute("""
                INSERT INTO patch_subjects (patch_id, subject_key)
                SELECT patch_id, subject_key FROM context_patches
                ON CONFLICT DO NOTHING
            """)
            print(f"Data copied.")
            
            # 3. Drop Column
            print("Dropping legacy subject_key column...")
            await conn.execute("ALTER TABLE context_patches DROP COLUMN subject_key")
        else:
            print("subject_key column not found (already migrated?)")

    except Exception as e:
        print(f"Error during data migration: {e}")

    await conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    asyncio.run(migrate_normalization())
