import asyncio
import uuid
import random
from datetime import datetime, timedelta
import asyncpg
import json

import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

# Patch Types and Colors (for reference)
# Identity: Blue
# Preference: Green
# Trait: Purple
# Experience: Orange

PATCH_TEMPLATES = {
    "identity": [
        "My email is passive@example.com",
        "I am a software engineer.",
        "My name is Passive Learner.",
        "I work at ContextQuilt Corp."
    ],
    "preference": [
        "I prefer dark mode UI.",
        "I like concise answers.",
        "I prefer Python over Java.",
        "Don't use emojis in responses."
    ],
    "trait": [
        "I am detail-oriented.",
        "I am a visual learner.",
        "I am patient with bugs.",
        "I am curious about AI."
    ],
    "experience": [
        "I debugged a race condition yesterday.",
        "I deployed the new dashboard feature.",
        "I attended the weekly standup meeting.",
        "I read the documentation on asyncpg."
    ]
}

TARGET_USER = "passive_learner_test"

async def synthesize_history():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        print(f"Synthesizing history for {TARGET_USER}...")
        
        # Get start date (30 days ago)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        
        patches_created = 0

        for _ in range(50): # Generate 50 patches
            patch_type = random.choice(list(PATCH_TEMPLATES.keys()))
            text = random.choice(PATCH_TEMPLATES[patch_type])
            
            # Random date between start and end
            random_seconds = random.randint(0, int((end_date - start_date).total_seconds()))
            created_at = start_date + timedelta(seconds=random_seconds)
            
            patch_id = str(uuid.uuid4())
            
            # 1. Insert into context_patches
            await conn.execute("""
                INSERT INTO context_patches (
                    patch_id, patch_name, patch_type, origin_mode, 
                    source_prompt, value, confidence, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)
            """, 
                patch_id, 
                f"synth_{patch_type}_{random.randint(1000,9999)}",
                patch_type,
                "inferred",
                "detective",
                json.dumps({"text": text, "value": text}),
                0.9,
                created_at
            )

            # 2. Insert into patch_subjects
            await conn.execute("""
                INSERT INTO patch_subjects (patch_id, subject_key)
                VALUES ($1, $2)
            """, patch_id, f"user:{TARGET_USER}")
            
            # 3. Insert into patch_usage_metrics
            await conn.execute("""
                INSERT INTO patch_usage_metrics (
                    patch_id, access_count, current_decay_score, last_accessed_at
                ) VALUES ($1, 1, 1.0, $2)
            """, patch_id, created_at)

            patches_created += 1

        print(f"Successfully created {patches_created} synthetic patches for {TARGET_USER} over the last 30 days.")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(synthesize_history())
