import asyncio
import asyncpg
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

async def test_query():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        start_date = "2025-12-07"
        end_date = "2025-12-07"
        
        print(f"Testing with {start_date} to {end_date}")
        
        query = """
            WITH date_series AS (
                SELECT generate_series(
                    $1::date, 
                    $2::date, 
                    '1 day'::interval
                )::date AS day
            )
            SELECT 
                ds.day, 
                COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'identity') as identity
            FROM date_series ds
            LEFT JOIN context_patches p ON date_trunc('day', p.created_at)::date = ds.day
            GROUP BY ds.day
            ORDER BY ds.day ASC
        """
        rows = await conn.fetch(query, start_date, end_date)
        print("Success!")
        for row in rows:
            print(row)
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(test_query())
