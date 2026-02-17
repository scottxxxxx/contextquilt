import asyncio
import os
import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

fix_sql = """
ALTER TABLE context_patches ADD COLUMN IF NOT EXISTS persistence TEXT DEFAULT 'sticky';
"""

async def migrate_fix():
    print(f"Connecting to {DATABASE_URL}...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        print("Connected.")
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    print("Applying fix migration...")
    try:
        await conn.execute(fix_sql)
        print("Fix applied successfully.")
    except Exception as e:
        print(f"Fix failed: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(migrate_fix())
