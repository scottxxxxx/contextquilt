#!/usr/bin/env python3
"""
One-shot recovery script to backfill origin_id on context_patches using
GhostPour's meeting_transcripts as the authoritative meeting map.

Context: the v1 migration (init-db/10_app_schema_registration.sql)
dropped context_patches.meeting_id without a backfill into the new
origin_id column — at the operator's instruction that pre-launch data
was disposable. That turned out to be wrong: SS's client groups patches
by meeting via origin_id, so every pre-migration patch became orphaned
from its meeting. See docs/ops/incidents/2026-04-19-origin-id-data-loss.md.

This script recovers the meeting association by matching each pre-v1
patch's created_at against GP's meeting_transcripts timestamps. A patch
is assigned to the latest GP meeting whose created_at <= patch.created_at
and within a 6h window.

Recovery ceiling: we can only recover meetings GP has records of.
GP's meeting_transcripts table started logging on ~2026-04-10; patches
from before that date remain origin_id=NULL.

Usage (inside the contextquilt container, with DATABASE_URL set):
    # 1. Export GP's meeting map (from inside GP container):
    python3 -c "
    import sqlite3, json
    conn = sqlite3.connect('/app/data/cloudzap.db')
    rows = conn.execute(
        'SELECT meeting_id, created_at FROM meeting_transcripts WHERE user_id = ?',
        ('<USER_UUID>',)
    ).fetchall()
    print(json.dumps([{'mid': m, 'ts': ts} for m, ts in rows]))
    " > /tmp/meetings.json

    # 2. Copy into the CQ container and run:
    docker cp /tmp/meetings.json contextquilt:/tmp/meetings.json
    docker exec -e USER_ID=<USER_UUID> -e MODE=dry    contextquilt \
        python3 /app/scripts/recover_origin_id_from_gp.py
    docker exec -e USER_ID=<USER_UUID> -e MODE=apply  contextquilt \
        python3 /app/scripts/recover_origin_id_from_gp.py

Env vars:
    USER_ID       — required; the user whose patches to backfill
    MODE          — 'dry' (default) to print plan; 'apply' to write
    WINDOW_HOURS  — optional; max gap between meeting and patch (default 6)
    MEETINGS_PATH — optional; path to meetings.json (default /tmp/meetings.json)
    DATABASE_URL  — required; Postgres connection string
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

import asyncpg


# Types that carry an origin per the v1 SS schema (project_scoped=true)
# plus `project` which is the container itself.
SCOPED_TYPES = (
    "decision", "commitment", "blocker", "takeaway",
    "goal", "constraint", "event", "role", "project",
)


async def main() -> int:
    user_id = os.environ.get("USER_ID", "").strip()
    if not user_id:
        print("ERROR: USER_ID env var required", file=sys.stderr)
        return 2

    mode = os.environ.get("MODE", "dry").strip().lower()
    window_hours = int(os.environ.get("WINDOW_HOURS", "6"))
    meetings_path = os.environ.get("MEETINGS_PATH", "/tmp/meetings.json")
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL env var required", file=sys.stderr)
        return 2

    try:
        with open(meetings_path) as f:
            meetings = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: meetings map not found at {meetings_path}", file=sys.stderr)
        return 2

    if not isinstance(meetings, list) or not meetings:
        print(f"ERROR: {meetings_path} is empty or malformed", file=sys.stderr)
        return 2

    for m in meetings:
        m["dt"] = datetime.fromisoformat(m["ts"].replace("Z", "+00:00"))
    meetings.sort(key=lambda x: x["dt"])
    window = timedelta(hours=window_hours)

    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(
            """
            SELECT cp.patch_id, cp.patch_type, cp.created_at
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND COALESCE(cp.status, 'active') = 'active'
              AND cp.origin_id IS NULL
              AND cp.patch_type = ANY($2::text[])
            ORDER BY cp.created_at
            """,
            f"user:{user_id}",
            list(SCOPED_TYPES),
        )

        matched: list[tuple[str, str, str]] = []  # (patch_id, patch_type, meeting_id)
        unmatched: list[tuple[str, str, str]] = []  # (patch_id, patch_type, iso_ts)
        for r in rows:
            pts = r["created_at"]
            best = None
            for m in meetings:
                if m["dt"] <= pts and pts - m["dt"] <= window:
                    if best is None or m["dt"] > best["dt"]:
                        best = m
            if best:
                matched.append((str(r["patch_id"]), r["patch_type"], best["mid"]))
            else:
                unmatched.append((str(r["patch_id"]), r["patch_type"], pts.isoformat()))

        print(f"User:         {user_id}")
        print(f"Meetings:     {len(meetings)} ({meetings[0]['ts']} → {meetings[-1]['ts']})")
        print(f"Window:       {window_hours}h")
        print(f"Candidates:   {len(rows)}")
        print(f"Matched:      {len(matched)}")
        print(f"Unmatched:    {len(unmatched)}")

        by_type_matched: dict[str, int] = {}
        for _, t, _ in matched:
            by_type_matched[t] = by_type_matched.get(t, 0) + 1
        by_type_unmatched: dict[str, int] = {}
        for _, t, _ in unmatched:
            by_type_unmatched[t] = by_type_unmatched.get(t, 0) + 1

        print(f"  by-type matched:   {by_type_matched}")
        print(f"  by-type unmatched: {by_type_unmatched}")

        if mode != "apply":
            if matched:
                print("\nSample matches:")
                for patch_id, patch_type, meeting_id in matched[:5]:
                    print(f"  {patch_id[:8]}... ({patch_type}) -> {meeting_id}")
            if unmatched:
                print("\nSample unmatched:")
                for patch_id, patch_type, ts in unmatched[:5]:
                    print(f"  {patch_id[:8]}... ({patch_type}) created={ts}")
            print("\nDRY RUN — no writes. Set MODE=apply to execute.")
            return 0

        print("\nAPPLYING...")
        applied = 0
        async with conn.transaction():
            for patch_id, _, meeting_id in matched:
                await conn.execute(
                    """
                    UPDATE context_patches
                    SET origin_id = $1,
                        origin_type = 'meeting',
                        updated_at = NOW()
                    WHERE patch_id = $2::uuid
                    """,
                    meeting_id,
                    patch_id,
                )
                applied += 1
        print(f"Applied: {applied} patches updated")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
