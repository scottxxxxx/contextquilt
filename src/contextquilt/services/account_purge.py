"""Account purge: the required action behind `account_deleted`.

Per the cq-tier-signals contract (GP, 2026-07-25), `account_deleted`
(new_tier literally 'deleted') is the deletion request for everything
CQ holds for a user. This module implements the purge the worker's
tier-signals consumer runs against queued signals.

Design decisions (Scott, 2026-07-25):

- FULL HARD PURGE. The delta-sync tombstone rule ("archive, never
  hard-delete") deliberately does not apply — there is no client left
  to sync. Postgres rows, the per-user Redis indexes, and the user's
  `memory_updates` stream entries (raw transcript payloads live there)
  all go. Working-memory `context:{conversation_id}` keys are not
  user-addressable and expire on their own short TTL. Backups age out
  on the locked 30-day retention; the deletion receipt (the processed
  tier_signals row + the `account_purged` log line) is what remains.

- The signal row itself is KEPT (processed_at + action stamps): it is
  the durable receipt that a deletion was requested and honored.

- Consistency gate: purge fires only when event_type='account_deleted'
  AND new_tier='deleted' (both, per contract). A signal claiming
  deletion with an inconsistent shape is never processed destructively;
  it is stamped `skipped_inconsistent` and logged loudly for a human.

Purge order is FK-safe: patch satellites first (connections, cues,
observations, usage, ACL, subjects), then patches, then the entity
graph (relationships, aliases, entities), then facts and profile.
Multi-subject patches don't exist in practice (verified on prod
2026-07-25: zero), but the patch cascade is scoped to patches whose
subject rows ALL belong to the user, so a future shared patch would
survive with the other subject intact.
"""
from __future__ import annotations

import json
from typing import Any, Dict

# Signals classified per the contract vocabulary. Unknown event types
# are recorded, never processed destructively.
PURGE_EVENT_TYPE = "account_deleted"
PURGE_NEW_TIER = "deleted"

ACTION_PURGED = "purged"
ACTION_RECORD_ONLY = "recorded_only"
ACTION_INCONSISTENT = "skipped_inconsistent"

STREAM_KEY = "memory_updates"
STREAM_SCAN_BATCH = 500


def classify_signal(event_type: str, new_tier: object) -> str:
    """Decide what the consumer does with a queued signal.

    Returns ACTION_PURGED only for a shape-consistent account_deleted
    (event_type AND new_tier must both say deletion). An
    account-deleted-shaped signal that is internally inconsistent is
    ACTION_INCONSISTENT — never destructive on a malformed request.
    Everything else (the ordinary tier vocabulary and any unknown
    future event type) is ACTION_RECORD_ONLY.
    """
    et = (event_type or "").strip().lower()
    tier = (new_tier or "").strip().lower() if isinstance(new_tier, str) else ""
    if et == PURGE_EVENT_TYPE and tier == PURGE_NEW_TIER:
        return ACTION_PURGED
    if et == PURGE_EVENT_TYPE or tier == PURGE_NEW_TIER:
        return ACTION_INCONSISTENT
    return ACTION_RECORD_ONLY


def stream_entry_is_users(raw_data: object, user_id: str) -> bool:
    """True when a memory_updates stream entry belongs to the user.

    Entries are {'data': <json>} with user_id at the payload top level
    (both capture payloads and hydrate markers). Malformed entries are
    never matched — deleting someone else's data on a parse guess is
    worse than leaving an unparseable row.
    """
    if not raw_data:
        return False
    try:
        payload = json.loads(raw_data)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("user_id") == user_id


async def purge_user_data(db, redis_client, user_id: str) -> Dict[str, Any]:
    """Hard-delete everything CQ holds for `user_id`. Returns counts.

    Postgres deletes run in a single transaction on a dedicated pool
    connection; Redis index deletion and the stream sweep run after
    commit (idempotent — a crash between the two is healed by re-running,
    and the consumer only stamps processed_at after this returns).
    """
    subject_key = f"user:{user_id}"
    counts: Dict[str, Any] = {}

    async with db.acquire() as conn:
        async with conn.transaction():
            # Patches whose every subject row is this user (in practice:
            # all of the user's patches; shared patches would survive).
            patch_ids = [
                r["patch_id"] for r in await conn.fetch(
                    """
                    SELECT ps.patch_id
                    FROM patch_subjects ps
                    GROUP BY ps.patch_id
                    HAVING bool_and(ps.subject_key = $1)
                    """,
                    subject_key,
                )
            ]
            counts["patches"] = len(patch_ids)
            if patch_ids:
                for table, col_sql in (
                    ("patch_connections", "from_patch_id = ANY($1) OR to_patch_id = ANY($1)"),
                    ("patch_cues", "patch_id = ANY($1)"),
                    ("patch_observations", "patch_id = ANY($1)"),
                    ("patch_usage_metrics", "patch_id = ANY($1)"),
                    ("context_patch_acl", "patch_id = ANY($1)"),
                    ("patch_subjects", "patch_id = ANY($1)"),
                    ("context_patches", "patch_id = ANY($1)"),
                ):
                    result = await conn.execute(
                        f"DELETE FROM {table} WHERE {col_sql}", patch_ids
                    )
                    counts[table] = int(result.split()[-1])
            # Subject rows the user holds on shared patches (none today,
            # future-proofing): drop the user's claim, keep the patch.
            await conn.execute(
                "DELETE FROM patch_subjects WHERE subject_key = $1", subject_key
            )
            for table in ("relationships", "entity_aliases", "entities", "facts", "profiles"):
                result = await conn.execute(
                    f"DELETE FROM {table} WHERE user_id = $1", user_id
                )
                counts[table] = int(result.split()[-1])

    # Per-user Redis indexes (working memory context:{conversation} keys
    # are not user-addressable and expire on their own TTL).
    counts["redis_indexes"] = await redis_client.delete(
        f"entity_index:{user_id}", f"cue_index:{user_id}"
    )

    # memory_updates stream: the raw capture payloads (full transcripts)
    # for this user. Batched XRANGE scan, XDEL matches.
    deleted_entries = 0
    cursor = "-"
    while True:
        entries = await redis_client.xrange(STREAM_KEY, min=cursor, count=STREAM_SCAN_BATCH)
        if not entries:
            break
        to_delete = [
            entry_id for entry_id, fields in entries
            if stream_entry_is_users(fields.get("data"), user_id)
        ]
        if to_delete:
            deleted_entries += await redis_client.xdel(STREAM_KEY, *to_delete)
        last_id = entries[-1][0]
        if len(entries) < STREAM_SCAN_BATCH:
            break
        cursor = "(" + last_id  # exclusive resume
    counts["stream_entries"] = deleted_entries

    return counts
