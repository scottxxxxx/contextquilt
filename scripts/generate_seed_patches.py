import json
import itertools

# Definitions
TYPES = ["identity", "preference", "trait", "experience"]
ORIGIN_MODES = ["declared", "inferred", "derived"]
SOURCE_PROMPTS = ["onboarding", "user_chat", "archivist", "system_migration"]
SENSITIVITIES = ["normal", "pii", "phi", "secret"]

# Base Values to make things semi-realistic
VALUES = {
    "identity": [
        "Name is John Doe", "Role is Software Architect", "Employed by TechCorp", "Location is Austin, TX"
    ],
    "preference": [
        "Prefers Dark Mode", "Likes concise answers", "Dislikes Java", "Wants git commands included"
    ],
    "trait": [
        "Highly technical", "Visual learner", "Skeptical of AI", "Detail-oriented"
    ],
    "experience": [
        "Debugged production incident #123", "Attended Q4 Planning", "Migrated database to Postgres", "Deployed v2.0"
    ]
}

patches = []
counter = 0

for ptype, origin, prompt, sens in itertools.product(TYPES, ORIGIN_MODES, SOURCE_PROMPTS, SENSITIVITIES):
    # Select a value based on type (round robin)
    base_values = VALUES[ptype]
    value_text = base_values[counter % len(base_values)]
    
    # Construct the patch
    patch = {
        "patch_type": ptype,
        "origin_mode": origin,
        "source_prompt": prompt,
        "sensitivity": sens,
        "value": {"text": value_text},
        "patch_name": f"seed_{ptype}_{counter}",
        "confidence": 1.0 if origin == "declared" else 0.75,
        "description": f"Auto-generated patch {counter} covering {ptype}/{origin}/{prompt}/{sens}"
    }
    patches.append(patch)
    counter += 1

# Output to data/seed_patches.json
output_path = "data/seed_patches.json"
with open(output_path, "w") as f:
    json.dump(patches, f, indent=2)

print(f"Generated {len(patches)} seed patches in {output_path}")
