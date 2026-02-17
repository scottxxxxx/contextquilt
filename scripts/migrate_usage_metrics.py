import asyncio
import asyncpg
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

async def migrate_usage_metrics():
    print(f"Connecting to {DATABASE_URL}...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    print("Starting Usage Metrics Migration...")

    # 1. Create Table
    print("Creating patch_usage_metrics table...")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS patch_usage_metrics (
            patch_id UUID PRIMARY KEY REFERENCES context_patches(patch_id) ON DELETE CASCADE,
            
            -- Usage Stats for Decay
            access_count INTEGER DEFAULT 0,
            last_accessed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            
            -- Optional: Weighting and Tracking
            last_accessed_by_app_id UUID,
            
            -- Calculated Decay Score (0.0 - 1.0)
            current_decay_score FLOAT DEFAULT 1.0
        );
    """)

    # 2. Backfill existing patches
    print("Backfilling metrics for existing patches...")
    # Initialize implementation: Set access_count to 1 (creation counts as access)
    # Set last_accessed_at to created_at from parent table
    await conn.execute("""
        INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
        SELECT patch_id, 1, created_at, 1.0 
        FROM context_patches
        ON CONFLICT (patch_id) DO NOTHING
    """)
    print("Backfill complete.")

    await conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    asyncio.run(migrate_usage_metrics())
