import asyncio
import json
import os
import sys
from datetime import datetime

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from contextquilt.types import ContextPatch, PatchCategory, PatchPersistence, PatchSource, MemoryType
from worker import classify_fact

async def verify_patch_flow():
    print("Verifying Context Patch Flow...")
    
    # 1. Verify Patch Categories and Legacy Mapping
    print("\n1. Verifying Categories & Legacy Mapping:")
    
    patches = [
        ("identity_test", PatchCategory.IDENTITY, MemoryType.FACTUAL),
        ("preference_test", PatchCategory.PREFERENCE, MemoryType.FACTUAL),
        ("trait_test", PatchCategory.TRAIT, MemoryType.FACTUAL),
        ("experience_test", PatchCategory.EXPERIENCE, MemoryType.EPISODIC),
    ]
    
    for key, cat, expected_legacy in patches:
        patch = ContextPatch(
            key=key,
            value="test",
            patch_type=cat
        )
        print(f"  - {cat.value}: Maps to {patch.memory_type.value}")
        assert patch.memory_type == expected_legacy, f"Expected {expected_legacy}, got {patch.memory_type}"

    # 2. Verify Fact Classification Logic
    print("\n2. Verifying Fact Classification:")
    
    test_cases = [
        ("My name is Scott", "identity"),
        ("I am a Python developer", "identity"),
        ("I prefer dark mode", "preference"),
        ("I hate waiting", "preference"),
        ("I am working on the dashboard", "experience"),
        ("Remember when we discussed the API?", "experience"),
        ("Please be concise", "trait"),
        ("Use technical jargon", "trait"),
        ("I know Rust", "identity"), # Skill -> Identity
    ]
    
    for text, expected_cat in test_cases:
        classification = classify_fact(text)
        print(f"  - '{text}' -> {classification}")
        assert classification == expected_cat, f"Expected {expected_cat} for '{text}', got {classification}"

    print("\nVerification Successful!")

if __name__ == "__main__":
    asyncio.run(verify_patch_flow())
