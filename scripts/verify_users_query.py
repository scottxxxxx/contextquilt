
import asyncio
import asyncpg
import os
import json

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/context_quilt"

async def test_users_query():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        query = """
            SELECT 
                p.user_id,
                COUNT(cp.patch_id) as patch_count
            FROM profiles p
            LEFT JOIN context_patches cp ON cp.subject_key = 'user:' || p.user_id
            GROUP BY p.user_id
            ORDER BY patch_count DESC
            LIMIT 5
        """
        rows = await conn.fetch(query)
        print("Query successful. Top 5 users by patch count:")
        for row in rows:
            print(f"User: {row['user_id']}, Count: {row['patch_count']}")
            
    except Exception as e:
        print(f"Query failed: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(test_users_query())
