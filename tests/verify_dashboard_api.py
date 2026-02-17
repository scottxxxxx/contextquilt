
import asyncio
import os
import uuid
import asyncpg
from datetime import datetime
import httpx
from fastapi.testclient import TestClient

# Adjust path to import main app
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.main import app

client = TestClient(app)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

async def setup_test_data():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Create a unique test user and patch
        user_id = f"test_user_{uuid.uuid4().hex[:8]}"
        subject_key = f"user:{user_id}"
        patch_id = str(uuid.uuid4())
        
        await conn.execute("""
            INSERT INTO context_patches (patch_id, subject_key, patch_name, value, patch_type, origin_mode)
            VALUES ($1, $2, 'test_patch', '{"foo": "bar"}', 'preference', 'inferred')
        """, patch_id, subject_key)
        
        return user_id, patch_id
    finally:
        await conn.close()

async def verify_dashboard():
    print("Setting up test data...")
    user_id, patch_id = await setup_test_data()
    print(f"Created test user: {user_id}, patch: {patch_id}")
    
    # 1. Verify Recent Patches
    print("\nVerifying /api/dashboard/patches/recent...")
    response = client.get("/api/dashboard/patches/recent")
    assert response.status_code == 200
    patches = response.json()
    assert len(patches) > 0
    # Find our patch
    found = next((p for p in patches if p['patch_id'] == patch_id), None)
    assert found is not None
    assert found['user_id'] == user_id
    assert found['patch_type'] == 'preference'
    assert found['origin'] == 'inferred'
    print("✓ Recent patches verification passed")

    # 2. Verify Stats
    print("\nVerifying /api/dashboard/stats...")
    response = client.get("/api/dashboard/stats")
    assert response.status_code == 200
    stats = response.json()
    assert stats['total_facts'] > 0 # Should count our patch
    print("✓ Stats verification passed")

    # 3. Verify Filter (Patch Type)
    print("\nVerifying filter by patch_type='preference'...")
    response = client.get("/api/dashboard/patches/recent?patch_type=preference")
    assert response.status_code == 200
    filtered = response.json()
    assert all(p['patch_type'] == 'preference' for p in filtered)
    print("✓ Filter verification passed")

    # 4. Verify Distribution
    print("\nVerifying /api/dashboard/patches/distribution...")
    response = client.get("/api/dashboard/patches/distribution?group_by=patch_type")
    assert response.status_code == 200
    dist = response.json()
    # Check if 'preference' is in distribution
    pref_group = next((g for g in dist if g['label'] == 'preference'), None)
    assert pref_group is not None
    assert pref_group['count'] >= 1
    print("✓ Distribution verification passed")

async def main():
    await verify_dashboard()

if __name__ == "__main__":
    asyncio.run(main())
