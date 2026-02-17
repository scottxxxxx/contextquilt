import asyncio
import os
import uuid
import json
import asyncpg
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

async def verify_schema():
    print(f"Connecting to {DATABASE_URL}...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        print("Connected.")
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    # Check tables
    print("Checking tables...")
    tables = await conn.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    table_names = [t['table_name'] for t in tables]
    print(f"Tables found: {table_names}")
    
    if "context_patches" in table_names and "context_patch_acl" in table_names:
        print("SUCCESS: New tables exist.")
    else:
        print("FAILURE: New tables missing.")

    # Insert a patch
    patch_id = str(uuid.uuid4())
    user_id = "test_user_verify"
    subject_key = f"user:{user_id}"
    patch_name = "test_patch_flavor"
    value = json.dumps({"flavor": "vanilla"})
    
    print("\nInserting test patch...")
    try:
        await conn.execute(
            """
            INSERT INTO context_patches (
                patch_id, subject_key, patch_name, patch_type, value,
                origin_mode, source_prompt, confidence, persistence
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            patch_id, subject_key, patch_name, "preference", value,
            "declared", "test_script", 1.0, "sticky"
        )
        print("Patch inserted.")
    except Exception as e:
        print(f"FAILURE: Insert patch failed: {e}")
        await conn.close()
        return

    # Insert ACL
    app_id = str(uuid.uuid4())
    print("\nInserting ACL...")
    try:
        # First creating a dummy app entry because of foreign key constraint
        await conn.execute(
            "INSERT INTO applications (app_id, app_name, client_secret_hash) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            app_id, "Test App", "hash"
        )
        
        await conn.execute(
            """
            INSERT INTO context_patch_acl (patch_id, app_id, can_read, can_write)
            VALUES ($1, $2, TRUE, FALSE)
            """,
            patch_id, app_id
        )
        print("ACL inserted.")
    except Exception as e:
        print(f"FAILURE: Insert ACL failed: {e}")

    # Verify Retrieval
    print("\nVerifying retrieval...")
    row = await conn.fetchrow(
        "SELECT * FROM context_patches WHERE patch_id = $1", patch_id
    )
    if row:
        print("SUCCESS: Patch retrieved.")
        print(dict(row))
    else:
        print("FAILURE: Patch not found.")

    acl_row = await conn.fetchrow(
        "SELECT * FROM context_patch_acl WHERE patch_id = $1 AND app_id = $2", patch_id, app_id
    )
    if acl_row:
        print("SUCCESS: ACL retrieved.")
        print(dict(acl_row))
    else:
        print("FAILURE: ACL not found.")
        
    await conn.close()

if __name__ == "__main__":
    asyncio.run(verify_schema())
