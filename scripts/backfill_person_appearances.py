#!/usr/bin/env python3
"""
Reconstruct person_appearances for history (docs/architecture/16-people.md 8c).

person_appearances only fills forward from the next extraction. Without a
backfill every person shows a zero meeting count on day one and the People
feature reads as knowing nothing, which is the opposite of the point.

THREE TIERS, and the split is the whole design. `meeting_count` renders to
a user as "9 meetings", which is a claim about ATTENDANCE. Two tiers
support that claim and one does not.

  ownership  Postgres-derived, complete over all patch history with no
             retention dependency. A person appears if they own something
             anchored to that meeting, via a raw `value.owner` string or
             an explicit `owns` edge. Deterministic, no LLM call.

  speakers   Transcript speaker labels, resolved against known person
             entities. The STRONGEST attendance signal CQ holds: having
             spoken in a meeting is better evidence of being there than
             having owned an action item out of it, since work gets
             assigned in absentia.

  mentions   Anyone NAMED anywhere in a transcript. NOT attendance, and it
             must never feed meeting_count. It counts people discussed in
             absentia or named once in passing, and on prod it took the
             busiest person from 37 meetings to 179. It IS the right
             number for the provenance line in design 1e ("named in 11
             transcripts"), which needs the `source` column proposed in
             8c before it can land anywhere useful.

`--tier attendance` (the default) runs ownership + speakers and is what
you almost always want.

Both stream-backed tiers are bounded by retention: `memory_updates` has
no settled MAXLEN policy, which is exactly why `ownership` exists and
cannot be replaced by them.

Everything is idempotent and safe to re-run. Timestamps come from the
source patch or stream entry, never NOW(): importing at wall-clock time
would make `last_seen_at DESC` meaningless and tell the app every meeting
happened today.

USAGE

    DATABASE_URL=... python scripts/backfill_person_appearances.py
    DATABASE_URL=... python scripts/backfill_person_appearances.py --apply
    ... --tier speakers --apply      one tier
    ... --tier ownership,speakers    explicit set
    ... --user <user_id>             restrict to one user
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict

import asyncpg
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Compound owners ("Thorne/Lockridge") are split the same way the extraction
# sanitizers split them, so this agrees with what the worker would record.
from contextquilt.services.extraction_schema import (  # noqa: E402
    _split_compound_owner,
    is_placeholder_or_self_person,
)

# A surface form shorter than this is not matched in free text: "Ann"
# would fire inside "Announce", and a backfill that writes noise is worse
# than one that misses a row.
MIN_FREE_TEXT_NAME = 4

# The ingest stream both stream-backed tiers read.
STREAM_KEY = "memory_updates"


def _as_dt(value):
    """Coerce a stream timestamp to a datetime, or None.

    Patch-derived tiers get real datetimes from asyncpg. Stream-derived
    tiers get whatever JSON held, which is an ISO string, and asyncpg
    rejects a str for a timestamptz parameter even with a cast in the
    SQL. Returning None is safe: the writer COALESCEs to NOW().
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if url:
        return url
    host = os.environ.get("REDIS_HOST", "localhost")
    port = os.environ.get("REDIS_PORT", "6379")
    pw = os.environ.get("REDIS_PASSWORD")
    return f"redis://:{pw}@{host}:{port}" if pw else f"redis://{host}:{port}"


async def load_people(conn, user_id: str | None):
    """surface form (lowercased) -> canonical entity_id, per user.

    Merged entities resolve forward, so a name folded into someone else
    lands on the survivor rather than reviving the dead row.
    """
    rows = await conn.fetch(
        """
        SELECT e.entity_id, e.user_id, e.name, e.merged_into
        FROM entities e
        WHERE e.entity_type = 'person'
          AND ($1::text IS NULL OR e.user_id = $1)
        """,
        user_id,
    )
    merged = {str(r["entity_id"]): str(r["merged_into"]) for r in rows if r["merged_into"]}

    def resolve(eid: str) -> str:
        seen = set()
        while eid in merged and eid not in seen:
            seen.add(eid)
            eid = merged[eid]
        return eid

    forms: dict[str, dict[str, str]] = defaultdict(dict)
    canonical_name: dict[str, str] = {}
    for r in rows:
        eid = resolve(str(r["entity_id"]))
        canonical_name[eid] = r["name"]
        if r["name"] and not is_placeholder_or_self_person(r["name"]):
            forms[r["user_id"]][r["name"].strip().lower()] = eid

    alias_rows = await conn.fetch(
        """
        SELECT a.user_id, a.alias, a.entity_id
        FROM entity_aliases a
        JOIN entities e ON e.entity_id = a.entity_id
        WHERE e.entity_type = 'person'
          AND ($1::text IS NULL OR a.user_id = $1)
        """,
        user_id,
    )
    for r in alias_rows:
        if r["alias"] and not is_placeholder_or_self_person(r["alias"]):
            forms[r["user_id"]][r["alias"].strip().lower()] = resolve(str(r["entity_id"]))

    return forms, canonical_name


async def tier_ownership(conn, forms, user_id: str | None):
    """Owner strings and `owns` edges on origin-bearing patches."""
    found: dict[tuple, dict] = {}

    def record(uid, eid, origin_id, origin_type, project_id, ts):
        key = (uid, eid, origin_id)
        cur = found.get(key)
        if cur is None:
            found[key] = {
                "user_id": uid, "entity_id": eid, "origin_id": origin_id,
                "origin_type": origin_type or "meeting", "project_id": project_id,
                "first": ts, "last": ts,
            }
            return
        if ts and cur["first"] and ts < cur["first"]:
            cur["first"] = ts
        if ts and cur["last"] and ts > cur["last"]:
            cur["last"] = ts
        if cur["project_id"] is None:
            cur["project_id"] = project_id

    # 1a. Raw value.owner on anything anchored to an origin.
    owner_rows = await conn.fetch(
        """
        SELECT ps.subject_key, cp.origin_id, cp.origin_type, cp.project_id,
               cp.created_at, cp.value->>'owner' AS owner
        FROM context_patches cp
        JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
        WHERE cp.origin_id IS NOT NULL
          AND cp.value->>'owner' IS NOT NULL
          AND ($1::text IS NULL OR ps.subject_key = 'user:' || $1)
        """,
        user_id,
    )
    for r in owner_rows:
        uid = r["subject_key"].removeprefix("user:")
        table = forms.get(uid) or {}
        for part in _split_compound_owner(r["owner"]):
            eid = table.get((part or "").strip().lower())
            if eid:
                record(uid, eid, r["origin_id"], r["origin_type"],
                       r["project_id"], r["created_at"])

    # 1b. Explicit `owns` edges: person patch -> item anchored to an origin.
    owns_rows = await conn.fetch(
        """
        SELECT ps.subject_key, tgt.origin_id, tgt.origin_type, tgt.project_id,
               tgt.created_at, src.value->>'text' AS person_text
        FROM patch_connections pc
        JOIN context_patches src ON src.patch_id = pc.from_patch_id
        JOIN context_patches tgt ON tgt.patch_id = pc.to_patch_id
        JOIN patch_subjects ps ON ps.patch_id = tgt.patch_id
        WHERE pc.connection_label = 'owns'
          AND src.patch_type = 'person'
          AND tgt.origin_id IS NOT NULL
          AND ($1::text IS NULL OR ps.subject_key = 'user:' || $1)
        """,
        user_id,
    )
    for r in owns_rows:
        uid = r["subject_key"].removeprefix("user:")
        eid = (forms.get(uid) or {}).get((r["person_text"] or "").strip().lower())
        if eid:
            record(uid, eid, r["origin_id"], r["origin_type"],
                   r["project_id"], r["created_at"])

    return found


SPEAKER_LABEL = re.compile(r"^\s*\[([^\]]{1,60})\]", re.MULTILINE)


async def tier_speakers(redis_url, forms, user_id, existing_keys):
    """Attendance from transcript speaker labels.

    The strongest attendance signal CQ holds. `meeting_count` renders to a
    user as "9 meetings", which is a claim about being THERE, and having
    spoken in a meeting is better evidence of that than having owned an
    action item out of it (people get assigned work in absentia).

    Only labels that resolve to a KNOWN person entity are recorded. That
    is what keeps caption-scanner noise out: ShoulderSurf's screen-capture
    reader hands on-screen text to a name-shape extractor, so strings like
    "Ask Gemini" and "BUILD SUCCEEDED" arrive shaped exactly like two-word
    human names and nothing downstream catches them. Resolving against the
    entity table drops them without needing a blocklist, because they were
    never people.

    Measured on prod 2026-08-02: 189 distinct non-placeholder speaker
    labels, 82 of which resolve to a person entity, taking coverage from
    93 people to 118 (+512 appearance rows). The 107 that do not resolve
    are a mix of that scanner noise and real people who spoke but were
    never extracted as entities. CQ cannot record a person it never
    learned, which is why this tier narrows the gap rather than closing
    it.
    """
    try:
        import redis.asyncio as redis
    except ImportError:
        print("  speaker tier skipped: redis package unavailable", file=sys.stderr)
        return {}

    client = redis.from_url(redis_url, decode_responses=True)
    try:
        entries = await client.xrange(STREAM_KEY, "-", "+")
    except Exception as e:
        # Include the type. This guard exists for retention and
        # connectivity, but it will happily swallow a code bug and report
        # it as a stream problem, which is how a NameError once looked
        # like an empty stream and produced a silent zero.
        print(f"  speaker tier skipped: {type(e).__name__}: {str(e)[:100]}",
              file=sys.stderr)
        return {}
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

    found: dict[tuple, dict] = {}
    for _sid, fields in entries:
        raw = fields.get("data")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        content = payload.get("content")
        meta = payload.get("metadata") or {}
        uid = payload.get("user_id")
        origin_id = meta.get("origin_id")
        if not (content and uid and origin_id):
            continue
        if user_id and uid != user_id:
            continue

        table = forms.get(uid) or {}
        ts = payload.get("timestamp")
        for raw_label in SPEAKER_LABEL.findall(content):
            # The (you) marker identifies the app user; strip it so the
            # label matches the entity name it was extracted from.
            name = raw_label.replace("(you)", "").strip()
            if not name or is_placeholder_or_self_person(name):
                continue
            eid = table.get(name.lower())
            if not eid:
                continue
            key = (uid, eid, str(origin_id))
            if key in existing_keys or key in found:
                continue
            found[key] = {
                "user_id": uid, "entity_id": eid, "origin_id": str(origin_id),
                "origin_type": meta.get("origin_type") or "meeting",
                "project_id": meta.get("project_id"),
                "first": _as_dt(ts), "last": _as_dt(ts),
            }
    return found


async def tier_mentions(redis_url, forms, user_id, existing_keys):
    """Mention-level appearances from retained transcripts.

    Only adds keys tier 1 did not already find, so the cheaper and more
    certain signal always wins on timestamps and project scope.
    """
    try:
        import redis.asyncio as redis
    except ImportError:
        print("  tier 2 skipped: redis package unavailable", file=sys.stderr)
        return {}

    client = redis.from_url(redis_url, decode_responses=True)
    try:
        entries = await client.xrange(STREAM_KEY, "-", "+")
    except Exception as e:
        print(f"  mentions tier skipped: {type(e).__name__}: {str(e)[:100]}",
              file=sys.stderr)
        return {}
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

    found: dict[tuple, dict] = {}
    patterns: dict[str, list] = {}

    for _sid, fields in entries:
        raw = fields.get("data")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        content = payload.get("content")
        meta = payload.get("metadata") or {}
        uid = payload.get("user_id")
        origin_id = meta.get("origin_id")
        if not (content and uid and origin_id):
            continue
        if user_id and uid != user_id:
            continue

        table = forms.get(uid) or {}
        if uid not in patterns:
            patterns[uid] = [
                (re.compile(rf"\b{re.escape(form)}\b", re.IGNORECASE), eid)
                for form, eid in table.items()
                if len(form) >= MIN_FREE_TEXT_NAME
            ]

        ts = payload.get("timestamp")
        for rx, eid in patterns[uid]:
            key = (uid, eid, str(origin_id))
            if key in existing_keys or key in found:
                continue
            if rx.search(content):
                found[key] = {
                    "user_id": uid, "entity_id": eid, "origin_id": str(origin_id),
                    "origin_type": meta.get("origin_type") or "meeting",
                    "project_id": meta.get("project_id"),
                    "first": _as_dt(ts), "last": _as_dt(ts),
                }
    return found


async def write(conn, rows) -> int:
    written = 0
    for r in rows.values():
        await conn.execute(
            """
            INSERT INTO person_appearances
                (user_id, entity_id, origin_id, origin_type, project_id,
                 first_seen_at, last_seen_at)
            VALUES ($1, $2::uuid, $3, $4, $5,
                    COALESCE($6::timestamptz, NOW()),
                    COALESCE($7::timestamptz, NOW()))
            ON CONFLICT (user_id, entity_id, origin_id) DO UPDATE SET
                first_seen_at = LEAST(person_appearances.first_seen_at,
                                      EXCLUDED.first_seen_at),
                last_seen_at  = GREATEST(person_appearances.last_seen_at,
                                         EXCLUDED.last_seen_at),
                project_id    = COALESCE(person_appearances.project_id,
                                         EXCLUDED.project_id)
            """,
            r["user_id"], r["entity_id"], r["origin_id"], r["origin_type"],
            r["project_id"], r["first"], r["last"],
        )
        written += 1
    return written


async def main(args) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is required")

    conn = await asyncpg.connect(dsn)
    try:
        before = await conn.fetchval("SELECT count(*) FROM person_appearances")
        forms, canonical = await load_people(conn, args.user)
        print(f"known person surface forms: {sum(len(v) for v in forms.values())} "
              f"across {len(forms)} users")

        want = set(args.tier.split(",")) if args.tier != "attendance" else {"ownership", "speakers"}
        if args.tier == "all":
            want = {"ownership", "speakers", "mentions"}

        rows: dict = {}
        if "ownership" in want:
            t1 = await tier_ownership(conn, forms, args.user)
            print(f"ownership (owner strings + owns edges): {len(t1)} appearances")
            rows.update(t1)

        if "speakers" in want:
            redis_url = _redis_url()
            ts_ = await tier_speakers(redis_url, forms, args.user, set(rows.keys()))
            print(f"speakers  (transcript speaker labels): {len(ts_)} additional")
            rows.update(ts_)

        if "mentions" in want:
            t2 = await tier_mentions(_redis_url(), forms, args.user, set(rows.keys()))
            print(f"mentions  (named anywhere in text)   : {len(t2)} additional")
            rows.update(t2)

        people = len({(k[0], k[1]) for k in rows})
        meetings = len({(k[0], k[2]) for k in rows})
        print(f"\ntotal: {len(rows)} appearance rows, {people} people, {meetings} meetings")

        top = sorted(
            ((sum(1 for k in rows if k[1] == eid), eid) for eid in {k[1] for k in rows}),
            reverse=True,
        )[:8]
        print("busiest people:")
        for n, eid in top:
            print(f"  {n:4d} meetings  {canonical.get(eid, eid)}")

        if args.apply:
            written = await write(conn, rows)
            after = await conn.fetchval("SELECT count(*) FROM person_appearances")
            print(f"\nAPPLIED: {written} upserts, table {before} -> {after}")
        else:
            print(f"\nDRY RUN (use --apply to write). Table currently holds {before} rows.")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default is dry run)")
    ap.add_argument(
        "--tier", default="attendance",
        help=("attendance (default) = ownership + speakers, the two tiers "
              "that mean 'was in the meeting'. Also: ownership, speakers, "
              "mentions, all, or a comma-separated set. `mentions` counts "
              "people merely NAMED in a transcript and must not feed "
              "meeting_count; see docs/architecture/16-people.md 8c."))
    ap.add_argument("--user", help="restrict to one user_id")
    asyncio.run(main(ap.parse_args()))
