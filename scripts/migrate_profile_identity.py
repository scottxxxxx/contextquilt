"""
Migration: Add display_name and email columns to profiles table.

Allows CQ to store human-readable identity info passed by calling apps
(e.g., CloudZap forwarding Apple Sign-In email/name).
"""
import asyncio
import asyncpg
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

async def migrate_profile_identity():
    print(f"Connecting to {DATABASE_URL}...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    print("Starting Profile Identity Migration...")

    # Add display_name column
    print("Adding display_name column...")
    await conn.execute("""
        ALTER TABLE profiles
        ADD COLUMN IF NOT EXISTS display_name TEXT
    """)

    # Add email column
    print("Adding email column...")
    await conn.execute("""
        ALTER TABLE profiles
        ADD COLUMN IF NOT EXISTS email TEXT
    """)

    # Add index on email for lookups
    print("Adding email index...")
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_profiles_email ON profiles (email)
    """)

    print("Migration complete.")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(migrate_profile_identity())
