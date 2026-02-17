import asyncio
import aiohttp
import asyncpg
import json
import uuid
import os
import sys
from datetime import datetime
import random

# Configuration
API_URL = "http://localhost:8000"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

# Synthetic Data Generators
NAMES = ["Alice", "Bob", "Charlie", "Diana", "Evan", "Fiona"]
COLORS = ["Red", "Blue", "Green", "Dark Mode", "High Contrast"]
TRAITS = ["Pragmatic", "Analytical", "Creative", "Concise", "Verbose"]
JOBS = ["Python Developer", "Data Scientist", "DevOps Engineer", "Product Manager"]
PROJECTS = ["Project Apollo", "Project Gemini", "Mercury Rewrite", "Legacy Migration"]

async def register_app(session):
    """Register the verifier app"""
    print(f"[*] Registering 'SynthesisVerifier' app...")
    async with session.post(f"{API_URL}/v1/auth/register", json={"app_name": "SynthesisVerifier"}) as resp:
        if resp.status != 200:
            print(f"[!] Failed to register app: {await resp.text()}")
            sys.exit(1)
        data = await resp.json()
        print(f"    -> App ID: {data['app_id']}")
        return data['app_id'], data['client_secret']

async def get_token(session, app_id, client_secret):
    """Get OAuth2 token"""
    data = {"username": app_id, "password": client_secret, "grant_type": "password"}
    async with session.post(f"{API_URL}/v1/auth/token", data=data) as resp:
        if resp.status != 200:
            print(f"[!] Failed to get token: {await resp.text()}")
            sys.exit(1)
        token_data = await resp.json()
        return token_data['access_token']

async def synthesize_data(user_count=1):
    """Main synthesis and verification flow"""
    print(f"\n[*] Starting Data Synthesis for {user_count} users...")
    
    users = []
    
    async with aiohttp.ClientSession() as session:
        # 1. Setup App & Auth
        app_id, secret = await register_app(session)
        token = await get_token(session, app_id, secret)
        headers = {"Authorization": f"Bearer {token}", "X-App-ID": app_id}
        
        # 2. Generate and Send Data
        for i in range(user_count):
            user_id = str(uuid.uuid4())
            name = NAMES[i % len(NAMES)]
            color = COLORS[i % len(COLORS)]
            job = JOBS[i % len(JOBS)]
            project = PROJECTS[i % len(PROJECTS)]
            
            users.append({
                "user_id": user_id, 
                "name": name, 
                "color": color,
                "job": job,
                "project": project
            })
            
            print(f"\n[+] Processing User: {name} ({user_id})")
            
            # A. Active Learning (Tool Call) - Explicit Identity
            payload_identity = {
                "user_id": user_id,
                "interaction_type": "tool_call",
                "fact": f"My name is {name}",
                "category": "identity",
                "source": "explicit",
                "persistence": "permanent"
            }
            async with session.post(f"{API_URL}/v1/memory", json=payload_identity, headers=headers) as resp:
                print(f"    -> Sent Identity Patch: {resp.status}")

            # B. Active Learning (Tool Call) - Explicit Preference
            payload_pref = {
                "user_id": user_id,
                "interaction_type": "tool_call",
                "fact": f"I prefer {color} theme",
                "category": "preference",
                "source": "explicit",
                "persistence": "sticky"
            }
            async with session.post(f"{API_URL}/v1/memory", json=payload_pref, headers=headers) as resp:
                print(f"    -> Sent Preference Patch: {resp.status}")
                
            # C. Passive Learning (Chat Log) - Implicit Experience/Trait
            # We construct a conversation that implies the job and project
            messages = [
                {"role": "user", "content": f"I need help debugging a race condition in the {project} codebase."},
                {"role": "assistant", "content": "I can help with that. What language are you using?"},
                {"role": "user", "content": f"We are using Python. As a {job}, I usually prefer sync code, but this module is async."},
                {"role": "assistant", "content": "Understood."}
            ]
            payload_chat = {
                "user_id": user_id,
                "interaction_type": "chat_log",
                "messages": messages,
                "timestamp": datetime.utcnow().isoformat()
            }
            async with session.post(f"{API_URL}/v1/memory", json=payload_chat, headers=headers) as resp:
                 print(f"    -> Sent Chat Log (Passive): {resp.status}")

    # 3. Verification (Poll DB)
    print(f"\n[*] Verifying Data in Storage (Polling Postgres)...")
    await verify_storage(users, app_id)

async def verify_storage(users, app_id):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        max_retries = 60 # wait up to 5 minutes
        for i in range(max_retries):
            print(f"    ... Polling Attempt {i+1}/{max_retries}")
            all_verified = True
            
            for user in users:
                user_id = user['user_id']
                
                # Check for Patches (Active + Passive)
                # We expect at least:
                # 1. Identity (Name) - Active
                # 2. Preference (Color) - Active
                # 3. Inferred (Job/Project) - Passive (might take longer)
                
                rows = await conn.fetch(
                    "SELECT * FROM context_patches WHERE subject_key = $1", 
                    f"user:{user_id}"
                )
                
                if len(rows) < 2: # At least the 2 active ones should be there quickly
                    all_verified = False
                    break
                
                # Analyze specific patches
                has_identity = False
                has_preference = False
                has_inferred = False
                acl_verified = True
                
                for row in rows:
                    patch_id = row['patch_id']
                    val = json.loads(row['value'])
                    text = val.get('text', '').lower()
                    
                    # Check Types
                    if row['origin_mode'] == 'declared':
                        if 'name is' in text: has_identity = True
                        if 'prefer' in text: has_preference = True
                    elif row['origin_mode'] == 'inferred':
                        has_inferred = True # Chat log processed!
                        
                    # Check ACL
                    acl_row = await conn.fetchrow(
                        "SELECT * FROM context_patch_acl WHERE patch_id = $1 AND app_id = $2",
                        patch_id, app_id
                    )
                    if not acl_row or not acl_row['can_read']:
                        print(f"    [!] ACL Missing for patch {patch_id} (User: {user_id})")
                        acl_verified = False

                if not (has_identity and has_preference):
                    all_verified = False
                    # Only print if we are still waiting for active (should be fast)
                    if i % 5 == 0: print(f"    [-] Waiting for active patches for {user['name']}...")
                elif not has_inferred:
                    # Don't fail immediately on passive, but note it
                    print(f"    [~] Active patches OK for {user['name']}, waiting for passive (Ollama)...")
                    all_verified = False # Wait for full complete
                elif not acl_verified:
                    all_verified = False
                
            if all_verified:
                print("\n[SUCCESS] All users and patches verified!")
                print(f"   - Verified Active Learning (Explicit)")
                print(f"   - Verified Passive Learning (Inferred from Chat)")
                print(f"   - Verified ACL Associations for App {app_id}")
                return

            await asyncio.sleep(5)
            
        print("\n[WARNING] Verification timed out. Passive learning might be slow or failed.")
        print("Check worker logs for Ollama status.")
        
    finally:
        await conn.close()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(synthesize_data())
