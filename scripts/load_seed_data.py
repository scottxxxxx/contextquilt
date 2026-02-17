import asyncio
import asyncpg
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")
SEED_FILE = "data/seed_patches.json"
USER_ID = "test_user_timeline"
APP_NAME = "Seed Data Loader"

async def get_or_create_app(conn):
    # Try to find existing app
    row = await conn.fetchrow("SELECT app_id FROM applications WHERE app_name = $1", APP_NAME)
    if row:
        return str(row["app_id"])
    
    # Create new app
    print(f"Creating application '{APP_NAME}'...")
    app_id = str(uuid.uuid4())
    # Assuming applications table structure: app_id, app_name, ...
    # Based on common patterns in this project (auth flow)
    # We might need to check if 'client_id' etc are needed.
    # For safety, I'll try a minimal insert and catch error if fails.
    try:
        await conn.execute(
            "INSERT INTO applications (app_id, app_name) VALUES ($1, $2)",
            app_id, APP_NAME
        )
        return app_id
    except asyncpg.UndefinedTableError:
        print("Warning: 'applications' table not found. Skipping ACL creation requiring app_id foreign key.")
        return None
    except Exception as e:
        # Fallback: maybe it needs client_secret etc.
        # "applications" schema typically matches what's in `src/contextquilt/access.py` or similar
        print(f"Warning: Could not create app: {e}. Trying to fetch ANY app.")
        row = await conn.fetchrow("SELECT app_id FROM applications LIMIT 1")
        if row:
            return str(row["app_id"])
        return None

async def main():
    print(f"Connecting to {DATABASE_URL}...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    
    # Target Users (Demo Personas)
    DEMO_USERS = [
        "alice_wonder", 
        "bob_builder", 
        "charlie_chef", 
        "diana_doctor", 
        "evan_engineer",
        "travel_sarah_final"
    ]

    # 1. Ensure Users Exist
    print(f"Ensuring {len(DEMO_USERS)} demo users exist...")
    for uid in DEMO_USERS:
        await conn.execute(
            "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", 
            uid
        )
    
    # 2. Get App ID for ACL
    app_id = await get_or_create_app(conn)
    if not app_id:
        print("Note: Proceeding without App ID for ACLs (might restrict visibility in dashboard).")
    else:
        print(f"Using App ID: {app_id}")

    # 3. Load Patches
    if not os.path.exists(SEED_FILE):
        print(f"Error: {SEED_FILE} not found. Run generate_seed_patches.py first.")
        return

    with open(SEED_FILE, "r") as f:
        patches = json.load(f)
        
    print(f"Loading {len(patches)} seed patches across {len(DEMO_USERS)} users...")
    
    now = datetime.now(timezone.utc)
    count = 0
    
    # Shuffle patches to ensure random distribution
    import random
    random.shuffle(patches)
    
    # Distribute patches
    for i, p in enumerate(patches):
        # Round robin assignment to ensuring even distribution
        user_id = DEMO_USERS[i % len(DEMO_USERS)]
        
        patch_id = str(uuid.uuid4())
        subject_key = f"user:{user_id}"
        
        # Random date distrubtion: uniform over last 30 days
        days_ago = random.uniform(0, 30)
        created_at = now - timedelta(days=days_ago)
        
        try:
            await conn.execute(
                """
                INSERT INTO context_patches (
                    patch_id, subject_key, patch_name, patch_type, value,
                    origin_mode, source_prompt, confidence, sensitivity, persistence, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11)
                """,
                patch_id,
                subject_key,
                p["patch_name"],
                p["patch_type"],
                json.dumps(p["value"]),
                p["origin_mode"],
                p["source_prompt"],
                p["confidence"],
                p["sensitivity"],
                "sticky",
                created_at
            )
            
            if app_id:
                await conn.execute(
                    """
                    INSERT INTO context_patch_acl (patch_id, app_id, can_read, can_write, can_delete)
                    VALUES ($1, $2, TRUE, TRUE, TRUE)
                    """,
                    patch_id,
                    app_id
                )
            
            count += 1
            if count % 50 == 0:
                print(f"Loaded {count} patches...")
                
        except Exception as e:
            print(f"Failed to insert patch: {e}")

    print(f"Successfully loaded {count} patches distributed among {len(DEMO_USERS)} users.")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
