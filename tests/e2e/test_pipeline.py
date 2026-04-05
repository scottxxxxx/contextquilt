#!/usr/bin/env python3
"""
End-to-end integration test: simulates the full SS → GP → CQ pipeline.

Sends a transcript → waits for extraction → verifies recall returns context.
Runs against the live production CQ instance by default.

Usage:
    # Against production
    CQ_BASE_URL=https://cq.shouldersurf.com \
    CQ_APP_ID=<app-uuid> \
    CQ_JWT=<jwt-token> \
    python tests/e2e/test_pipeline.py

    # Quick run with admin key (bypasses app auth)
    CQ_BASE_URL=https://cq.shouldersurf.com \
    CQ_ADMIN_KEY=<admin-key> \
    python tests/e2e/test_pipeline.py

    # Against local
    CQ_BASE_URL=http://localhost:8000 python tests/e2e/test_pipeline.py
"""

import asyncio
import httpx
import json
import os
import sys
import time
import uuid

# ============================================
# Configuration
# ============================================

CQ_BASE_URL = os.getenv("CQ_BASE_URL", "https://cq.shouldersurf.com")
CQ_JWT = os.getenv("CQ_JWT", "")
CQ_APP_ID = os.getenv("CQ_APP_ID", "")
CQ_ADMIN_KEY = os.getenv("CQ_ADMIN_KEY", "")

# Test user — unique per run to avoid collisions
TEST_USER_ID = f"e2e-test-{uuid.uuid4().hex[:8]}"
TEST_PROJECT_ID = str(uuid.uuid4())
TEST_MEETING_ID = str(uuid.uuid4())

# Test transcript with (you) marker for proper attribution
TEST_TRANSCRIPT = f"""[TestUser (you)] I've been working on the DataSync API. The main challenge is handling rate limits from the upstream provider.

[Alex] What's the current rate limit?

[TestUser (you)] 100 requests per minute. I'll implement exponential backoff with jitter. Should have it done by next Wednesday.

[Alex] Sounds good. I'll handle the retry queue on the consumer side.

[TestUser (you)] Perfect. One thing — I prefer using Redis for the queue over RabbitMQ. Simpler to operate.

[Alex] Agreed. We should also add monitoring. I'll set up the Grafana dashboard.

[TestUser (you)] I tend to over-engineer the error handling, so keep me honest. Let's keep it simple.

The deadline for the MVP is April 15."""

# Expected extractions (for validation)
EXPECTED = {
    "has_project": True,  # "DataSync API" should be extracted as a project
    "has_commitment": True,  # "implement exponential backoff" with owner TestUser
    "has_preference": True,  # "prefers Redis over RabbitMQ"
    "has_trait": True,  # "tends to over-engineer error handling"
    "has_entity_alex": False,  # Alex may or may not appear in patches (entity-only is OK)
    "min_patches": 4,
}


# ============================================
# Helpers
# ============================================

def get_headers():
    """Build auth headers based on available credentials."""
    headers = {"Content-Type": "application/json"}
    if CQ_JWT:
        headers["Authorization"] = f"Bearer {CQ_JWT}"
    elif CQ_APP_ID:
        headers["X-App-ID"] = CQ_APP_ID
    elif CQ_ADMIN_KEY:
        headers["X-Admin-Key"] = CQ_ADMIN_KEY
    else:
        # Try without auth (local dev)
        pass
    return headers


def print_step(step, msg):
    """Print a formatted step message."""
    print(f"\n{'='*60}")
    print(f"  STEP {step}: {msg}")
    print(f"{'='*60}")


def print_result(label, passed, detail=""):
    """Print a test result."""
    icon = "✓" if passed else "✗"
    print(f"  {icon} {label}{f' — {detail}' if detail else ''}")


# ============================================
# Test Steps
# ============================================

async def step_1_health_check(client):
    """Verify CQ is reachable."""
    print_step(1, "Health Check")

    r = await client.get(f"{CQ_BASE_URL}/health")
    passed = r.status_code == 200
    print_result("Health endpoint", passed, f"HTTP {r.status_code}")

    if not passed:
        print(f"  FATAL: CQ not reachable at {CQ_BASE_URL}")
        sys.exit(1)

    return passed


async def step_2_send_transcript(client, headers):
    """Send a test transcript for extraction."""
    print_step(2, "Send Transcript")

    payload = {
        "user_id": TEST_USER_ID,
        "interaction_type": "meeting_transcript",
        "content": TEST_TRANSCRIPT,
        "metadata": {
            "project_id": TEST_PROJECT_ID,
            "project": "DataSync API",
            "meeting_id": TEST_MEETING_ID,
            "display_name": "TestUser",
        },
    }

    r = await client.post(f"{CQ_BASE_URL}/v1/memory", headers=headers, json=payload)
    passed = r.status_code == 200 and r.json().get("status") == "queued"
    print_result("Transcript queued", passed, r.json().get("status", r.text[:100]))

    return passed


async def step_3_wait_for_extraction(client, headers, max_wait=30, poll_interval=3):
    """Poll the quilt until patches appear (worker processes async)."""
    print_step(3, f"Wait for Extraction (max {max_wait}s)")

    start = time.time()
    patches = []

    while time.time() - start < max_wait:
        r = await client.get(f"{CQ_BASE_URL}/v1/quilt/{TEST_USER_ID}", headers=headers)
        if r.status_code == 200:
            data = r.json()
            patches = data.get("facts", [])
            if patches:
                elapsed = time.time() - start
                print_result("Patches extracted", True, f"{len(patches)} patches in {elapsed:.1f}s")
                return patches

        await asyncio.sleep(poll_interval)
        print(f"  ... polling ({time.time() - start:.0f}s)")

    print_result("Patches extracted", False, f"No patches after {max_wait}s")
    return []


async def step_4_validate_extraction(patches):
    """Check that the extracted patches match expectations."""
    print_step(4, "Validate Extraction")

    results = {}
    patch_types = [p.get("patch_type", p.get("category", "")) for p in patches]
    patch_texts = " ".join(
        p.get("fact", str(p.get("value", ""))) for p in patches
    ).lower()

    # Check patch count
    count = len(patches)
    results["min_patches"] = count >= EXPECTED["min_patches"]
    print_result(f"Patch count >= {EXPECTED['min_patches']}", results["min_patches"], f"got {count}")

    # Check for project patch
    results["has_project"] = "project" in patch_types
    print_result("Has project patch", results["has_project"],
                 next((p["fact"] for p in patches if p.get("patch_type") == "project"), "none"))

    # Check for commitment
    results["has_commitment"] = "commitment" in patch_types
    print_result("Has commitment", results["has_commitment"],
                 next((p["fact"] for p in patches if p.get("patch_type") == "commitment"), "none"))

    # Check for preference
    results["has_preference"] = "preference" in patch_types or "redis" in patch_texts
    print_result("Has preference (Redis)", results["has_preference"])

    # Check for trait
    results["has_trait"] = "trait" in patch_types or "over-engineer" in patch_texts
    print_result("Has trait", results["has_trait"])

    # Check for Alex (in patches or as a participant/owner)
    all_text = patch_texts + " " + " ".join(
        str(p.get("owner", "")) + " " + " ".join(p.get("participants", []))
        for p in patches
    ).lower()
    results["has_entity_alex"] = "alex" in all_text
    print_result("Has Alex reference", results["has_entity_alex"],
                 "found in patches" if results["has_entity_alex"] else "may be entity-only (OK)")

    # Check project_id and meeting_id on patches
    has_project_id = any(p.get("project_id") == TEST_PROJECT_ID for p in patches)
    has_meeting_id = any(p.get("meeting_id") == TEST_MEETING_ID for p in patches)
    print_result("project_id on patches", has_project_id, TEST_PROJECT_ID[:8] + "...")
    print_result("meeting_id on patches", has_meeting_id, TEST_MEETING_ID[:8] + "...")

    return results


async def step_5_test_recall(client, headers):
    """Test recall with a query that should match extracted entities."""
    print_step(5, "Test Recall")

    payload = {
        "user_id": TEST_USER_ID,
        "text": "What's the status of the DataSync API and what did Alex commit to?",
        "metadata": {
            "project_id": TEST_PROJECT_ID,
            "project": "DataSync API",
        },
    }

    r = await client.post(f"{CQ_BASE_URL}/v1/recall", headers=headers, json=payload)
    passed = r.status_code == 200
    data = r.json() if passed else {}

    context = data.get("context", "")
    entities = data.get("matched_entities", [])
    patch_count = data.get("patch_count", 0)
    timing = data.get("timing_ms", {})

    print_result("Recall returned context", bool(context), f"{len(context)} chars")
    print_result("Matched entities", bool(entities), ", ".join(entities) if entities else "none")
    print_result("Patch count", patch_count > 0, str(patch_count))

    if timing:
        print(f"\n  Timing breakdown:")
        for k, v in timing.items():
            print(f"    {k}: {v}ms")

    # Check context contains expected info
    context_lower = context.lower()
    has_commitment = "backoff" in context_lower or "wednesday" in context_lower
    has_preference = "redis" in context_lower
    has_trait = "over-engineer" in context_lower

    print_result("Context includes commitment", has_commitment)
    print_result("Context includes preference", has_preference)
    print_result("Context includes trait", has_trait)

    return {
        "has_context": bool(context),
        "has_entities": bool(entities),
        "has_commitment": has_commitment,
        "has_preference": has_preference,
        "has_trait": has_trait,
    }


async def step_6_test_prewarm(client, headers):
    """Test cache prewarm endpoint."""
    print_step(6, "Test Prewarm")

    r = await client.post(f"{CQ_BASE_URL}/v1/prewarm?user_id={TEST_USER_ID}", headers=headers)
    passed = r.status_code == 200
    data = r.json() if passed else {}

    print_result("Prewarm", passed, json.dumps(data))
    return passed


async def step_7_test_communication_profile(client, headers):
    """Check if communication profile was extracted."""
    print_step(7, "Communication Profile")

    r = await client.get(
        f"{CQ_BASE_URL}/v1/profile/{TEST_USER_ID}?keys=communication_profile",
        headers=headers,
    )
    passed = r.status_code == 200
    data = r.json() if passed else {}

    profile = data.get("variables", {}).get("communication_profile")
    if profile:
        print_result("Profile extracted", True, f"sample_count={profile.get('_sample_count', 0)}")
        for dim in ("verbosity", "directness", "formality", "technical_level"):
            val = profile.get(dim)
            if val is not None:
                bar = "█" * int(val * 10) + "░" * (10 - int(val * 10))
                print(f"    {dim:20s} {bar} {val}")
    else:
        print_result("Profile extracted", False, "no profile yet (may need (you) marker)")

    return profile is not None


async def step_8_cleanup(client, headers):
    """Delete all test user data."""
    print_step(8, "Cleanup")

    r = await client.delete(f"{CQ_BASE_URL}/v1/quilt/{TEST_USER_ID}", headers=headers)
    passed = r.status_code == 200
    data = r.json() if passed else {}

    print_result("Test data deleted", passed,
                 f"{data.get('patches_deleted', 0)} patches, {data.get('entities_deleted', 0)} entities")

    return passed


# ============================================
# Main
# ============================================

async def main():
    print(f"\n{'='*60}")
    print(f"  CONTEXTQUILT END-TO-END PIPELINE TEST")
    print(f"{'='*60}")
    print(f"  Target:   {CQ_BASE_URL}")
    print(f"  User:     {TEST_USER_ID}")
    print(f"  Project:  DataSync API ({TEST_PROJECT_ID[:8]}...)")
    print(f"  Meeting:  {TEST_MEETING_ID[:8]}...")
    print(f"  Auth:     {'JWT' if CQ_JWT else 'App-ID' if CQ_APP_ID else 'Admin-Key' if CQ_ADMIN_KEY else 'None'}")

    headers = get_headers()
    all_passed = True

    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: Health check
        await step_1_health_check(client)

        # Step 2: Send transcript
        if not await step_2_send_transcript(client, headers):
            print("\nFATAL: Failed to queue transcript. Check auth.")
            sys.exit(1)

        # Step 3: Wait for extraction
        patches = await step_3_wait_for_extraction(client, headers)
        if not patches:
            print("\nFATAL: No patches extracted. Check worker logs.")
            sys.exit(1)

        # Step 4: Validate extraction
        extraction_results = await step_4_validate_extraction(patches)
        all_passed = all_passed and all(extraction_results.values())

        # Step 5: Test recall
        recall_results = await step_5_test_recall(client, headers)
        all_passed = all_passed and recall_results["has_context"]

        # Step 6: Test prewarm
        await step_6_test_prewarm(client, headers)

        # Step 7: Communication profile
        await step_7_test_communication_profile(client, headers)

        # Step 8: Cleanup
        await step_8_cleanup(client, headers)

    # Summary
    print(f"\n{'='*60}")
    print(f"  RESULT: {'ALL TESTS PASSED ✓' if all_passed else 'SOME TESTS FAILED ✗'}")
    print(f"{'='*60}\n")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
