import asyncio
import asyncpg
import os
from collections import defaultdict

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

async def inspect():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        print("Connected to DB.")
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    rows = await conn.fetch("""
        SELECT ps.subject_key, cp.patch_type, cp.origin_mode 
        FROM context_patches cp
        JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
    """)
    
    print(f"Total Patches: {len(rows)}")
    
    counts = defaultdict(int)
    users = set()
    
    for r in rows:
        key = f"{r['subject_key']} | {r['patch_type']} | {r['origin_mode']}"
        counts[key] += 1
        users.add(r['subject_key'])
        
    print("\n--- Distribution ---")
    for k, v in sorted(counts.items()):
        print(f"{k}: {v}")
        
    print("\n--- Users ---")
    for u in sorted(users):
        print(u)
        
    await conn.close()

if __name__ == "__main__":
    asyncio.run(inspect())
