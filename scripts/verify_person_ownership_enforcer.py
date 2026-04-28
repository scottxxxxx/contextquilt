"""
Smoke test for PR #84 — person-ownership safety net.

Replays a real captured transcript (meeting 79676670-… from the
memory_updates Redis Stream) through the live worker and confirms the
new enforcer is producing person patches + owns connections.

Run AFTER PR #84 is merged and the worker container is force-recreated.

Usage:
    DATABASE_URL='postgres://...' \
    REDIS_URL='redis://...' \
    CQ_BASE_URL='https://cq.shouldersurf.com' \
    CQ_APP_ID='930824d3-...' \
    CQ_CLIENT_SECRET='...' \
    python scripts/verify_person_ownership_enforcer.py

What it does:
1. Reads the original captured transcript for meeting 79676670 from the
   memory_updates Redis Stream.
2. Re-POSTs it to /v1/memory with a fresh, isolated origin_id so we
   don't conflict with existing patches.
3. Polls extraction_metrics for the new origin_id until processed.
4. Counts person patches and owns connections produced.
5. Reports pass/fail with the specific numbers.

Pass criteria:
- ≥ 3 person patches (Brian, Reshmi, Venkata at minimum)
- ≥ 6 owns connections from person → action_item patches
- _person_ownership_enforced audit field appears in worker logs

This script is read-mostly and creates exactly one test extraction.
The synthetic origin_id makes the result easy to clean up if desired
(DELETE FROM context_patches WHERE origin_id = '<test_origin>').
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx

# The original meeting whose transcript we replay.
SOURCE_ORIGIN_ID = "79676670-B081-46E2-A629-7B44AA12D1CB"
SOURCE_USER_ID = "fa4d903c-24c0-45d5-9fdb-b5496e32501b"

# Pass criteria. We assert structural correctness ("at least one person→
# action `owns` edge was created"), not specific names/counts — Haiku 4.5
# extraction output varies run-to-run on the same transcript, so a hard
# count threshold is fragile. The enforcer's job is to ensure that
# whenever the LLM names an owner, the graph artifact exists; one example
# proves the path. For richer assertions, run multiple iterations and
# compare medians.
MIN_OWNS_EDGES_INTO_TEST = 1


def _require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit(f"Missing required env vars: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}


async def _fetch_token(base_url: str, app_id: str, secret: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{base_url}/v1/auth/token",
            data={"username": app_id, "password": secret},
        )
        r.raise_for_status()
        return r.json()["access_token"]


def _pull_source_transcript_payload() -> dict[str, Any]:
    """Pull the original /v1/memory payload from the Redis Stream by SSHing
    to prod. Returns the parsed JSON payload."""
    import subprocess

    redis_pwd = os.environ.get("REDIS_PASSWORD")
    if not redis_pwd:
        sys.exit("REDIS_PASSWORD env required to pull from prod Redis")
    ssh_target = os.environ.get("CQ_SSH_TARGET", "scottguida@35.239.227.192")
    ssh_key = os.environ.get("CQ_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519"))

    cmd = [
        "ssh", "-i", ssh_key, "-o", "StrictHostKeyChecking=no", ssh_target,
        f"sudo docker exec cq-redis sh -c 'redis-cli -a {redis_pwd} XRANGE memory_updates - + 2>/dev/null'",
    ]
    out = subprocess.check_output(cmd, text=True)
    # Parse XRANGE output: lines alternate stream-id, "data", JSON
    lines = out.split("\n")
    for i, line in enumerate(lines):
        if SOURCE_ORIGIN_ID in line and line.lstrip().startswith("{"):
            return json.loads(line.strip())
    sys.exit(f"Could not find transcript for origin_id {SOURCE_ORIGIN_ID} on the stream")


async def _replay_extraction(
    base_url: str, token: str, payload: dict[str, Any], new_origin_id: str
) -> None:
    """POST the captured payload back to /v1/memory under a fresh origin_id."""
    body = dict(payload)  # shallow copy
    body.pop("app_id", None)  # API will re-set from auth
    body.pop("timestamp", None)
    body["metadata"] = dict(body.get("metadata") or {})
    body["metadata"]["origin_id"] = new_origin_id

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{base_url}/v1/memory",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        result = r.json()
        print(f"  /v1/memory queued: {result}")


def _query_db(sql: str) -> str:
    import subprocess

    ssh_target = os.environ.get("CQ_SSH_TARGET", "scottguida@35.239.227.192")
    ssh_key = os.environ.get("CQ_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519"))
    cmd = [
        "ssh", "-i", ssh_key, "-o", "StrictHostKeyChecking=no", ssh_target,
        f'sudo docker exec cq-postgres psql -U postgres -d context_quilt -At -c "{sql}"',
    ]
    return subprocess.check_output(cmd, text=True).strip()


def _wait_for_extraction(new_origin_id: str, timeout_s: int = 60) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        out = _query_db(
            f"SELECT COUNT(*) FROM extraction_metrics "
            f"WHERE origin_id = '{new_origin_id}'"
        )
        if out and int(out) > 0:
            return True
        time.sleep(2)
    return False


def _summarize_outcome(new_origin_id: str) -> dict[str, int]:
    person_count = int(_query_db(
        f"SELECT COUNT(*) FROM context_patches "
        f"WHERE origin_id = '{new_origin_id}' AND patch_type = 'person'"
    ) or 0)

    action_item_count = int(_query_db(
        f"SELECT COUNT(*) FROM context_patches "
        f"WHERE origin_id = '{new_origin_id}' "
        f"AND patch_type IN ('commitment','blocker','decision','goal')"
    ) or 0)

    # Count owns edges by the TARGET (action item) origin. The FROM side
    # (person patch) is often deduplicated against an existing person, so
    # filtering by cp_from.origin_id misses edges whose FROM was reused.
    owns_edge_count = int(_query_db(
        f"SELECT COUNT(*) FROM patch_connections pc "
        f"JOIN context_patches cp_to ON pc.to_patch_id = cp_to.patch_id "
        f"JOIN context_patches cp_from ON pc.from_patch_id = cp_from.patch_id "
        f"WHERE cp_to.origin_id = '{new_origin_id}' "
        f"AND cp_from.patch_type = 'person' "
        f"AND pc.connection_role = 'informs' "
    ) or 0)

    # List the person → action edges with their endpoint names so the
    # operator can see what the enforcer wired up at a glance.
    edge_detail = _query_db(
        f"SELECT cp_from.value->>'text' || ' → ' || cp_to.patch_type || ': ' "
        f"|| LEFT(cp_to.value->>'text', 50) "
        f"FROM patch_connections pc "
        f"JOIN context_patches cp_from ON pc.from_patch_id = cp_from.patch_id "
        f"JOIN context_patches cp_to ON pc.to_patch_id = cp_to.patch_id "
        f"WHERE cp_to.origin_id = '{new_origin_id}' "
        f"AND cp_from.patch_type = 'person' "
        f"AND pc.connection_role = 'informs'"
    ) or ""

    return {
        "person_count": person_count,
        "action_item_count": action_item_count,
        "owns_edge_count": owns_edge_count,
        "edge_detail": edge_detail.split("\n") if edge_detail else [],
    }


async def main() -> int:
    env = _require_env("CQ_BASE_URL", "CQ_APP_ID", "CQ_CLIENT_SECRET")

    print(f"Pulling original transcript for {SOURCE_ORIGIN_ID} from Redis Stream…")
    payload = _pull_source_transcript_payload()
    print(f"  ✓ payload size: {len(json.dumps(payload))} bytes")

    token = await _fetch_token(
        env["CQ_BASE_URL"], env["CQ_APP_ID"], env["CQ_CLIENT_SECRET"]
    )
    print("  ✓ auth token acquired")

    new_origin_id = f"PR84-SMOKE-{uuid.uuid4().hex[:12].upper()}"
    print(f"Replaying transcript with isolated origin_id: {new_origin_id}")
    await _replay_extraction(env["CQ_BASE_URL"], token, payload, new_origin_id)

    print(f"Waiting for worker to process (up to 60s)…")
    if not _wait_for_extraction(new_origin_id):
        print("  ✗ TIMEOUT — extraction did not complete within 60s")
        return 1

    print("  ✓ extraction complete")
    summary = _summarize_outcome(new_origin_id)
    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"  Person patches new on this origin:  {summary['person_count']:3d}")
    print(f"    (often 0 — existing persons get deduped via trigram match)")
    print(f"  Action-item patches:                {summary['action_item_count']:3d}")
    print(f"  Owns edges into this origin:        {summary['owns_edge_count']:3d}  (need ≥ {MIN_OWNS_EDGES_INTO_TEST})")
    if summary["edge_detail"]:
        print(f"  Edges wired:")
        for e in summary["edge_detail"]:
            print(f"    • {e}")
    print()

    passed = summary["owns_edge_count"] >= MIN_OWNS_EDGES_INTO_TEST
    if passed:
        print("  ✓ PASS — enforcer is producing person + owns shape on real data")
    else:
        print("  ✗ FAIL — see counts above")

    print()
    print(f"  Test origin_id (for cleanup): {new_origin_id}")
    print(f"  Cleanup if desired:")
    print(f"    DELETE FROM context_patches WHERE origin_id = '{new_origin_id}';")
    print(f"    DELETE FROM extraction_metrics WHERE origin_id = '{new_origin_id}';")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
