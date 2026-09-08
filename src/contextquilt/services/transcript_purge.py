"""Clearing raw transcripts off the ingest stream when their meeting goes.

SCOTT'S RULING, 2026-09-07: a deleted meeting takes its transcript with
it, and a deleted project takes its meetings' transcripts too.

WHAT WAS TRUE BEFORE, AND WHY IT WAS AN ACCIDENT RATHER THAN A POLICY.
Every `/v1/memory` POST persists on the `memory_updates` Redis stream
WITH ITS BODY, because the stream is how the worker is woken. Measured
2026-09-06: 1,477 entries, max-deleted-id 0-0, first entry 2026-03-22,
1,287 carrying a transcript, and no XTRIM anywhere in the source.
Nothing had ever been trimmed in five and a half months. `account_purge`
was the only thing that had ever deleted an entry, so a user who deleted
a meeting to be rid of it had no mechanism that did, and no way to find
that out. `maxmemory-policy allkeys-lru` at 256MB meant retention was
decided by luck rather than by anyone.

WHAT THIS COSTS, said plainly because it is not recoverable:
RE-EXTRACTION DIES WITH THE TRANSCRIPT. Every backfill that replays
history (`backfill_person_appearances`, `replay_gated_meetings`,
`backfill_origin_projects`) reads this stream, so a meeting cleared here
can never be re-extracted, re-scored, or repaired by a future prompt.
That is the price of the delete meaning what it says, and Scott chose it
against these numbers.

WHAT IT CANNOT REACH, and this bounds any "no remnants" claim made on
top of it. Measured on the largest account: 294 of 1,202 entries, about
a quarter, carry no resolvable origin and no project, so neither a
meeting delete nor a project delete will ever touch them. Only an
account purge clears those. A further 24 entries sit against projects
that are ALREADY archived and hold zero meetings and zero patches, which
is the remnant class this ruling exists to stop growing.

The matcher is pure and unit-tested; the sweep takes the client. Same
split as `account_purge`, and the same conservative rule: an entry that
does not parse is never matched, because deleting somebody's data on a
parse guess is worse than leaving a row nobody can read.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional, Set

STREAM_KEY = "memory_updates"
STREAM_SCAN_BATCH = 500


def entry_origin(raw_data: object, user_id: str) -> Optional[tuple]:
    """`(origin_id, project_id)` for one stream entry, or None.

    None means "not this user's, or unreadable, or carries neither", and
    every one of those must fail closed. Both halves are returned rather
    than a boolean because the caller matches on either: a meeting delete
    keys on origin_id, a project delete resolves origin_id through
    `origin_project_assignments` and falls back to the entry's own
    project_id for a meeting that was never assigned.
    """
    if not raw_data:
        return None
    try:
        payload = json.loads(raw_data)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("user_id") != user_id:
        return None
    meta = payload.get("metadata")
    if not isinstance(meta, dict):
        return None
    origin_id = meta.get("origin_id")
    project_id = meta.get("project_id")
    origin_id = origin_id if isinstance(origin_id, str) and origin_id else None
    project_id = project_id if isinstance(project_id, str) and project_id else None
    if origin_id is None and project_id is None:
        return None
    return (origin_id, project_id)


def entry_matches(
    raw_data: object,
    user_id: str,
    *,
    origin_ids: Optional[Set[str]] = None,
    project_id: Optional[str] = None,
) -> bool:
    """True when this entry belongs to the meetings (or project) being cleared.

    `origin_ids` is the resolved meeting set, which the caller reads from
    `origin_project_assignments` for a project delete or holds directly
    for a meeting delete. `project_id` is the FALLBACK only: an entry
    whose meeting was never assigned anywhere still carries the project
    it arrived under, and 24 entries on prod sit exactly there, against
    projects that are already archived with no assignment rows at all.

    A caller passing neither matches nothing. That is deliberate: an
    empty scope must clear nothing rather than everything, which is the
    same failure direction #466 chose for its delete flag.
    """
    if not origin_ids and not project_id:
        return False
    parsed = entry_origin(raw_data, user_id)
    if parsed is None:
        return False
    origin_id, entry_project = parsed
    if origin_ids and origin_id is not None and origin_id in origin_ids:
        return True
    if project_id and entry_project == project_id:
        return True
    return False


async def sweep(
    redis_client,
    user_id: str,
    *,
    origin_ids: Optional[Iterable[str]] = None,
    project_id: Optional[str] = None,
    apply: bool = False,
) -> Dict[str, Any]:
    """Count matching stream entries, and delete them when `apply`.

    PREVIEW AND DELETE WALK THE SAME PREDICATE, on purpose. #466 already
    established that the number a user is warned with cannot come from a
    different query than the one that does the deleting, and a transcript
    count is the number most likely to be quoted in a dialog.

    Batched XRANGE scan with an exclusive cursor, XDEL on matches. Bytes
    are reported because "35 recordings" and "0.34 MB" answer different
    questions and a client may want either.
    """
    ids = {o for o in (origin_ids or ()) if isinstance(o, str) and o}
    matched = 0
    matched_bytes = 0
    deleted = 0
    cursor = "-"
    while True:
        entries = await redis_client.xrange(
            STREAM_KEY, min=cursor, count=STREAM_SCAN_BATCH)
        if not entries:
            break
        hits = []
        for entry_id, fields in entries:
            raw = fields.get("data")
            if entry_matches(raw, user_id, origin_ids=ids, project_id=project_id):
                hits.append(entry_id)
                matched += 1
                matched_bytes += len(raw or "")
        if hits and apply:
            deleted += await redis_client.xdel(STREAM_KEY, *hits)
        last_id = entries[-1][0]
        if len(entries) < STREAM_SCAN_BATCH:
            break
        cursor = "(" + last_id  # exclusive resume, same as account_purge
    return {
        "matched": matched,
        "bytes": matched_bytes,
        "deleted": deleted,
        "applied": bool(apply),
    }
