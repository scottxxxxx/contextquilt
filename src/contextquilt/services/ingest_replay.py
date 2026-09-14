"""A replayed ingest lands ONCE on the stream (Scott's ruling, 2026-09-14).

THE MARKER MEANS "DO NOT RE-INGEST". IT NEVER MEANS "TAKE THE NEWER ONE".
`X-CZ-Recovery` on a POST /v1/memory says the sender is replaying a
delivery it could not confirm. When an entry for that origin already
sits on `memory_updates`, the replay is acknowledged with the same 200
the first delivery got and NO second entry is written. The FIRST bytes
stay. That is correct today because ShoulderSurf's sweep replays the
identical persisted transcript, and it stays correct only as long as
nobody assumes a replay can correct an earlier upload. A re-rendered
transcript that must REPLACE an old one is a new explicit operation (a
different marker value, or delete-then-ingest), never the retry path
quietly doing something different. ShoulderSurf carries the same
sentence at MeetingEnrichmentCoordinator.swift:757, so both ends of the
wire agree on what the header is for.

ABSENT MEANS APPEND, deliberately. Without the marker CQ cannot tell a
menu retry from a deliberate re-send, and an unmarked re-ingest is
somebody asking for a new entry. Guessing is worse than a duplicate.

AND ONE UNMARKED PRODUCER IS OURS, ON PURPOSE.
`scripts/replay_gated_meetings.py --apply` republishes lost payloads
verbatim to re-run an extraction that was gated away. That is a
deliberate re-ingest, so it must NOT carry the marker: stamping it would
make this module refuse the write and the repair would become a silent
no-op. Measured 2026-09-14, the stream held 387 duplicate entries and
NONE carried a marker, in monthly bursts clustering milliseconds apart,
which is that script's signature and not a client retry. So the dedupe
here closes the replay path the marker names; it does not, and should
not, close deliberate re-ingest.

WHAT WAS TRUE BEFORE. The handler XADDed unconditionally, with no lookup
on origin_id, and since 2026-09-09 the stream is the only copy of a raw
transcript. Measured in the handler's own docstring: 61 origins with
byte-identical repeats, 86 entries in May and 33 in September. The
worker was already idempotent per origin for what it DERIVES
(`_apply_patch_dedup` refuses to move freshness anchors on a same-origin
re-observation, doc 19.4), which is why a comment in ShoulderSurf's tree
could say "CQ ingest is idempotent per meeting_id" and be half true: the
patches did not double. The transcript did, and every reader that walks
the stream by origin_id saw two.

WHY INGEST AND NOT READERS-TAKE-LATEST, in Scott's words as relayed: a
fix in one handler can be verified once; a fix in every reader can only
be verified until somebody writes another reader, and there are already
three backfills.

THE INDEX. A per-user Redis SET, `ingest_origins:{user_id}`, written on
every XADD. History predates the SET, so a marked replay that misses it
falls back to ONE XRANGE scan of the stream (the same parse
`transcript_purge` uses) and records the origin on a hit, so the scan
runs at most once per pre-SET origin. `scripts/dedupe_memory_updates.py`
populates the SET for all existing origins in the same pass that clears
the historical duplicates. The scan is bounded by the stream length,
runs only on a marked replay, and is on the async-queue path, not the
recall hot path.

Pure where it can be (the origin parse, the verdict shape); the two
async functions take the client. No fastapi import, so the two-POST
property is testable outside CI.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

STREAM_KEY = "memory_updates"
STREAM_SCAN_BATCH = 500


def origins_key(user_id: str) -> str:
    return f"ingest_origins:{user_id}"


def payload_origin(payload: Any) -> Optional[Tuple[str, str]]:
    """`(user_id, origin_id)` for an ingest payload, or None.

    None when either half is missing or not a non-empty string. A payload
    with no origin cannot be a replay OF anything, so it appends as
    before; this must fail toward "append", never toward "skip".
    """
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("user_id")
    meta = payload.get("metadata")
    origin_id = meta.get("origin_id") if isinstance(meta, dict) else None
    if not (isinstance(user_id, str) and user_id):
        return None
    if not (isinstance(origin_id, str) and origin_id):
        return None
    return user_id, origin_id


def entry_is_origin(raw_data: object, user_id: str, origin_id: str) -> bool:
    """Does one stream entry carry this user's ingest of this origin.

    Conservative on purpose, the same rule as `transcript_purge`: an
    entry that does not parse is never a match.
    """
    if not raw_data:
        return False
    try:
        payload = json.loads(raw_data)
    except (TypeError, ValueError):
        return False
    parsed = payload_origin(payload)
    return parsed == (user_id, origin_id)


async def _scan_for_origin(redis_client, user_id: str, origin_id: str) -> bool:
    cursor = "-"
    while True:
        entries = await redis_client.xrange(STREAM_KEY, min=cursor, count=STREAM_SCAN_BATCH)
        if not entries:
            return False
        for _entry_id, fields in entries:
            if entry_is_origin(fields.get("data"), user_id, origin_id):
                return True
        if len(entries) < STREAM_SCAN_BATCH:
            return False
        cursor = "(" + entries[-1][0]


async def already_ingested(redis_client, user_id: str, origin_id: str) -> bool:
    """Has this origin already landed for this user. SET first, then one
    scan for history that predates the SET, recorded on a hit."""
    key = origins_key(user_id)
    if await redis_client.sismember(key, origin_id):
        return True
    if await _scan_for_origin(redis_client, user_id, origin_id):
        await redis_client.sadd(key, origin_id)
        return True
    return False


async def remember(redis_client, payload: Any) -> None:
    """Record an origin the moment its entry is written."""
    parsed = payload_origin(payload)
    if parsed:
        await redis_client.sadd(origins_key(parsed[0]), parsed[1])


async def admit(redis_client, payload: Any, marker: Optional[str]) -> Dict[str, Any]:
    """Decide whether this ingest writes a stream entry.

    Returns {"write": bool, "deduplicated": bool, "origin_id": str|None}.
    `write` is False ONLY when the marker is present AND the origin is
    already on the stream. Every other case writes, including a marked
    replay of an origin that never landed (the first delivery failed
    before the XADD, so the replay IS the first).
    """
    parsed = payload_origin(payload)
    origin_id = parsed[1] if parsed else None
    if not marker or not parsed:
        return {"write": True, "deduplicated": False, "origin_id": origin_id}
    if await already_ingested(redis_client, parsed[0], parsed[1]):
        return {"write": False, "deduplicated": True, "origin_id": origin_id}
    return {"write": True, "deduplicated": False, "origin_id": origin_id}


# --- one-time cleanup of the duplicates that already exist ----------------

def _stream_id_key(entry_id: str) -> Tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    try:
        return int(ms), int(seq or 0)
    except ValueError:
        return (0, 0)


# #476 began stamping the recovery marker INTO the payload when it
# deployed, 2026-09-10 04:17:13Z. A repeat entry from before that carries
# no marker whatever the sender did, so "unmarked" is only evidence of an
# unnamed producer for entries after this instant.
MARKER_STAMPED_SINCE_MS = 1_789_013_833_000  # 2026-09-10T04:17:13Z


def plan_dedupe(entries) -> Dict[str, Any]:
    """Which existing entries to delete so each (user, origin) has one.

    LATEST WINS, by stream id, ties impossible (ids are unique) but the
    comparison is the full (ms, seq) pair so a same-millisecond pair
    resolves by sequence rather than by string order. Entries with no
    resolvable origin are never touched: a quarter of the stream on the
    largest account carries neither origin nor project, and only an
    account purge reaches those.

    `entries` is an iterable of (entry_id, raw_data). Returns
    {"delete": [entry_ids], "keep": {(user, origin): entry_id},
     "origins": {user_id: set(origin_ids)}, "marker_stats": {...}} where
    `origins` is every origin seen, for populating the per-user SET in
    the same pass, and `marker_stats` answers ShoulderSurf's question:
    of the REPEAT entries (every entry of an origin after its first), how
    many carry the recovery marker. A repeat since MARKER_STAMPED_SINCE_MS
    with no marker is a producer of duplicates nobody has named, and the
    ingest dedupe does not stop it.
    """
    latest: Dict[Tuple[str, str], str] = {}
    delete = []
    origins: Dict[str, set] = {}
    first_seen: Dict[Tuple[str, str], Tuple[int, int]] = {}
    stats = {"repeats": 0, "repeats_marked": 0,
             "repeats_since_marker": 0, "unmarked_since_marker": 0}
    for entry_id, raw in entries:
        try:
            payload = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            continue
        parsed = payload_origin(payload)
        if not parsed:
            continue
        origins.setdefault(parsed[0], set()).add(parsed[1])
        key = _stream_id_key(entry_id)
        held = latest.get(parsed)
        if held is None:
            latest[parsed] = entry_id
            first_seen[parsed] = key
            continue
        # A repeat: anything after the origin's earliest entry.
        stats["repeats"] += 1
        marked = bool(payload.get("recovery"))
        if marked:
            stats["repeats_marked"] += 1
        if key[0] >= MARKER_STAMPED_SINCE_MS:
            stats["repeats_since_marker"] += 1
            if not marked:
                stats["unmarked_since_marker"] += 1
        if key > _stream_id_key(held):
            delete.append(held)
            latest[parsed] = entry_id
        else:
            delete.append(entry_id)
    return {"delete": delete, "keep": latest, "origins": origins,
            "marker_stats": stats}


async def load_all(redis_client):
    """Every (entry_id, raw_data) on the stream, in id order."""
    out = []
    cursor = "-"
    while True:
        entries = await redis_client.xrange(STREAM_KEY, min=cursor, count=STREAM_SCAN_BATCH)
        if not entries:
            return out
        for entry_id, fields in entries:
            out.append((entry_id, fields.get("data")))
        if len(entries) < STREAM_SCAN_BATCH:
            return out
        cursor = "(" + entries[-1][0]
