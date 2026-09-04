#!/usr/bin/env python3
"""Archive behavior rows recorded about the user themselves.

`BEHAVIOR_SYSTEM` says outright: "Never record an observation about the
speaker marked (you). That is the user, and this corpus is about the
people they work with." On 2026-09-04 production held 130 active rows
that were about the users.

The ingest rule is `extraction_schema._is_self_owner`, which recognises
the marker forms on its own and needs `user_label` to recognise a bare
name. The main extraction chain never passed it. This script is the
repair for the history that made.

TWO THINGS IT DOES NOT DO, both deliberate.

It does not guess the label. It reads the `user_label` each user's own
ingests actually carried, off the `memory_updates` stream, which is the
same field the fixed ingest path reads. Inventing a label from a display
name or a profile would be a second source of truth for "who is the
user", and the whole defect being repaired is what happens when one rule
has two sources.

It does not restate the predicate. It imports `_is_self_owner` and hands
it the label, so history and live ingest cannot disagree about a
sentence. That is the standing rule for backfills in this repo.

A user whose ingests never carried a label is REPORTED AND SKIPPED
rather than guessed at. Roughly half of prod ingests carry none, and
archiving somebody's colleague because the script assumed a name is a
worse outcome than leaving a row.

Dry run by default. `--apply` writes.

    DATABASE_URL=... REDIS_URL=... python scripts/backfill_self_owned_behavior.py
    DATABASE_URL=... REDIS_URL=... python scripts/backfill_self_owned_behavior.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict

import asyncpg
import redis.asyncio as redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, "/app/src")

from contextquilt.services.extraction_schema import (  # noqa: E402
    BEHAVIOR_OBSERVATION_TYPES,
    _is_self_owner,
)

STREAM = "memory_updates"
ARCHIVE_CAUSE = "self_observation"


async def labels_by_user(client) -> dict[str, set[str]]:
    """Every `user_label` each user's own ingests carried.

    A set rather than one value: a user can legitimately appear under
    more than one form over time, and taking only the newest would miss
    rows written under an earlier one.
    """
    out: dict[str, set[str]] = defaultdict(set)
    entries = await client.xrange(STREAM, min="-", max="+", count=100000)
    for _id, fields in entries:
        raw = fields.get("data")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        uid = payload.get("user_id")
        label = (payload.get("metadata") or {}).get("user_label")
        if uid and isinstance(label, str) and label.strip():
            out[uid].add(label.strip())
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    dsn, rurl = os.environ.get("DATABASE_URL"), os.environ.get("REDIS_URL")
    if not dsn or not rurl:
        print("DATABASE_URL and REDIS_URL are required", file=sys.stderr)
        return 2

    rc = redis.from_url(rurl, decode_responses=True)
    try:
        labels = await labels_by_user(rc)
    finally:
        await rc.aclose()
    print(f"users with at least one observed user_label: {len(labels)}")

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT cp.patch_id, ps.subject_key, cp.value->>'owner' AS owner,
                   left(cp.value->>'text', 110) AS text, cp.source_prompt,
                   cp.created_at::date AS created
            FROM context_patches cp
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            WHERE cp.patch_type = ANY($1::text[])
              AND cp.status = 'active'
              AND cp.value->>'owner' IS NOT NULL
            ORDER BY cp.created_at
            """,
            list(BEHAVIOR_OBSERVATION_TYPES),
        )
        print(f"active rows with an owner: {len(rows)}")

        hits, skipped = [], defaultdict(int)
        for r in rows:
            uid = r["subject_key"].split("user:", 1)[-1]
            known = labels.get(uid)
            if not known:
                skipped[uid] += 1
                continue
            if any(_is_self_owner(r["owner"], lab) for lab in known):
                hits.append(r)

        if skipped:
            print(f"\nSKIPPED, no observed user_label so nothing to match on: "
                  f"{sum(skipped.values())} rows across {len(skipped)} users")
            print("  (not guessed at; a wrong guess archives somebody's colleague)")

        if not hits:
            print("\nNo self-owned rows found. The predicate ran against "
                  f"{len(rows) - sum(skipped.values())} rows that had a label.")
            return 0

        print(f"\nSELF-OWNED: {len(hits)}\n")
        per_user = defaultdict(list)
        for r in hits:
            per_user[r["subject_key"]].append(r)
        for subject, items in sorted(per_user.items(), key=lambda kv: -len(kv[1])):
            uid = subject.split("user:", 1)[-1]
            by_src = defaultdict(int)
            for r in items:
                by_src[r["source_prompt"]] += 1
            print(f"  {uid[:8]}  labels={sorted(labels.get(uid, []))}  "
                  f"{len(items)} rows  by producer: {dict(by_src)}")
            for r in items[:3]:
                print(f"      {r['created']}  {r['owner']!r}: {r['text'][:90]}")
            if len(items) > 3:
                print(f"      ... and {len(items) - 3} more")

        if not args.apply:
            print(f"\nDRY RUN. --apply archives these {len(hits)} rows with "
                  f"archive_cause={ARCHIVE_CAUSE!r}.")
            return 0

        ids = [r["patch_id"] for r in hits]
        result = await conn.execute(
            """
            UPDATE context_patches
               SET status = 'archived',
                   updated_at = NOW(),
                   value = jsonb_set(value, '{archive_cause}', $2::jsonb)
             WHERE patch_id = ANY($1::uuid[])
            """,
            ids, f'"{ARCHIVE_CAUSE}"',
        )
        print(f"\narchived: {result}")
        print("Archived, not deleted, so the delta sync's `deleted` array "
              "carries them to every client.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
