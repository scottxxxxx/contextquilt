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

import hashlib
import json
from typing import Any, Dict, Optional, Tuple

STREAM_KEY = "memory_updates"
STREAM_SCAN_BATCH = 500


def origins_key(user_id: str) -> str:
    return f"ingest_origins:{user_id}"


def payload_origin(payload: Any) -> Optional[Tuple[str, str, str]]:
    """`(user_id, origin_id, interaction_type)` for an ingest payload.

    None when the user or origin is missing or not a non-empty string. A
    payload with no origin cannot be a replay OF anything, so it appends
    as before; this must fail toward "append", never toward "skip".

    THE TYPE IS PART OF THE KEY, and leaving it out was a bug in the
    first version of this module. One meeting legitimately produces
    SEVERAL different ingests: measured 2026-09-14 on prod, 102 origins
    carry both an `analysis` and a `meeting_transcript` payload, which
    are two different records of one meeting and not two deliveries of
    one record. Keyed on (user, origin) alone they looked like
    duplicates, so a marked replay of the transcript would have been
    REFUSED because an analysis for that origin already existed, and the
    transcript would never have landed. Silent, and exactly the failure
    this module exists to prevent.
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
    kind = payload.get("interaction_type") or payload.get("type") or ""
    return user_id, origin_id, (kind if isinstance(kind, str) else "")


def entry_is_origin(raw_data: object, user_id: str, origin_id: str,
                    kind: str = "") -> bool:
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
    return parsed == (user_id, origin_id, kind)


async def _scan_for_origin(redis_client, user_id: str, origin_id: str,
                           kind: str = "") -> bool:
    cursor = "-"
    while True:
        entries = await redis_client.xrange(STREAM_KEY, min=cursor, count=STREAM_SCAN_BATCH)
        if not entries:
            return False
        for _entry_id, fields in entries:
            if entry_is_origin(fields.get("data"), user_id, origin_id, kind):
                return True
        if len(entries) < STREAM_SCAN_BATCH:
            return False
        cursor = "(" + entries[-1][0]


def _member(origin_id: str, kind: str) -> str:
    """The SET member. Carries the TYPE, or the index would collapse an
    analysis and a transcript for one meeting back into one entry and
    reintroduce the bug payload_origin's docstring describes.

    A VISIBLE separator on purpose. The first version used a NUL byte,
    which is invisible in redis-cli, in a log line and in an approval
    dialog; an operator reading this SET should be able to see what the
    member is made of. Origin ids are UUIDs and types are identifiers,
    so neither half can contain '::'."""
    return f"{origin_id}::{kind}" if kind else origin_id


async def already_ingested(redis_client, user_id: str, origin_id: str,
                           kind: str = "") -> bool:
    """Has this user's ingest of THIS TYPE for this origin already landed.
    SET first, then one scan for history that predates the SET, recorded
    on a hit."""
    key = origins_key(user_id)
    if await redis_client.sismember(key, _member(origin_id, kind)):
        return True
    if await _scan_for_origin(redis_client, user_id, origin_id, kind):
        await redis_client.sadd(key, _member(origin_id, kind))
        return True
    return False


async def remember(redis_client, payload: Any) -> None:
    """Record an origin the moment its entry is written."""
    parsed = payload_origin(payload)
    if parsed:
        await redis_client.sadd(origins_key(parsed[0]), _member(parsed[1], parsed[2]))


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
    if await already_ingested(redis_client, parsed[0], parsed[1], parsed[2]):
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

    LATEST WINS AMONG BYTE-IDENTICAL COPIES ONLY. A group whose copies
    differ in content is left entirely alone (see the safety gate at the
    end of this function): those are two different transcripts for one
    meeting, not one transcript delivered twice, and no tie-break can
    choose between them without losing text. Entries with no
    resolvable origin are never touched: a quarter of the stream on the
    largest account carries neither origin nor project, and only an
    account purge reaches those.

    `entries` is an iterable of (entry_id, raw_data). Returns
    {"delete": [entry_ids], "keep": {(user, origin): entry_id},
     "origins": {user_id: set(origin_ids)}, "marker_stats": {...}} where
    `origins` is every (origin, type) member seen, for populating the
    per-user SET in the same pass, and `marker_stats` answers ShoulderSurf's question:
    of the REPEAT entries (every entry of an origin after its first), how
    many carry the recovery marker. A repeat since MARKER_STAMPED_SINCE_MS
    with no marker is a producer of duplicates nobody has named, and the
    ingest dedupe does not stop it.
    """
    latest: Dict[Tuple[str, str], str] = {}
    delete = []
    origins: Dict[str, set] = {}
    first_seen: Dict[Tuple[str, str], Tuple[int, int]] = {}
    # Payload digests per (user, origin). A group whose copies are NOT
    # byte-identical is never deleted from: see the header.
    digests: Dict[Tuple[str, str], set] = {}
    by_key: Dict[Tuple[str, str], list] = {}
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
        origins.setdefault(parsed[0], set()).add(_member(parsed[1], parsed[2]))
        digests.setdefault(parsed, set()).add(
            hashlib.sha256((raw or "").encode("utf-8")).hexdigest())
        by_key.setdefault(parsed, []).append(entry_id)
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
    # SAFETY GATE, and it is the whole reason this function is not just
    # "latest wins". A group whose copies differ in CONTENT is not a
    # duplicate delivery of one transcript, it is two different
    # transcripts for one meeting, and deleting either loses text that
    # exists nowhere else. Measured 2026-09-14 on prod: 278 duplicate
    # groups, only 61 byte-identical, 217 DIFFERING, with sizes like
    # 52,538 against 13,129 characters for the same origin seconds
    # apart. Latest-wins across those would have thrown away the longer
    # transcript in every group where the shorter one arrived second.
    #
    # So deletion is restricted to groups whose every copy hashes the
    # same. Differing groups are REPORTED and left alone; what to do
    # with them is a judgement about content, not a tie-break rule.
    ambiguous = {k: sorted(v) for k, v in by_key.items()
                 if len(digests.get(k, ())) > 1}
    ambiguous_ids = {eid for ids in ambiguous.values() for eid in ids}
    delete = [eid for eid in delete if eid not in ambiguous_ids]
    return {"delete": delete, "keep": latest, "origins": origins,
            "marker_stats": stats, "ambiguous": ambiguous}


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
