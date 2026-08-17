#!/usr/bin/env python3
"""Per-meeting distribution of behavior observations. Read only.

ShoulderSurf's ask, and it is the right one: a MEAN hides the case that
changes the decision. Two observations per meeting across ten meetings
could be one meeting with twenty and nine with nothing, and those two
worlds want opposite calls. Nine zeroes says the prompt only fires on
unusually dramatic material, which is a different problem from "real
meetings are thinner than the synthetic one".

So this reports the shape: the count for every meeting, how many
produced nothing, the median, and the spread. The mean is printed last
and deliberately so.

    DATABASE_URL=... python scripts/inspect_behavior_yield.py
    ... --since 2026-08-16      only meetings ingested after a date
    ... --source behaviour-corpus-rebuild   one replay's own meetings
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

# Every meeting that produced ANY patch, with how many of them were
# behavior observations. LEFT JOIN on purpose: a meeting that yielded
# zero observations is the most important row in the report and an
# INNER JOIN would hide exactly those.
QUERY = """
    SELECT m.origin_id,
           to_char(m.ingested, 'MM-DD HH24:MI') AS ingested,
           COALESCE(b.n, 0) AS observations,
           m.total_patches
    FROM (
        SELECT origin_id, min(created_at) AS ingested,
               count(*) AS total_patches
        FROM context_patches
        WHERE origin_id IS NOT NULL
          AND ($1::timestamptz IS NULL OR created_at >= $1)
        GROUP BY origin_id
    ) m
    LEFT JOIN (
        SELECT origin_id, count(*) AS n
        FROM context_patches
        WHERE patch_type = 'behavior' AND origin_id IS NOT NULL
        GROUP BY origin_id
    ) b ON b.origin_id = m.origin_id
    ORDER BY m.ingested
"""


def histogram(counts: list) -> str:
    """A shape you can read at a glance, which is the whole point."""
    if not counts:
        return "  (nothing to plot)"
    buckets: dict = {}
    for c in counts:
        buckets[c] = buckets.get(c, 0) + 1
    widest = max(buckets.values())
    lines = []
    for value in sorted(buckets):
        bar = "#" * max(1, round(20 * buckets[value] / widest))
        lines.append(f"  {value:>3} observations | {bar} {buckets[value]} meetings")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=None,
                        help="only meetings first ingested on or after this")
    parser.add_argument("--limit", type=int, default=40,
                        help="how many per-meeting rows to print")
    args = parser.parse_args()

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(QUERY, args.since)
    finally:
        await conn.close()

    if not rows:
        print("no meetings matched")
        return 0

    counts = [r["observations"] for r in rows]
    zeroes = sum(1 for c in counts if c == 0)

    print(f"{len(rows)} meetings\n")
    for r in rows[-args.limit:]:
        flag = "  <- nothing" if r["observations"] == 0 else ""
        print(f"  {r['ingested']}  {r['observations']:>3} obs "
              f"({r['total_patches']} patches total){flag}")

    print(f"\nSHAPE")
    print(histogram(counts))
    print(f"\n  meetings with ZERO observations: {zeroes} of {len(rows)} "
          f"({round(100 * zeroes / len(rows))}%)")
    print(f"  median: {statistics.median(counts)}")
    if len(counts) > 1:
        print(f"  spread: {min(counts)} to {max(counts)}")
    # Last, deliberately. The mean is the number that hides the shape.
    print(f"  mean:   {statistics.mean(counts):.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
