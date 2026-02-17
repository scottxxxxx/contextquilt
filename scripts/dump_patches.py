import asyncio
import asyncpg
import os
import json

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

async def dump_patches():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT * FROM context_patches ORDER BY created_at DESC LIMIT 20")
        print(f"Found {len(rows)} patches.")
        for row in rows:
            val = json.loads(row['value'])
            print(f"[{row['patch_type']}] {row['subject_key']} -> {val.get('text')} (Origin: {row['origin_mode']}, App ACLs: checking...)")
            
            # Check ACLs
            acls = await conn.fetch("SELECT * FROM context_patch_acl WHERE patch_id = $1", row['patch_id'])
            for acl in acls:
                print(f"   - ACL: App {acl['app_id']} (Read: {acl['can_read']}, Write: {acl['can_write']})")
            print("-" * 40)
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(dump_patches())
