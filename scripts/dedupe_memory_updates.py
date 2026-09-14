#!/usr/bin/env python3
"""One-time cleanup: each (user, origin) keeps ONE entry on memory_updates.

Scott's ruling, 2026-09-14: the stream should be honest going forward,
not merely non-growing. The ingest handler now refuses a marked replay
of an origin already on the stream (services/ingest_replay); this script
clears the duplicates that landed before it existed and populates the
per-user origin index the handler consults, in one pass.

LATEST WINS, by stream id. The historical duplicates were unmarked or
predate the marker being carried at all, so nothing says which was the
retry; the most recent delivery is the one a client would have believed
succeeded. This is the opposite of the handler's own rule (first bytes
stay on a marked replay), and that is deliberate: the handler acts on a
label the sender put there, this script acts on history with no label.

Entries with no resolvable origin are never touched. A quarter of the
stream on the largest account carries neither origin nor project, and
only an account purge reaches those.

DRY RUN BY DEFAULT. `--apply` XDELs the losers and SADDs every origin
into `ingest_origins:{user_id}`. The dry run is the measurement.

    REDIS_URL=redis://... python scripts/dedupe_memory_updates.py
    REDIS_URL=redis://... python scripts/dedupe_memory_updates.py --apply

XDEL of an already-consumed entry is safe: the worker reads through a
consumer group and has ACKed everything older than its pending window;
transcript_purge has been XDELing consumed entries since #466.
"""
from __future__ import annotations

import argparse
import json
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contextquilt.services.ingest_replay import (  # noqa: E402
    STREAM_KEY, load_all, origins_key, plan_dedupe,
)


async def main(apply: bool) -> int:
    import redis.asyncio as redis_lib

    url = os.environ.get("REDIS_URL")
    if not url:
        print("REDIS_URL is required", file=sys.stderr)
        return 2
    client = redis_lib.from_url(url, decode_responses=True)
    try:
        entries = await load_all(client)
        plan = plan_dedupe(entries)

        # Attribute each deletion to its user for the report.
        per_user = Counter()
        by_id = {eid: raw for eid, raw in entries}
        for eid in plan["delete"]:
            try:
                per_user[json.loads(by_id[eid]).get("user_id")] += 1
            except Exception:
                per_user["<unparsed>"] += 1

        print(f"stream entries:        {len(entries)}")
        print(f"distinct (user,origin): {len(plan['keep'])}")
        print(f"duplicates to delete:  {len(plan['delete'])}")
        for user_id, n in per_user.most_common():
            print(f"  {user_id}: {n}")
        print(f"origins to index:      {sum(len(s) for s in plan['origins'].values())}"
              f" across {len(plan['origins'])} users")
        ms = plan["marker_stats"]
        print("\nShoulderSurf's question, do the repeats carry X-CZ-Recovery:")
        print(f"  repeat entries total:                 {ms['repeats']}")
        print(f"  of which marked (any era):            {ms['repeats_marked']}")
        print(f"  repeats since #476 stamped the marker: {ms['repeats_since_marker']}")
        print(f"  UNMARKED since then:                   {ms['unmarked_since_marker']}"
              "   <- nonzero = an unnamed producer the ingest dedupe does not stop")

        if not apply:
            print("\nDRY RUN. Re-run with --apply to delete and index.")
            return 0

        deleted = 0
        ids = plan["delete"]
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            if batch:
                deleted += await client.xdel(STREAM_KEY, *batch)
        indexed = 0
        for user_id, origins in plan["origins"].items():
            if origins:
                indexed += await client.sadd(origins_key(user_id), *sorted(origins))
        print(f"\nAPPLIED: deleted={deleted} indexed_new_origins={indexed}")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="delete and index (default: dry run)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.apply)))
