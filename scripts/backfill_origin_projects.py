"""Record what already-stored meetings belonged to (the #450 backfill).

#450 makes every new ingest record its project in
`origin_project_assignments`, so a meeting whose only output is
origin-scoped rows (a `moment`, which is `project_scoped: false`) is no
longer orphaned. Meetings ingested BEFORE that have no record, so their
rows keep the old reach: recall reads them as unassigned and can serve
them into any project's chat.

Two sources, in this order, and the order is the point:

  1. A patch from that meeting that still carries a project. This is the
     CURRENT truth, and it already includes anything the user rescoped
     later, so it outranks the ingest's original statement.
  2. The `memory_updates` stream, which holds every ingest payload CQ has
     ever received. For a meeting that produced only origin-scoped rows
     this is the ONLY surviving record of the project it came from.

Writes through the live constant with its ON CONFLICT DO NOTHING, so an
explicit assignment and an explicit unassignment (project_id NULL) are
both preserved: this fills gaps and never argues with a human.

    python scripts/backfill_origin_projects.py [--user <id>]
    python scripts/backfill_origin_projects.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from contextquilt.services.origin_project import (  # noqa: E402
    RECORD_INGEST_PROJECT_SQL,
)

# Every meeting that has patches, with the project its patches still
# carry (NULL when none of them do) and whether a decision is on record.
NEEDS_RECORD_SQL = """
    SELECT ps.subject_key,
           cp.origin_id,
           COALESCE(cp.origin_type, 'meeting')            AS origin_type,
           max(cp.project_id)                             AS stamped_project_id,
           max(cp.project)                                AS stamped_project,
           count(*)                                       AS patches,
           count(*) FILTER (WHERE cp.project_id IS NULL)  AS unstamped
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE cp.origin_id IS NOT NULL
       AND ($1::text IS NULL OR ps.subject_key = $1)
       AND NOT EXISTS (
           SELECT 1 FROM origin_project_assignments opa
            WHERE opa.user_id = split_part(ps.subject_key, ':', 2)
              AND opa.origin_id = cp.origin_id
              AND opa.origin_type = COALESCE(cp.origin_type, 'meeting'))
     GROUP BY 1, 2, 3
     ORDER BY 2
"""


async def _stream_projects(origins: set) -> dict:
    """origin_id -> (project_id, project) from the ingest stream.

    The latest payload naming a project wins: a re-ingest that moved the
    meeting is a newer statement than the first one.
    """
    import redis.asyncio as aioredis

    url = os.environ.get("REDIS_URL")
    if not url:
        host = os.environ.get("REDIS_HOST", "cq-redis")
        port = os.environ.get("REDIS_PORT", "6379")
        pw = os.environ.get("REDIS_PASSWORD")
        url = f"redis://{':' + pw + '@' if pw else ''}{host}:{port}/0"
    r = aioredis.from_url(url, decode_responses=True)
    wanted = {o.lower() for o in origins}
    found: dict = {}
    scanned = 0
    last = "-"
    try:
        while True:
            batch = await r.xrange("memory_updates", last, "+", count=500)
            if not batch:
                break
            for _sid, fields in batch:
                scanned += 1
                raw = fields.get("data") or ""
                low = raw.lower()
                if not any(o in low for o in wanted):
                    continue
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                md = payload.get("metadata") or {}
                oid, pid = md.get("origin_id"), md.get("project_id")
                if oid and pid:
                    found[str(oid).lower()] = (pid, md.get("project"))
            last = "(" + batch[-1][0]
            if len(batch) < 500:
                break
    finally:
        await r.aclose()
    print(f"  scanned {scanned} stream entries, {len(found)} origins carried a project")
    return found


async def main(apply: bool, user: str | None) -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 1
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(NEEDS_RECORD_SQL, f"user:{user}" if user else None)
        print(f"{len(rows)} meetings with no record on file")
        if not rows:
            return 0

        from_patches = [r for r in rows if r["stamped_project_id"]]
        no_stamp = [r for r in rows if not r["stamped_project_id"]]
        print(f"  {len(from_patches)} resolve from a patch that still carries a project")
        print(f"  {len(no_stamp)} carry no project on any patch: the orphan case")
        stream = await _stream_projects({r["origin_id"] for r in no_stamp}) if no_stamp else {}

        plan, unresolved = [], []
        for r in rows:
            pid, name, src = r["stamped_project_id"], r["stamped_project"], "patch"
            if not pid:
                pid, name = stream.get(str(r["origin_id"]).lower(), (None, None))
                src = "stream"
            if not pid:
                unresolved.append(r)
                continue
            plan.append((r["subject_key"].split(":", 1)[-1], r["origin_id"],
                         r["origin_type"], pid, name, src, r["patches"]))

        by_src = Counter(p[5] for p in plan)
        print(f"\nwould record {len(plan)}: {dict(by_src)}")
        print(f"no project anywhere, left alone: {len(unresolved)}")
        print("\nsamples (source, patches, origin, project):")
        for p in plan[:10]:
            print(f"  {p[5]:6} {p[6]:3} {str(p[1])[:8]} -> {p[4] or p[3]}")
        if unresolved:
            print("\nunresolved samples:")
            for r in unresolved[:5]:
                print(f"  {str(r['origin_id'])[:8]} patches={r['patches']}")

        if not apply:
            print("\nDRY RUN. Re-run with --apply to write.\n")
            return 0

        written = 0
        for uid, oid, otype, pid, name, _src, _n in plan:
            await conn.execute(RECORD_INGEST_PROJECT_SQL, uid, str(oid), otype, pid, name)
            written += 1
        print(f"\nAPPLIED: {written} records written (existing rows untouched).")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--user", help="restrict to one user_id")
    sys.exit(asyncio.run(main(ap.parse_args().apply, ap.parse_args().user)))
