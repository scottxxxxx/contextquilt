#!/usr/bin/env python3
"""Replay the transcripts lost to the gate-path crash (#415).

WHAT WAS LOST. Between 2026-08-31 02:55 UTC (when the extraction gate
shipped, c0c57d7 / #356) and 2026-09-03 05:40 UTC (when #415 deployed),
every transcript under the gate's 1200 character floor raised
UnboundLocalError inside `handle_meeting_summary`. The handler's blanket
`except Exception` swallowed it into one `meeting_summary_failed` line
and the task ended. Seventeen transcripts across seven users.

WHAT A REPLAY CAN AND CANNOT RECOVER, and the difference matters more
than the count. The gate was going to skip the MAIN extraction for all
seventeen anyway; that is the gate working, not the bug. What the crash
additionally destroyed was the behavior lane and the headline pass,
which #356 deliberately kept running on gated meetings. The behavior
lane has its OWN 400 character floor (services/behavior_extraction.py:44),
so a transcript below 400 produces nothing even on a healthy replay.
Six of the seventeen are at or above 400. The other eleven are replayed
for completeness and to make the set auditable, and they are expected to
yield nothing. A replay that "worked" and produced eleven empty results
is the correct outcome, not a failure.

WHY RE-SENDING IS SAFE HERE, specifically. Doc 19.4 says a re-ingest is
the same observation arriving twice and must not move anything that
measures recency. That protection is not what makes this safe: what
makes it safe is that the crash happened ABOVE every write, so nothing
at all was stored for these seventeen. There is no row to double count,
no `last_observed_at` to bump, no appearance to re-date. This is a first
ingest that is three days late.

The original payload is re-sent VERBATIM, which is load bearing. Its
`timestamp` is what becomes the `Meeting date:` prompt anchor and the
`origin_meetings` row. Minting a fresh one would date every replayed
meeting to the replay, which is exactly the shape of 19.4's fourth door:
the presence backfill supplied the wrong clock and 361 rows needed
repair. So: same bytes, same stream, same consumer, three days later.

Dry run by default. `--apply` re-publishes.

    DATABASE_URL=... REDIS_URL=... python scripts/replay_gated_meetings.py
    DATABASE_URL=... REDIS_URL=... python scripts/replay_gated_meetings.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys

import redis.asyncio as redis

STREAM = "memory_updates"

#: The window the crash was live. Start is the gate's deploy, end is
#: #415's. Both are UTC and both are deliberately explicit rather than
#: relative, so re-running this script next week replays the same set.
CRASH_START = dt.datetime(2026, 8, 31, 8, 0, tzinfo=dt.timezone.utc)
CRASH_END = dt.datetime(2026, 9, 3, 5, 40, tzinfo=dt.timezone.utc)

#: The gate's floor, and the behavior lane's. Read from the services
#: rather than restated where the import is available; hard-coded here
#: only as a fallback for running this outside the app image.
GATE_FLOOR = 1200
BEHAVIOR_FLOOR = 400


def _floors() -> tuple[int, int]:
    """Prefer the live floors, so this script cannot describe a gate that
    no longer exists. A drifted constant here would report the wrong set
    as recoverable, which is the number a human would act on."""
    gate, behavior = GATE_FLOOR, BEHAVIOR_FLOOR
    try:
        sys.path.insert(0, "/app/src")
        from contextquilt.services import extraction_gate  # noqa: E402
        from contextquilt.services import behavior_extraction  # noqa: E402
        gate = extraction_gate.min_transcript_chars()
        behavior = behavior_extraction.MIN_TRANSCRIPT_CHARS
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"note: using fallback floors ({exc.__class__.__name__}); "
              f"gate={gate} behavior={behavior}")
    return gate, behavior


async def collect(client, gate_floor: int) -> list[tuple[str, dict, int]]:
    start_ms = int(CRASH_START.timestamp() * 1000)
    end_ms = int(CRASH_END.timestamp() * 1000)
    entries = await client.xrange(STREAM, min=f"{start_ms}-0", max=f"{end_ms}-0",
                                  count=20000)
    out = []
    for entry_id, fields in entries:
        raw = fields.get("data")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if payload.get("interaction_type") != "meeting_transcript":
            continue
        body = payload.get("summary") or payload.get("content") or ""
        if len(body) >= gate_floor:
            continue
        out.append((entry_id, payload, len(body)))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="re-publish (default is a dry run)")
    args = ap.parse_args()

    url = os.environ.get("REDIS_URL")
    if not url:
        print("REDIS_URL is required", file=sys.stderr)
        return 2

    gate_floor, behavior_floor = _floors()
    client = redis.from_url(url, decode_responses=True)
    try:
        lost = await collect(client, gate_floor)
        if not lost:
            # An empty result NAMES ITSELF. "Found nothing" and "the
            # window was wrong" and "the stream aged out" are three
            # different states and must not share one silence.
            print("No transcripts found under the gate floor in the crash "
                  f"window {CRASH_START:%Y-%m-%d %H:%M}Z to "
                  f"{CRASH_END:%Y-%m-%d %H:%M}Z. Either the window is wrong "
                  f"or the stream has aged past it (XLEN "
                  f"{await client.xlen(STREAM)}).")
            return 1

        recoverable = [x for x in lost if x[2] >= behavior_floor]
        users = {p.get('user_id') for _, p, _ in lost}

        print(f"crash window : {CRASH_START:%Y-%m-%d %H:%M}Z to {CRASH_END:%Y-%m-%d %H:%M}Z")
        print(f"gate floor   : {gate_floor}   behavior lane floor: {behavior_floor}")
        print(f"lost         : {len(lost)} transcripts across {len(users)} users")
        print(f"can yield    : {len(recoverable)} at or above the behavior floor")
        print(f"expected nil : {len(lost) - len(recoverable)} below it (correct, not a failure)")
        print()
        for entry_id, payload, size in lost:
            ts = dt.datetime.fromtimestamp(int(entry_id.split("-")[0]) / 1000,
                                           dt.timezone.utc)
            md = payload.get("metadata") or {}
            flag = "CAN YIELD" if size >= behavior_floor else "expect nil"
            print(f"  {ts:%m-%d %H:%M}Z  {size:>5}ch  {flag:<10} "
                  f"user={str(payload.get('user_id'))[:8]} "
                  f"project={md.get('project')!r} origin={md.get('origin_id')}")

        if not args.apply:
            print(f"\nDRY RUN. Re-run with --apply to republish {len(lost)} "
                  "payloads verbatim onto the stream.")
            return 0

        print(f"\nAPPLYING: republishing {len(lost)} payloads verbatim...")
        published = 0
        for entry_id, payload, _ in lost:
            # Verbatim. Not a rebuilt payload: the timestamp inside it is
            # the meeting's own clock and every date downstream reads it.
            await client.xadd(STREAM, {"data": json.dumps(payload)})
            published += 1
        print(f"republished {published} of {len(lost)}")
        print()
        print("ACCEPTANCE IS NOT THAT THIS EXITED 0. Read the worker log: "
              "each should log `extraction_skipped` and NOT "
              "`meeting_summary_failed`, and the "
              f"{len(recoverable)} above the behavior floor are the only "
              "ones that can produce patches.")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
