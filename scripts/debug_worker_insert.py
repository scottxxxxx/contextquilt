import asyncio
import asyncpg
import os
import uuid
import json
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

async def debug_insert():
    print(f"Connecting to {DATABASE_URL}")
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        user_id = str(uuid.uuid4())
        print(f"User ID: {user_id}")
        
        # 1. Profile
        await conn.execute(
            "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            user_id
        )
        print("Profile inserted.")
        
        # 2. Patch
        patch_id = str(uuid.uuid4())
        subject_key = f"user:{user_id}"
        patch_name = f"debug_patch_{patch_id[:8]}"
        value_json = json.dumps({"text": "Debug Fact"})
        created_at = datetime.utcnow()
        
        # Use exact query from worker.py
        await conn.execute(
            """
            INSERT INTO context_patches (
                patch_id, subject_key, patch_name, patch_type, value,
                origin_mode, source_prompt, confidence, persistence, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            patch_id, subject_key, patch_name, "identity", value_json,
            "declared", "debug", 1.0, "sticky", created_at, created_at
        )
        print(f"Patch inserted: {patch_id}")
        
        # 3. Verify
        row = await conn.fetchrow("SELECT * FROM context_patches WHERE patch_id = $1", patch_id)
        if row:
            print("VERIFIED: Patch found in DB!")
        else:
            print("FAILURE: Patch NOT found in DB!")
            
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(debug_insert())
